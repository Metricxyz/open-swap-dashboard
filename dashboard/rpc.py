from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger(__name__)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOKEN0_SELECTOR = "0x0dfe1681"
TOKEN1_SELECTOR = "0xd21220a7"
GET_IMMUTABLES_SELECTOR = "0xbcdb4dad"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class RpcError(RuntimeError):
    pass


def normalize_address(value: str) -> str:
    address = str(value or "").strip().lower()
    if not _ADDRESS_RE.fullmatch(address):
        raise ValueError(f"Invalid EVM address: {value!r}")
    return address


def validate_rpc_url(value: str) -> str:
    url = str(value or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("RPC URL must be an absolute http:// or https:// URL")
    return url


def redact_rpc_url(url: str) -> str:
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    path = "/…" if parts.path not in {"", "/"} else parts.path
    return f"{parts.scheme}://{parts.hostname or ''}{port}{path}"


def sanitize_error(value) -> str:
    """Strip URL credentials, path keys and query secrets before crossing a boundary."""
    return re.sub(r"https?://[^\s\"'<>]+", lambda match: redact_rpc_url(match[0]), str(value))


@dataclass(frozen=True)
class RpcStatus:
    url: str
    display_url: str
    chain_id: int
    latest_block: int


class RpcClient:
    def __init__(self, url: str | list[str] | tuple[str, ...], *, timeout: float = 20.0,
                 expected_chain_id: int | None = None, retry_rounds: int = 1, report=None, stop=None):
        self._urls = list(dict.fromkeys(validate_rpc_url(value) for value in ([url] if isinstance(url, str) else url)))
        if not self._urls:
            raise RpcError("No usable RPC endpoint: empty endpoint list")
        self.url = self._urls[0]
        self._expected_chain_id = expected_chain_id
        self._verified_url = None
        self._retry_rounds = max(1, retry_rounds)
        self._stop = stop if stop is not None else threading.Event()
        self._report = report or (lambda _message: None)
        self._client = httpx.Client(timeout=timeout, headers={"Content-Type": "application/json"})
        self._id = 0
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        start = self._urls.index(self.url)
        attempts = len(self._urls) * self._retry_rounds
        for attempt in range(attempts):
            if self._stop.is_set():
                raise RpcError("RPC request cancelled")
            self.url = self._urls[(start + attempt) % len(self._urls)]
            self._report(f"RPC {method} via {redact_rpc_url(self.url)} · attempt {attempt + 1}/{attempts}")
            try:
                if self._expected_chain_id is not None and self._verified_url != self.url:
                    chain_id = self._call_once("eth_chainId", [])
                    if int(chain_id, 16) != self._expected_chain_id:
                        raise RpcError(f"Expected chain ID {self._expected_chain_id}, received {chain_id}")
                    self._verified_url = self.url
                    if method == "eth_chainId":
                        return chain_id
                return self._call_once(method, params or [])
            except (RpcError, ValueError, TypeError) as exc:
                self._verified_url = None
                message = f"{redact_rpc_url(self.url)}: {sanitize_error(exc)}"
                logger.warning("rpc read failed method=%s attempt=%s/%s error=%s", method, attempt + 1, attempts, message)
                if attempt + 1 < attempts and (attempt + 1) % len(self._urls) == 0:
                    self._report("RPC endpoints unavailable; retrying in 1 second")
                    if self._stop.wait(1):
                        raise RpcError("RPC request cancelled") from None
        raise RpcError(f"No usable RPC endpoint for {method}: {message}") from None

    def _call_once(self, method: str, params: list[Any]) -> Any:
        request_id = self._next_id()
        try:
            response = self._client.post(
                self.url,
                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or []},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RpcError(f"{method} failed via {redact_rpc_url(self.url)}: {sanitize_error(exc)}") from None
        if not isinstance(payload, dict):
            raise RpcError(f"{method} returned an invalid JSON-RPC response")
        if payload.get("error"):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RpcError(f"{method}: {sanitize_error(message)}")
        if "result" not in payload or payload["result"] is None:
            raise RpcError(f"{method} returned no result")
        result = payload["result"]
        if method in {"eth_chainId", "eth_blockNumber"}:
            int(result, 16)
        if method == "eth_getBlockByNumber":
            if not isinstance(result, dict) or not result.get("hash") or not result.get("timestamp"):
                raise RpcError(f"{method} returned an invalid block header")
            int(result["timestamp"], 16)
        return result

    def status(self) -> RpcStatus:
        chain_id = self.call("eth_chainId")
        latest = self.call("eth_blockNumber")
        return RpcStatus(
            url=self.url,
            display_url=redact_rpc_url(self.url),
            chain_id=int(chain_id, 16),
            latest_block=int(latest, 16),
        )

    def code(self, address: str) -> str:
        return str(self.call("eth_getCode", [normalize_address(address), "latest"]))

    def eth_call(self, to: str, data: str, block: int | str = "latest") -> str:
        block_tag = hex(block) if isinstance(block, int) else block
        return str(self.call("eth_call", [{"to": normalize_address(to), "data": data}, block_tag]))

    def token_metadata(self, token_address: str) -> tuple[str, int]:
        token = normalize_address(token_address)
        symbol = _decode_abi_string(self.eth_call(token, "0x95d89b41"))
        decimals_raw = self.eth_call(token, "0x313ce567")
        try:
            decimals = int(decimals_raw, 16)
        except ValueError as exc:
            raise RpcError(f"Token {token} returned invalid decimals") from exc
        if not symbol or not 0 <= decimals <= 255:
            raise RpcError(f"Token {token} returned invalid metadata")
        return symbol.upper(), decimals

    def pool_token_addresses(self, pool_address: str) -> list[str] | None:
        pool = normalize_address(pool_address)
        try:
            tokens = [
                _decode_abi_address(self.eth_call(pool, TOKEN0_SELECTOR)),
                _decode_abi_address(self.eth_call(pool, TOKEN1_SELECTOR)),
            ]
            if tokens[0] != tokens[1]:
                return tokens
        except (RpcError, ValueError):
            pass

        try:
            words = bytes.fromhex(self.eth_call(pool, GET_IMMUTABLES_SELECTOR).removeprefix("0x"))
            # Metric OMM returns factory/token0/token1 in a 24-word struct; older
            # supported pools put token0/token1 at words 2 and 3.
            token_word = 1 if len(words) == 24 * 32 else 2
            tokens = [
                _decode_abi_address(
                    "0x" + words[index * 32:(index + 1) * 32].hex()
                )
                for index in (token_word, token_word + 1)
            ]
            if tokens[0] != tokens[1]:
                return tokens
        except (RpcError, ValueError):
            pass
        return None

    def token_balance(self, token_address: str, owner: str, block: int) -> int:
        owner_hex = normalize_address(owner)[2:].rjust(64, "0")
        result = self.eth_call(token_address, f"0x70a08231{owner_hex}", block)
        return int(result, 16)

    def block_headers(self, block_numbers: list[int]) -> list[dict[str, Any]]:
        unique = sorted(set(int(number) for number in block_numbers))
        headers = []
        for number in unique:
            payload = self.call("eth_getBlockByNumber", [hex(number), False])
            if not isinstance(payload, dict):
                raise RpcError(f"Block {number} was not returned")
            headers.append(
                {
                    "number": number,
                    "hash": str(payload["hash"]).lower(),
                    "timestamp": int(payload["timestamp"], 16),
                }
            )
        return headers

def connect_rpc(urls: list[str] | tuple[str, ...], expected_chain_id: int, *, report=None, stop=None) -> tuple[RpcClient, RpcStatus]:
    client = RpcClient(urls, expected_chain_id=expected_chain_id, retry_rounds=2, report=report, stop=stop)
    try:
        return client, client.status()
    except Exception:
        client.close()
        raise


def _decode_abi_string(value: str) -> str:
    raw = bytes.fromhex(str(value or "0x").removeprefix("0x"))
    if not raw:
        return ""
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 <= len(raw):
            length = int.from_bytes(raw[offset : offset + 32], "big")
            data = raw[offset + 32 : offset + 32 + length]
            return data.decode("utf-8", errors="replace").strip("\x00 ")
    return raw[:32].rstrip(b"\x00").decode("utf-8", errors="replace").strip()


def _decode_abi_address(value: str) -> str:
    raw = bytes.fromhex(str(value or "0x").removeprefix("0x"))
    if len(raw) != 32 or any(raw[:12]) or not any(raw[12:]):
        raise ValueError("Invalid ABI address")
    return normalize_address("0x" + raw[12:].hex())
