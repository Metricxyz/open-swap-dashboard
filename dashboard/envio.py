from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Iterator
from typing import Any

import httpx

from .rpc import TRANSFER_TOPIC, normalize_address


MAX_QUERY_BLOCKS = 250_000
MAX_RETRIES = 5
logger = logging.getLogger(__name__)


class EnvioError(RuntimeError):
    pass


# ==== Process-wide rate limiting ====

class _RateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._requests: deque[float] = deque()

    def acquire(self, rpm_limit: int, *, stop=None, report=None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._requests and self._requests[0] <= now - 60:
                    self._requests.popleft()
                if len(self._requests) < rpm_limit:
                    self._requests.append(now)
                    return
                delay = self._requests[0] + 60 - now
            logger.info("envio rate limit reached rpm_limit=%s wait_seconds=%.1f", rpm_limit, delay)
            if report:
                report(f"Envio rate limit; eligible in {delay:.0f}s")
            if stop is not None:
                if stop.wait(delay):
                    raise EnvioError("Shutdown requested")
            else:
                time.sleep(delay)


_RATE_LIMITER = _RateLimiter()


# ==== HyperSync requests ====

class EnvioClient:
    def __init__(self, api_key: str, rpm_limit: int, *, timeout: float = 60.0):
        key = str(api_key or "").strip()
        self.rpm_limit = int(rpm_limit)
        if not key:
            raise EnvioError("Configure an Envio API key in Network Configuration")
        if self.rpm_limit <= 0:
            raise EnvioError("Envio RPM limit must be positive")
        self.stop = None
        self.report = None
        self._api_key = key
        self._client = httpx.Client(timeout=timeout, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        })

    def close(self) -> None:
        self._client.close()

    def transfer_pages(
        self,
        chain_id: int,
        owner_address: str,
        from_block: int,
        to_block: int,
        token_addresses: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        owner_topic = "0x" + normalize_address(owner_address)[2:].rjust(64, "0")
        addresses = sorted(normalize_address(address) for address in token_addresses or [])
        logs_filter = [
            {"topics": [[TRANSFER_TOPIC], [owner_topic]]},
            {"topics": [[TRANSFER_TOPIC], [], [owner_topic]]},
        ]
        if addresses:
            for log_filter in logs_filter:
                log_filter["address"] = addresses

        requested_end = int(to_block) + 1
        cursor = int(from_block)
        request_count = 0
        while cursor < requested_end:
            if self.stop and self.stop.is_set():
                return
            request_count += 1
            request_end = min(cursor + MAX_QUERY_BLOCKS, requested_end)
            logger.info(
                "envio query chain_id=%s request=%s blocks=%s-%s token_count=%s",
                chain_id, request_count, cursor, request_end - 1, len(addresses),
            )
            payload = {
                "from_block": cursor,
                "to_block": request_end,
                "logs": logs_filter,
                "field_selection": {
                    "block": ["number", "hash", "timestamp"],
                    "log": [
                        "address", "topic0", "topic1", "topic2", "topic3", "data",
                        "transaction_hash", "transaction_index", "log_index",
                        "block_number", "block_hash",
                    ],
                    # Default join mode returns each matched log's transaction; "from" is its signer.
                    "transaction": ["hash", "from"],
                },
            }
            body = self._post(f"https://{int(chain_id)}.hypersync.xyz/query", payload)
            data = body.get("data", body)
            chunks = data if isinstance(data, list) else [data]
            if any(not isinstance(chunk, dict) for chunk in chunks):
                diagnostic = _response_diagnostic(body, self._api_key)
                logger.error(
                    "envio invalid page chain_id=%s blocks=%s-%s %s",
                    chain_id, cursor, request_end - 1, diagnostic,
                )
                raise EnvioError(f"Envio returned invalid page data ({diagnostic})")
            raw_logs = []
            raw_blocks = []
            signers = {}
            for chunk in chunks:
                raw_logs.extend(chunk.get("logs") or [])
                raw_blocks.extend(chunk.get("blocks") or [])
                for tx in chunk.get("transactions") or []:
                    if tx.get("hash") and tx.get("from"):
                        signers[str(tx["hash"]).lower()] = str(tx["from"]).lower()

            next_block = body.get("next_block", body.get("nextBlock"))
            if next_block is None and isinstance(data, dict):
                next_block = data.get("next_block", data.get("nextBlock"))
            if next_block is None:
                raise EnvioError("Envio response omitted next_block")
            next_block = _integer(next_block)
            archive_height = body.get("archive_height", body.get("archiveHeight"))
            archive_height = _integer(archive_height) if archive_height is not None else None
            if (
                next_block == cursor
                and not raw_logs
                and not raw_blocks
                and archive_height is not None
                and archive_height <= cursor
            ):
                logger.info(
                    "envio archive boundary chain_id=%s cursor=%s archive_height=%s",
                    chain_id, cursor, archive_height,
                )
                if self.report:
                    self.report(f"Waiting for Envio archive; indexed boundary {cursor}, target {requested_end - 1}")
                return
            if next_block <= cursor:
                raise EnvioError(f"Envio returned non-progressing next_block {next_block}")
            next_block = min(next_block, request_end, requested_end)
            headers = [_normalize_block(item) for item in raw_blocks]
            header_by_number = {item["number"]: item for item in headers}
            logs_by_key = {}
            for item in raw_logs:
                log = _normalize_log(item)
                log["transactionFrom"] = signers.get(log["transactionHash"])
                logs_by_key[(log["transactionHash"], log["logIndex"])] = log
            logs = list(logs_by_key.values())
            missing_headers = sorted({int(log["blockNumber"], 16) for log in logs} - header_by_number.keys())
            if missing_headers:
                raise EnvioError(f"Envio omitted block metadata for {missing_headers[0]}")
            yield {
                "from_block": cursor,
                "to_block": next_block - 1,
                "logs": sorted(logs, key=lambda item: (
                    int(item["blockNumber"], 16),
                    int(item["transactionIndex"], 16),
                    int(item["logIndex"], 16),
                )),
                "headers": sorted(header_by_number.values(), key=lambda item: item["number"]),
            }
            cursor = next_block

    def _post(self, url: str, payload: dict) -> dict:
        last_error = "unknown error"
        for attempt in range(1, MAX_RETRIES + 1):
            _RATE_LIMITER.acquire(self.rpm_limit, stop=self.stop, report=self.report)
            if self.report:
                self.report("Waiting for Envio response")
            started = time.perf_counter()
            try:
                response = self._client.post(url, json=payload)
                duration_ms = round((time.perf_counter() - started) * 1000)
                request_id = _response_request_id(response)
                logger.debug(
                    "envio response status=%s duration_ms=%s request_id=%s",
                    response.status_code, duration_ms, request_id or "none",
                )
                if response.status_code != 429 and response.status_code < 500:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        diagnostic = _http_response_diagnostic(response, self._api_key)
                        logger.warning(
                            "envio request rejected status=%s request_id=%s %s",
                            response.status_code, request_id or "none", diagnostic,
                        )
                        raise EnvioError(
                            f"Envio request failed with HTTP {response.status_code} ({diagnostic})"
                        ) from exc
                    body = response.json()
                    if not isinstance(body, dict):
                        logger.error(
                            "envio invalid response status=%s request_id=%s response_type=%s",
                            response.status_code, request_id or "none", type(body).__name__,
                        )
                        raise EnvioError(
                            "Envio returned invalid JSON "
                            f"(response_type={type(body).__name__} request_id={request_id or 'none'})"
                        )
                    return body
                diagnostic = _http_response_diagnostic(response, self._api_key)
                last_error = (
                    f"HTTP {response.status_code} request_id={request_id or 'none'} {diagnostic}"
                )
                retry_after = response.headers.get("Retry-After")
            except EnvioError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
                retry_after = None
            if attempt == MAX_RETRIES:
                break
            try:
                delay = max(0.0, float(retry_after)) if retry_after is not None else min(30.0, 2 ** (attempt - 1))
            except ValueError:
                delay = min(30.0, 2 ** (attempt - 1))
            logger.warning(
                "envio request retry error=%s next_attempt=%s max_attempts=%s delay_seconds=%.1f",
                last_error, attempt + 1, MAX_RETRIES, delay,
            )
            if self.report:
                self.report(f"Envio retry {attempt + 1}; eligible in {delay:.0f}s")
            if self.stop is not None:
                if self.stop.wait(delay):
                    return {}
            else:
                time.sleep(delay)
        raise EnvioError(f"Envio request failed after {MAX_RETRIES} attempts: {last_error}")


def _response_request_id(response) -> str | None:
    for name in ("x-request-id", "request-id", "cf-ray"):
        value = response.headers.get(name)
        if value:
            return str(value)[:200]
    return None


def _http_response_diagnostic(response, secret: str) -> str:
    try:
        body = response.json()
    except (ValueError, TypeError):
        return "response_body=non-JSON"
    return _response_diagnostic(body, secret)


def _response_diagnostic(body: Any, secret: str) -> str:
    if not isinstance(body, dict):
        return f"response_type={type(body).__name__}"
    data = body.get("data", body)
    keys = ",".join(sorted(str(key) for key in body)) or "none"
    details = [f"response_keys={keys}", f"data_type={type(data).__name__}"]
    upstream_error = next(
        (body.get(key) for key in ("error", "message", "detail") if body.get(key) is not None),
        None,
    )
    if upstream_error is not None:
        text = json.dumps(upstream_error, default=str, separators=(",", ":"))
        text = text.replace(secret, "<redacted>") if secret else text
        text = re.sub(r"(?i)bearer\s+[^\s\"']+", "Bearer <redacted>", text)
        details.append(f"upstream_error={text[:500]}")
    return " ".join(details)


# ==== Response normalization ====

def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Expected a nonnegative integer")
    number = int(value, 16 if value.startswith("0x") else 10) if isinstance(value, str) else value
    if number < 0:
        raise ValueError("Expected a nonnegative integer")
    return number


def _normalize_block(item: dict) -> dict:
    try:
        normalized = {
            "number": _integer(item.get("number", item.get("block_number"))),
            "hash": str(item.get("hash", item.get("block_hash"))).lower(),
            "timestamp": _integer(item["timestamp"]),
        }
        if not normalized["hash"].startswith("0x"):
            raise ValueError("missing block hash")
        return normalized
    except (KeyError, TypeError, ValueError) as exc:
        raise EnvioError("Envio returned invalid block metadata") from exc


def _normalize_log(item: dict) -> dict:
    try:
        topics = [item.get(f"topic{index}") for index in range(4)]
        normalized = {
            "address": normalize_address(item["address"]),
            "topics": [str(topic).lower() for topic in topics if topic is not None],
            "data": str(item.get("data") or "0x"),
            "transactionHash": str(item.get("transaction_hash", item.get("transactionHash"))).lower(),
            "transactionIndex": hex(_integer(item.get("transaction_index", item.get("transactionIndex")))),
            "logIndex": hex(_integer(item.get("log_index", item.get("logIndex")))),
            "blockNumber": hex(_integer(item.get("block_number", item.get("blockNumber")))),
            "blockHash": str(item.get("block_hash", item.get("blockHash"))).lower(),
        }
        if not normalized["transactionHash"].startswith("0x") or not normalized["blockHash"].startswith("0x"):
            raise ValueError("missing hashes")
        return normalized
    except (KeyError, TypeError, ValueError) as exc:
        raise EnvioError("Envio returned an invalid log") from exc
