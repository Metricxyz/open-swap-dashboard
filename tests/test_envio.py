import logging

import httpx
import pytest
import pytest

from dashboard import envio
from dashboard.rpc import TRANSFER_TOPIC


OWNER = "0x" + "aa" * 20
TOKEN = "0x" + "bb" * 20
OTHER_TOPIC = "0x" + ("00" * 12) + ("cc" * 20)
OWNER_TOPIC = "0x" + OWNER[2:].rjust(64, "0")


class Response:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed",
                request=httpx.Request("POST", "https://8453.hypersync.xyz/query"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self.payload


class HttpClient:
    def __init__(self, responses, **kwargs):
        self.responses = list(responses)
        self.kwargs = kwargs
        self.requests = []
        self.closed = False

    def post(self, url, json):
        self.requests.append((url, json))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def response_page(next_block=102):
    return {
        "data": {
            "blocks": [{"number": 101, "hash": "0xBLOCK", "timestamp": "0x10"}],
            "logs": [{
                "address": TOKEN,
                "topic0": TRANSFER_TOPIC,
                "topic1": OTHER_TOPIC,
                "topic2": OWNER_TOPIC,
                "data": "0x2a",
                "transaction_hash": "0xTX",
                "transaction_index": 2,
                "log_index": "0x3",
                "block_number": 101,
                "block_hash": "0xBLOCK",
            }],
        },
        "next_block": next_block,
    }


def test_transfer_pages_build_filters_and_normalize_response(monkeypatch):
    page = response_page()
    page["data"]["transactions"] = [{"hash": "0xTX", "from": "0xSIGNER"}]
    transport = HttpClient([Response(page)])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    monkeypatch.setattr(envio._RATE_LIMITER, "acquire", lambda rpm, **_kwargs: None)
    client = envio.EnvioClient("secret", 30)

    pages = list(client.transfer_pages(8453, OWNER, 100, 101, [TOKEN]))

    assert pages == [{
        "from_block": 100,
        "to_block": 101,
        "logs": [{
            "address": TOKEN,
            "topics": [TRANSFER_TOPIC, OTHER_TOPIC, OWNER_TOPIC],
            "data": "0x2a",
            "transactionHash": "0xtx",
            "transactionIndex": "0x2",
            "logIndex": "0x3",
            "blockNumber": "0x65",
            "blockHash": "0xblock",
            "transactionFrom": "0xsigner",
        }],
        "headers": [{"number": 101, "hash": "0xblock", "timestamp": 16}],
    }]
    url, payload = transport.requests[0]
    assert url == "https://8453.hypersync.xyz/query"
    assert payload["logs"] == [
        {"topics": [[TRANSFER_TOPIC], [OWNER_TOPIC]], "address": [TOKEN]},
        {"topics": [[TRANSFER_TOPIC], [], [OWNER_TOPIC]], "address": [TOKEN]},
    ]
    assert "transaction_index" in payload["field_selection"]["log"]
    assert payload["field_selection"]["transaction"] == ["hash", "from"]
    client.close()
    assert transport.closed


def test_transfer_pages_follow_next_block_and_reject_missing_metadata(monkeypatch):
    first = response_page(next_block=101)
    first["data"] = {"blocks": [], "logs": []}
    transport = HttpClient([Response(first), Response(response_page(next_block=102))])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    monkeypatch.setattr(envio._RATE_LIMITER, "acquire", lambda rpm, **_kwargs: None)

    pages = list(envio.EnvioClient("secret", 30).transfer_pages(8453, OWNER, 100, 101))
    assert [(page["from_block"], page["to_block"]) for page in pages] == [(100, 100), (101, 101)]
    assert all("address" not in item for item in transport.requests[0][1]["logs"])

    broken = response_page()
    broken["data"]["blocks"] = []
    transport = HttpClient([Response(broken)])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    with pytest.raises(envio.EnvioError, match="omitted block metadata"):
        list(envio.EnvioClient("secret", 30).transfer_pages(8453, OWNER, 100, 101))

    missing_cursor = response_page()
    del missing_cursor["next_block"]
    transport = HttpClient([Response(missing_cursor)])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    with pytest.raises(envio.EnvioError, match="omitted next_block"):
        list(envio.EnvioClient("secret", 30).transfer_pages(8453, OWNER, 100, 101))


def test_transfer_pages_accept_empty_data_list(monkeypatch):
    transport = HttpClient([Response({"data": [], "next_block": 102})])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    monkeypatch.setattr(envio._RATE_LIMITER, "acquire", lambda rpm, **_kwargs: None)

    pages = list(envio.EnvioClient("secret", 30).transfer_pages(8453, OWNER, 100, 101))

    assert pages == [{
        "from_block": 100,
        "to_block": 101,
        "logs": [],
        "headers": [],
    }]


def test_transfer_pages_stop_at_temporary_archive_boundary(monkeypatch):
    first = {"data": [], "next_block": 101, "archive_height": 101}
    boundary = {"data": [], "next_block": 101, "archive_height": 101}
    transport = HttpClient([Response(first), Response(boundary)])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    monkeypatch.setattr(envio._RATE_LIMITER, "acquire", lambda rpm, **_kwargs: None)

    pages = list(envio.EnvioClient("secret", 30).transfer_pages(4663, OWNER, 100, 101))

    assert [(page["from_block"], page["to_block"]) for page in pages] == [(100, 100)]
    assert len(transport.requests) == 2

    transport = HttpClient([Response({
        "data": [], "next_block": 100, "archive_height": 101,
    })])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    with pytest.raises(envio.EnvioError, match="non-progressing next_block"):
        list(envio.EnvioClient("secret", 30).transfer_pages(4663, OWNER, 100, 101))


def test_invalid_page_reports_safe_upstream_diagnostics(monkeypatch, caplog):
    secret = "my-secret-token"
    transport = HttpClient([Response({
        "data": None,
        "error": f"upstream rejected Bearer {secret}",
        "request_id": "request-123",
    })])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    monkeypatch.setattr(envio._RATE_LIMITER, "acquire", lambda rpm, **_kwargs: None)

    with caplog.at_level(logging.ERROR, logger="dashboard.envio"):
        with pytest.raises(envio.EnvioError) as raised:
            list(envio.EnvioClient(secret, 30).transfer_pages(8453, OWNER, 100, 101))

    message = str(raised.value)
    assert "data_type=NoneType" in message
    assert "response_keys=data,error,request_id" in message
    assert "<redacted>" in message
    assert secret not in message
    assert "chain_id=8453 blocks=100-101" in caplog.text
    assert secret not in caplog.text


def test_retry_and_validation(monkeypatch):
    transport = HttpClient([
        Response({}, 429, {"Retry-After": "0"}),
        Response(response_page()),
    ])
    limits = []
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    monkeypatch.setattr(envio._RATE_LIMITER, "acquire", lambda rpm, **_kwargs: limits.append(rpm))
    monkeypatch.setattr(envio.time, "sleep", lambda _delay: None)
    pages = list(envio.EnvioClient("secret", 17).transfer_pages(8453, OWNER, 100, 101))
    assert len(pages) == 1 and limits == [17, 17]

    transport = HttpClient([Response({}, 429), Response({}, 429), Response(response_page())])
    delays = []
    monkeypatch.setattr(envio.time, "sleep", lambda delay: delays.append(delay))
    list(envio.EnvioClient("secret", 17).transfer_pages(8453, OWNER, 100, 101))
    assert delays == [15, 30]

    with pytest.raises(envio.EnvioError, match="Configure an Envio API key"):
        envio.EnvioClient("", 30)
    with pytest.raises(envio.EnvioError, match="must be positive"):
        envio.EnvioClient("secret", 0)


def test_rate_limiter_waits_and_nonretryable_http_fails_once(monkeypatch):
    ticks = iter([0.0, 0.0, 0.5, 10.0])
    delays = []
    monkeypatch.setattr(envio.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(envio.time, "sleep", lambda delay: delays.append(delay))
    limiter = envio._RateLimiter()
    for _ in range(4):
        limiter.acquire(30)
    assert delays == [2.0, 3.5]  # 2s spacing at 30 rpm; the 4th call arrives after its slot

    transport = HttpClient([Response({}, 401)])
    monkeypatch.setattr(envio.httpx, "Client", lambda **kwargs: transport)
    monkeypatch.setattr(envio._RATE_LIMITER, "acquire", lambda rpm, **_kwargs: None)
    with pytest.raises(envio.EnvioError, match="HTTP 401"):
        list(envio.EnvioClient("bad", 30).transfer_pages(8453, OWNER, 100, 101))
    assert len(transport.requests) == 1


@pytest.mark.parametrize('field', ['transaction_index', 'log_index', 'block_number'])
@pytest.mark.parametrize('value', [None, '', 'bad', -1, True, 1.5, 'missing'])
def test_required_ordering_metadata_rejects_page(monkeypatch, field, value):
    page = response_page()
    log = page['data']['logs'][0]
    if value == 'missing':
        del log[field]
    else:
        log[field] = value
    monkeypatch.setattr(envio.httpx, 'Client', lambda **kwargs: HttpClient([Response(page)]))
    monkeypatch.setattr(envio._RATE_LIMITER, 'acquire', lambda *_a, **_k: None)
    with pytest.raises(envio.EnvioError, match='invalid log'):
        list(envio.EnvioClient('secret', 30).transfer_pages(8453, OWNER, 100, 101))


def test_zero_ordering_metadata_is_valid():
    log = response_page()['data']['logs'][0]
    log.update(transaction_index=0, log_index='0x0', block_number='0')
    normalized = envio._normalize_log(log)
    assert [normalized[key] for key in ('transactionIndex', 'logIndex', 'blockNumber')] == ['0x0'] * 3
