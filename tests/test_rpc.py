import httpx
import pytest

from dashboard import rpc


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed", request=httpx.Request("POST", "https://rpc.test"), response=httpx.Response(self.status_code)
            )

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class HttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
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


def client_with(monkeypatch, *responses):
    transport = HttpClient(responses)
    monkeypatch.setattr(rpc.httpx, "Client", lambda **_kwargs: transport)
    return rpc.RpcClient("https://user:password@rpc.test/path?token=secret"), transport


def test_address_url_validation_and_redaction():
    address = "0x" + "AB" * 20
    assert rpc.normalize_address(address) == address.lower()
    with pytest.raises(ValueError, match="Invalid EVM address"):
        rpc.normalize_address("0x123")
    assert rpc.validate_rpc_url(" https://rpc.test/path ") == "https://rpc.test/path"
    with pytest.raises(ValueError, match="absolute"):
        rpc.validate_rpc_url("file:///tmp/socket")
    assert rpc.redact_rpc_url("https://user:pw@rpc.test:8545/key?q=secret") == "https://rpc.test:8545/…"


def test_json_rpc_call_success_and_failures(monkeypatch):
    client, transport = client_with(
        monkeypatch,
        Response({"jsonrpc": "2.0", "id": 1, "result": "0x1"}),
        Response({"jsonrpc": "2.0", "id": 2, "error": {"message": "rate limited"}}),
        Response({"jsonrpc": "2.0", "id": 3}),
        Response([]),
        Response(ValueError("not json")),
    )

    assert client.call("eth_chainId") == "0x1"
    assert transport.requests[0][1]["method"] == "eth_chainId"
    with pytest.raises(rpc.RpcError, match="rate limited"):
        client.call("eth_blockNumber")
    with pytest.raises(rpc.RpcError, match="no result"):
        client.call("eth_blockNumber")
    with pytest.raises(rpc.RpcError, match="invalid JSON-RPC"):
        client.call("eth_blockNumber")
    with pytest.raises(rpc.RpcError, match="failed via https://rpc.test/…"):
        client.call("eth_blockNumber")
    client.close()
    assert transport.closed


def test_rpc_reads_send_individual_json_objects(monkeypatch):
    client, transport = client_with(
        monkeypatch,
        Response({"result": "0x1"}), Response({"result": "0x64"}),
        Response({"result": {"hash": "0xabc", "timestamp": "0x10"}}),
        Response({"result": {"hash": "0xdef", "timestamp": "0x20"}}),
    )
    assert client.status().chain_id == 1
    assert len(client.block_headers([2, 1, 2])) == 2
    requests = [body for _, body in transport.requests]
    assert all(isinstance(body, dict) for body in requests)
    assert [body["method"] for body in requests] == [
        "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getBlockByNumber"]
    assert [body["params"] for body in requests[2:]] == [["0x1", False], ["0x2", False]]
    assert len({body["id"] for body in requests}) == 4
    client.close()
    assert transport.closed


def test_rpc_domain_methods_decode_metadata_headers_and_balances(monkeypatch):
    client, _ = client_with(monkeypatch)
    client.call = lambda method, params=None: "0x2105" if method == "eth_chainId" else "0x64"
    status = client.status()
    assert (status.chain_id, status.latest_block) == (8453, 100)

    calls = []
    client.call = lambda method, params: calls.append((method, params)) or "0x6000"
    assert client.code("0x" + "11" * 20) == "0x6000"
    assert client.eth_call("0x" + "22" * 20, "0x1234", 15) == "0x6000"
    assert calls[-1][1][-1] == "0xf"

    symbol = b"WETH"
    dynamic_symbol = (32).to_bytes(32, "big") + len(symbol).to_bytes(32, "big") + symbol.ljust(32, b"\x00")
    results = iter(["0x" + dynamic_symbol.hex(), "0x12"])
    client.eth_call = lambda *_args: next(results)
    assert client.token_metadata("0x" + "33" * 20) == ("WETH", 18)

    results = iter(["0x" + b"USDC".ljust(32, b"\x00").hex(), "0x06"])
    client.eth_call = lambda *_args: next(results)
    assert client.token_metadata("0x" + "33" * 20) == ("USDC", 6)
    results = iter(["0x", "not-hex"])
    client.eth_call = lambda *_args: next(results)
    with pytest.raises(rpc.RpcError, match="invalid decimals"):
        client.token_metadata("0x" + "33" * 20)

    token0 = "0x" + "44" * 20
    token1 = "0x" + "55" * 20
    word = lambda address: "0x" + address[2:].rjust(64, "0")
    client.eth_call = lambda _pool, data: {
        rpc.TOKEN0_SELECTOR: word(token0),
        rpc.TOKEN1_SELECTOR: word(token1),
    }[data]
    assert client.pool_token_addresses("0x" + "33" * 20) == [token0, token1]

    immutables = "0x" + "".join([
        ("11" * 20).rjust(64, "0"),
        ("22" * 20).rjust(64, "0"),
        token0[2:].rjust(64, "0"),
        token1[2:].rjust(64, "0"),
    ])
    def immutable_call(_pool, data):
        if data == rpc.GET_IMMUTABLES_SELECTOR:
            return immutables
        raise rpc.RpcError("unsupported selector")
    client.eth_call = immutable_call
    assert client.pool_token_addresses("0x" + "33" * 20) == [token0, token1]

    metric_immutables = "0x" + "".join([
        ("11" * 20).rjust(64, "0"),
        token0[2:].rjust(64, "0"),
        token1[2:].rjust(64, "0"),
        "1".rjust(64, "0"),
        *("0" * 64 for _ in range(20)),
    ])
    client.eth_call = lambda _pool, data: (
        metric_immutables if data == rpc.GET_IMMUTABLES_SELECTOR
        else (_ for _ in ()).throw(rpc.RpcError("unsupported selector"))
    )
    assert client.pool_token_addresses("0x" + "33" * 20) == [token0, token1]

    client.eth_call = lambda *_args: (_ for _ in ()).throw(rpc.RpcError("unsupported selector"))
    assert client.pool_token_addresses("0x" + "33" * 20) is None

    client.eth_call = lambda _token, data, block: "0x2a"
    assert client.token_balance("0x" + "44" * 20, "0x" + "55" * 20, 9) == 42

    client.call = lambda _method, params: {
        "0x1": {"hash": "0xABC", "timestamp": "0x10"},
        "0x2": {"hash": "0xDEF", "timestamp": "0x20"},
    }[params[0]]
    assert client.block_headers([2, 1, 2]) == [
        {"number": 1, "hash": "0xabc", "timestamp": 16},
        {"number": 2, "hash": "0xdef", "timestamp": 32},
    ]
    calls = []
    client.call = lambda method, params: calls.append((method, params)) or {"hash": params[0], "timestamp": "0x10"}
    assert len(client.block_headers(list(range(25)))) == 25
    assert len(calls) == 25
    assert client.block_headers([]) == []
    assert len(calls) == 25
    client.call = lambda *_args: None
    with pytest.raises(rpc.RpcError, match="was not returned"):
        client.block_headers([1])



def mock_transport(monkeypatch, handler):
    factory = httpx.Client
    clients = []
    def create(**kwargs):
        client = factory(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(rpc.httpx, "Client", create)
    return clients


@pytest.mark.parametrize("failure", [403, 429, 503, "timeout", "jsonrpc", "null", "malformed"])
def test_rpc_rotates_after_connection_and_retries_exact_block(monkeypatch, failure):
    import json
    requests = []
    def handler(request):
        body = json.loads(request.content)
        host, method = request.url.host, body["method"]
        requests.append((host, method, body["params"]))
        if method == "eth_chainId":
            result = "0x2" if host == "wrong.test" else "0x1"
        elif method == "eth_blockNumber":
            result = "0x64"
        elif host == "primary.test":
            if isinstance(failure, int):
                return httpx.Response(failure)
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if failure == "jsonrpc":
                return httpx.Response(200, json={"error": {"code": -32005, "message": "rate limited"}})
            result = None if failure == "null" else {"hash": "0xabc", "timestamp": "not-hex"}
        else:
            result = {"hash": "0xabc", "timestamp": "0x10"}
        return httpx.Response(200, json={"id": body["id"], "result": result})
    transports = mock_transport(monkeypatch, handler)
    messages = []
    client, status = rpc.connect_rpc(["https://primary.test", "https://wrong.test", "https://backup.test"], 1, report=messages.append)
    assert status.latest_block == 100
    assert client.block_headers([84]) == [{"number": 84, "hash": "0xabc", "timestamp": 16}]
    assert client.url == "https://backup.test"
    assert [(host, params) for host, method, params in requests if method == "eth_getBlockByNumber"] == [
        ("primary.test", ["0x54", False]), ("backup.test", ["0x54", False])]
    assert not any(host == "wrong.test" and method != "eth_chainId" for host, method, _ in requests)
    client.block_headers([85])
    assert requests[-1] == ("backup.test", "eth_getBlockByNumber", ["0x55", False])
    assert "backup.test" in messages[-1]
    client.close()
    assert all(c.is_closed for c in transports)


def test_rpc_retries_full_list_once_then_closes_and_redacts(monkeypatch, caplog):
    requests = []
    def handler(request):
        requests.append(request.url.host)
        return httpx.Response(403)
    transports = mock_transport(monkeypatch, handler)
    with pytest.raises(rpc.RpcError, match="No usable RPC endpoint") as caught:
        rpc.connect_rpc(["https://user:password@one.test/secret?key=hidden", "https://two.test"], 1)
    assert requests == ["one.test", "two.test", "one.test", "two.test"]
    assert all(secret not in str(caught.value) + caplog.text for secret in ("password", "secret", "hidden"))
    assert all(c.is_closed for c in transports)


def test_rpc_retry_wait_is_interruptible(monkeypatch):
    import threading
    stop = threading.Event()
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(503)
    transports = mock_transport(monkeypatch, handler)
    def report(message):
        if "retrying in" in message:
            stop.set()
    with pytest.raises(rpc.RpcError, match="cancelled"):
        rpc.connect_rpc(["https://one.test"], 1, report=report, stop=stop)
    assert len(requests) == 1
    assert transports[0].is_closed


def test_single_override_retries_and_recovers(monkeypatch):
    import json
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(503)
        method = json.loads(request.content)['method']
        return httpx.Response(200, json={'result': '0x1' if method == 'eth_chainId' else '0x64'})
    mock_transport(monkeypatch, handler)
    client, status = rpc.connect_rpc(['https://custom.test'], 1)
    assert status.latest_block == 100 and count == 3
    client.close()


def test_balance_read_rotates_without_changing_call_or_block(monkeypatch):
    import json
    calls = []
    def handler(request):
        body = json.loads(request.content)
        if body['method'] == 'eth_chainId':
            return httpx.Response(200, json={'result': '0x1'})
        if body['method'] == 'eth_blockNumber':
            return httpx.Response(200, json={'result': '0x64'})
        calls.append(body['params'])
        return httpx.Response(403) if request.url.host == 'one.test' else httpx.Response(200, json={'result': '0x2a'})
    mock_transport(monkeypatch, handler)
    client, _ = rpc.connect_rpc(['https://one.test', 'https://two.test'], 1)
    assert client.token_balance('0x' + '11' * 20, '0x' + '22' * 20, 84) == 42
    assert len(calls) == 2 and calls[0] == calls[1] and calls[0][1] == '0x54'
    client.close()
