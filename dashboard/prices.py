from __future__ import annotations

import csv
import io
import logging
import math
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

import httpx

from .database import Database, upsert_rows


logger = logging.getLogger(__name__)


MAX_TRADE_LAG_SECONDS = 300
MAX_PRICE_SWAPS_PER_SYNC = 10_000
MAX_PRICING_SECONDS_PER_TURN = 30
MAX_PRICE_OBSERVATIONS_PER_COMMIT = 500
MAX_CEX_REQUESTS_PER_SYNC = 50
RECENT_RETRY_SECONDS = 60
HISTORICAL_RETRY_SECONDS = 6 * 60 * 60
# Attempts retried never; analytics treat them as settled, excluded observations.
THIN_MARKET = "infinity"
_PROVIDER_LOCK = threading.Lock()
_PROVIDER_DEADLINES: dict[str, datetime] = {}


def provider_deadline(exchange: str) -> datetime | None:
    with _PROVIDER_LOCK:
        deadline = _PROVIDER_DEADLINES.get(exchange)
        return deadline if deadline and deadline > datetime.now(timezone.utc) else None


def _cooldown(exchange: str, deadline: datetime | None) -> None:
    if deadline:
        with _PROVIDER_LOCK:
            _PROVIDER_DEADLINES[exchange] = max(deadline, _PROVIDER_DEADLINES.get(exchange, deadline))



@dataclass(frozen=True)
class Trade:
    trade_id: str | None
    timestamp: datetime
    price: float


@dataclass(frozen=True)
class LookupResult:
    trades: list[Trade | None]
    attempted: frozenset[int]
    requests: int = 0
    retry_after: datetime | None = None


# ==== Market configuration ====

def validate_market(exchange: str, market: str, market_type: str) -> str:
    if exchange.lower() != "binance":
        raise ValueError("Exchange must be binance; replace retired pricing in the pool editor")
    market = market.upper()
    if deadline := provider_deadline("binance"):
        raise ValueError(f"Provider binance cooldown until {deadline.isoformat()}")
    base = "https://api.binance.com/api/v3" if market_type.lower() == "spot" else "https://fapi.binance.com/fapi/v1"
    with httpx.Client(timeout=15, headers={"User-Agent": "OpenMetricDashboard/0.1"}) as client:
        response = client.get(f"{base}/exchangeInfo", params={"symbol": market})
        _cooldown("binance", _rate_limit_retry_after(response))
        # Spot answers an unknown symbol with 400; futures ignores the filter and lists every market.
        if response.status_code != 400:
            response.raise_for_status()
        symbols = response.json().get("symbols", []) if response.status_code != 400 else []
        row = next((row for row in symbols if str(row.get("symbol", "")).upper() == market), None)
        if row is None:
            raise ValueError(f"Binance {market_type.lower()} market {market} does not exist; check the symbol or try the other market type")
        quote = str(row.get("quoteAsset") or "").upper()
        if quote not in {"USDT", "USDC"}:
            raise ValueError(f"Market {market} must be quoted in USDT or USDC (USD proxies); received {quote or 'unknown quote'}")
        return quote


def suggest_binance_market(symbol: str) -> dict | None:
    """Best-effort default: WETH prices as ETHUSDC spot; other tokens use their exact symbol quoted in USDT
    or USDC, spot before futures. Unreachable or unknown markets simply yield no suggestion."""
    symbol = symbol.upper()
    if symbol == "WETH":
        return {"price_kind": "binance", "exchange": "binance", "market": "ETHUSDC", "market_type": "spot"}
    if not symbol.isalnum():
        return None
    for market_type in ("spot", "futures"):
        for quote in ("USDT", "USDC"):
            try:
                validate_market("binance", symbol + quote, market_type)
            except (ValueError, httpx.HTTPError):
                continue
            return {"price_kind": "binance", "exchange": "binance", "market": symbol + quote, "market_type": market_type}
    return None


# Pending observations and eligibility are shared with worker status. Keep one
# row per asset, including deferred legs but not thin-market ones; batch selection happens downstream.
PENDING_PRICES_SQL = """
    SELECT s.tx_hash, s.timestamp, a.position,
        greatest(coalesce(e.retry_after, '-infinity'::TIMESTAMPTZ),
            CASE WHEN a.price_kind = 'binance' THEN coalesce(?::TIMESTAMPTZ, '-infinity'::TIMESTAMPTZ)
                 ELSE '-infinity'::TIMESTAMPTZ END) AS eligible_at
    FROM swaps s JOIN pools p ON p.id = s.pool_id
    JOIN pool_assets a ON a.pool_id = s.pool_id AND a.price_kind != 'none'
    LEFT JOIN reference_prices r ON r.pool_id = s.pool_id AND r.tx_hash = s.tx_hash
        AND r.asset_position = a.position AND r.config_revision = p.config_revision
    LEFT JOIN price_enrichment_attempts e ON e.pool_id = s.pool_id AND e.tx_hash = s.tx_hash
        AND e.asset_position = a.position AND e.config_revision = p.config_revision
    WHERE s.pool_id = ? AND r.tx_hash IS NULL AND e.retry_after IS DISTINCT FROM 'infinity'
"""


# ==== Price enrichment ====

def enrich_missing_prices(
    db: Database, pool: dict, cache_root: Path, *, limit: int = MAX_PRICE_SWAPS_PER_SYNC, report=None, stop=None
) -> bool:
    started = time.monotonic()
    current = db.get_pool(pool["id"])
    if (not current or current["paused"] or current["incarnation"] != pool.get("incarnation")
            or current["work_generation"] != pool["work_generation"]):
        return False
    emit = report or (lambda *_args, **_kwargs: None)
    pool_name = pool.get("name") or pool["id"]
    assets = db.rows("SELECT * FROM pool_assets WHERE pool_id = ? ORDER BY position", [pool["id"]])
    swaps = db.rows("WITH pending AS (" + PENDING_PRICES_SQL + """), eligible AS (
            SELECT * FROM pending WHERE eligible_at <= now()
        ), batch AS (
            SELECT tx_hash, timestamp FROM eligible GROUP BY tx_hash, timestamp
            ORDER BY timestamp DESC, tx_hash LIMIT ?
        ) SELECT eligible.* FROM eligible JOIN batch USING (tx_hash, timestamp)
    """, [provider_deadline("binance"), pool["id"], max(1, int(limit))])
    if not swaps:
        return False
    emit("pricing", f"PRICING {pool_name} — {len({s['tx_hash'] for s in swaps}):,} SWAPS", pool["id"])

    def checkpoint(message, *, finish_archive=False):
        # Archives restart from byte/row zero, so let them finish past the soft
        # budget. Explicit cancellation still applies throughout archive work.
        if (stop and stop.is_set()) or (not finish_archive and time.monotonic() - started >= MAX_PRICING_SECONDS_PER_TURN):
            return False
        current = db.get_pool(pool["id"])
        if (not current or current["paused"] or current["incarnation"] != pool["incarnation"]
                or current["work_generation"] != pool["work_generation"]):
            return False
        emit("waiting", message, pool["id"])
        return True

    rows_to_write = []
    failed_attempts = []
    remaining_requests = MAX_CEX_REQUESTS_PER_SYNC
    request_count = 0
    # Value fixed legs before spending the turn's time budget on network requests.
    for asset in sorted(assets, key=lambda asset: asset["price_kind"] != "fixed"):
        if time.monotonic() - started >= MAX_PRICING_SECONDS_PER_TURN:
            break
        if stop and stop.is_set():
            return False
        position = int(asset["position"])
        missing = [row for row in swaps if row["position"] == position]
        if not missing or asset["price_kind"] == "none":
            continue
        if asset["price_kind"] == "fixed":
            price = float(asset["fixed_price"])
            if not math.isfinite(price) or price <= 0:
                raise ValueError("Fixed price must be finite and positive")
            for swap in missing:
                target = _utc(swap["timestamp"])
                rows_to_write.append(
                    _price_row(pool, swap["tx_hash"], position, "fixed", None, None, target, None, target, price)
                )
            continue

        if asset.get("quote_error"):
            raise ValueError(asset["quote_error"])
        if not asset.get("quote_verified"):
            raise ValueError(f"Market {asset['market']} is unverified; waiting for quote validation")
        exchange = str(asset["exchange"]).lower()
        market = str(asset["market"]).upper()
        market_type = str(asset["market_type"] or "spot").lower()
        by_day: dict[date, list[dict]] = {}
        for swap in missing:
            by_day.setdefault(_utc(swap["timestamp"]).date(), []).append(swap)
        for day, day_swaps in sorted(by_day.items()):
            if time.monotonic() - started >= MAX_PRICING_SECONDS_PER_TURN:
                break
            if stop and stop.is_set():
                return False
            if provider_deadline(exchange):
                emit("waiting", f"Provider {exchange} rate limit until {provider_deadline(exchange).isoformat()}", pool["id"])
                break
            emit("waiting", f"Downloading {exchange} {market} trades for {day}", pool["id"])
            swaps_by_target: dict[datetime, list[dict]] = {}
            for swap in day_swaps:
                swaps_by_target.setdefault(_utc(swap["timestamp"]), []).append(swap)
            targets = list(swaps_by_target)
            try:
                trades = _trades_for_day(exchange, market, market_type, day, cache_root, report=lambda message, **values: emit("waiting", message, pool["id"], **values), stop=stop, checkpoint=lambda message: checkpoint(message, finish_archive=True))
                if trades is None:
                    lookup = _recent_trade_lookup(
                        exchange, market, market_type, targets, request_budget=remaining_requests, checkpoint=checkpoint
                    )
                else:
                    selected = _select_preceding_trades(trades, targets)
                    preceding = None
                    if any(trade is None for trade in selected):
                        previous_day_trades = _trades_for_day(
                            exchange, market, market_type, day - timedelta(days=1), cache_root, report=lambda message, **values: emit("waiting", message, pool["id"], **values), stop=stop, checkpoint=lambda message: checkpoint(message, finish_archive=True)
                        )
                        if previous_day_trades is not None:
                            preceding = _select_preceding_trades(previous_day_trades, [min(targets)])[0]
                        selected = [trade or preceding for trade in selected]
                    attempted = set(range(len(targets)))
                    retry_after = None
                    missing_indexes = [index for index, trade in enumerate(selected) if trade is None]
                    if missing_indexes and remaining_requests:
                        recent_targets = [targets[index] for index in missing_indexes]
                        recent = _recent_trade_lookup(
                            exchange, market, market_type, recent_targets,
                            request_budget=remaining_requests, checkpoint=checkpoint,
                        )
                        for recent_index, target_index in enumerate(missing_indexes):
                            selected[target_index] = recent.trades[recent_index]
                            if recent_index not in recent.attempted:
                                attempted.discard(target_index)
                        lookup = LookupResult(
                            selected, frozenset(attempted), recent.requests, recent.retry_after
                        )
                    else:
                        lookup = LookupResult(selected, frozenset(attempted))
            except InterruptedError:
                break  # Incomplete archive scans leave this day eligible.
            remaining_requests -= lookup.requests
            request_count += lookup.requests
            for index, (target, trade) in enumerate(zip(targets, lookup.trades, strict=True)):
                if trade is None:
                    if index in lookup.attempted:
                        failed_attempts.extend(
                            (swap["tx_hash"], position, target, lookup.retry_after)
                            for swap in swaps_by_target[target]
                        )
                    continue
                lag_ms = int((target - trade.timestamp).total_seconds() * 1000)
                if not math.isfinite(trade.price) or trade.price <= 0 or lag_ms < 0 or lag_ms > MAX_TRADE_LAG_SECONDS * 1000:
                    # The selected trade is the market's last one before the swap, so a stale
                    # one is final for this config revision: the market is too thin to price it.
                    retry_after = THIN_MARKET if lag_ms > MAX_TRADE_LAG_SECONDS * 1000 else lookup.retry_after
                    failed_attempts.extend(
                        (swap["tx_hash"], position, target, retry_after)
                        for swap in swaps_by_target[target]
                    )
                    continue
                rows_to_write.extend(
                    _price_row(
                        pool, swap["tx_hash"], position, exchange, market, market_type,
                        target, trade.trade_id, trade.timestamp, trade.price,
                    )
                    for swap in swaps_by_target[target]
                )
    if stop and stop.is_set():
        return False
    if not rows_to_write and not failed_attempts:
        return False

    attempted_at = datetime.now(timezone.utc)
    attempt_rows = []
    for tx_hash, position, target, retry_after in failed_attempts:
        retry_seconds = RECENT_RETRY_SECONDS if target.date() >= attempted_at.date() else HISTORICAL_RETRY_SECONDS
        attempt_rows.append([
            pool["id"], tx_hash, position, int(pool["config_revision"]),
            attempted_at, retry_after or attempted_at + timedelta(seconds=retry_seconds),
        ])
    # Release the database between small, guarded commits so dashboard reads
    # need not wait for an entire 10,000-swap pricing pass.
    for offset in range(0, max(len(rows_to_write), len(attempt_rows)), MAX_PRICE_OBSERVATIONS_PER_COMMIT):
        if stop and stop.is_set():
            return False
        price_chunk = rows_to_write[offset:offset + MAX_PRICE_OBSERVATIONS_PER_COMMIT]
        attempt_chunk = attempt_rows[offset:offset + MAX_PRICE_OBSERVATIONS_PER_COMMIT]

        def operation(connection):
            upsert_rows(connection, "reference_prices", price_chunk)
            upsert_rows(connection, "price_enrichment_attempts", attempt_chunk)
            if price_chunk:
                connection.execute("""
                    DELETE FROM price_enrichment_attempts e USING reference_prices r
                    WHERE e.pool_id = ? AND e.config_revision = ?
                      AND r.pool_id = e.pool_id AND r.tx_hash = e.tx_hash
                      AND r.asset_position = e.asset_position AND r.config_revision = e.config_revision
                """, [pool["id"], int(pool["config_revision"])])

        emit("pricing", f"PRICING {pool_name} — SAVING OBSERVATIONS", pool["id"])
        if not db.write_if_current(
            pool["id"], int(pool["work_generation"]), operation, incarnation=pool.get("incarnation"), invalidate=bool(price_chunk)
        ):
            logger.warning("price enrichment discarded stale result pool_id=%s", pool["id"])
            return False
        committed = min(offset + MAX_PRICE_OBSERVATIONS_PER_COMMIT, len(rows_to_write))
        deferred = min(offset + MAX_PRICE_OBSERVATIONS_PER_COMMIT, len(attempt_rows))
        emit("progress", f"Committed {committed} price observations; {deferred} deferred", pool["id"])
    logger.info(
        "price enrichment completed pool_id=%s swaps=%s requests=%s cached=%s deferred=%s",
        pool["id"], len({s["tx_hash"] for s in swaps}), request_count, len(rows_to_write), len(failed_attempts),
    )
    return True


def _price_row(pool, tx_hash, position, provider, market, market_type, target, trade_id, source_ts, price):
    if not math.isfinite(float(price)) or float(price) <= 0:
        raise ValueError("Reference price must be finite and positive")
    lag_ms = int((target - source_ts).total_seconds() * 1000)
    return [
        pool["id"], tx_hash, position, int(pool["config_revision"]), provider,
        market, market_type, target, trade_id, source_ts, float(price), lag_ms,
        datetime.now(timezone.utc),
    ]


# ==== Historical trade archives ====

def _trades_for_day(
    exchange: str,
    market: str,
    market_type: str,
    day: date,
    cache_root: Path,
    *, report=None, stop=None, checkpoint=None,
) -> Iterable[Trade] | None:
    if day >= datetime.now(timezone.utc).date():
        return None
    cache_root.mkdir(parents=True, exist_ok=True)
    if exchange == "binance":
        area = "spot" if market_type == "spot" else "futures/um"
        url = f"https://data.binance.vision/data/{area}/daily/aggTrades/{market}/{market}-aggTrades-{day.isoformat()}.zip"
        path = cache_root / "binance" / market_type / market / f"{day.isoformat()}.zip"
        if not _download(url, path, report=report, stop=stop, checkpoint=checkpoint):
            return None
        return _iter_binance_zip(path, checkpoint=checkpoint)
    raise ValueError("Exchange must be binance")


def _download(url: str, path: Path, *, report=None, stop=None, checkpoint=None) -> bool:
    exchange = "binance"
    if provider_deadline(exchange):
        return False
    if path.exists() and path.stat().st_size > 0:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    try:
        with httpx.stream("GET", url, timeout=60, headers={"User-Agent": "OpenMetricDashboard/0.1"}) as response:
            _cooldown(exchange, _rate_limit_retry_after(response))
            if response.status_code != 200:
                return False
            received = 0
            checked_at = time.monotonic()
            with partial.open("wb") as output:
                for chunk in response.iter_bytes():
                    if stop and stop.is_set():
                        partial.unlink(missing_ok=True)
                        return False
                    if checkpoint and time.monotonic() - checked_at >= 1:
                        if not checkpoint("Downloading market archive"):
                            partial.unlink(missing_ok=True)
                            return False
                        checked_at = time.monotonic()
                    output.write(chunk)
                    received += len(chunk)
                    if report:
                        report(f"Downloading market archive — {received:,} bytes received", download_bytes=received)
        partial.replace(path)
        return True
    except (httpx.HTTPError, OSError):
        partial.unlink(missing_ok=True)
        return False


def _iter_binance_zip(path: Path, *, checkpoint=None) -> Iterable[Trade]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if not names:
            return
        with archive.open(names[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))
            for index, row in enumerate(reader):
                if index % 10000 == 0 and checkpoint and not checkpoint(f"Reading market archive — {index:,} rows"):
                    raise InterruptedError("Archive scan interrupted")
                if len(row) < 6:
                    continue
                try:
                    trade_id = str(row[0])
                    price = float(row[1])
                    timestamp = _timestamp_from_number(float(row[5]))
                except ValueError:
                    continue
                yield Trade(trade_id, timestamp, price)


def _select_preceding_trades(trades: Iterable[Trade], targets: list[datetime]) -> list[Trade | None]:
    indexed_targets = sorted(enumerate(targets), key=lambda item: item[1])
    selected: list[Trade | None] = [None] * len(targets)
    target_index = 0
    previous = None
    for trade in trades:
        if not math.isfinite(trade.price) or trade.price <= 0:
            continue
        while target_index < len(indexed_targets) and indexed_targets[target_index][1] < trade.timestamp:
            original_index, _ = indexed_targets[target_index]
            selected[original_index] = previous
            target_index += 1
        if target_index == len(indexed_targets):
            break
        previous = trade
    while target_index < len(indexed_targets):
        original_index, _ = indexed_targets[target_index]
        selected[original_index] = previous
        target_index += 1
    return selected


# ==== Recent trade APIs ====

def _recent_trade_lookup(
    exchange: str, market: str, market_type: str, targets: list[datetime],
    *, request_budget: int, checkpoint=None,
) -> LookupResult:
    if exchange != "binance":
        raise ValueError("Exchange must be binance")
    if deadline := provider_deadline(exchange):
        return LookupResult([None] * len(targets), frozenset(), 0, deadline)
    base = "https://api.binance.com/api/v3" if market_type == "spot" else "https://fapi.binance.com/fapi/v1"
    with httpx.Client(timeout=15, headers={"User-Agent": "OpenMetricDashboard/0.1"}) as client:
        return _recent_binance_lookup(client, base, market, targets, request_budget=request_budget, checkpoint=checkpoint)


def _recent_binance_lookup(
    client: httpx.Client,
    base: str,
    market: str,
    targets: list[datetime],
    *,
    request_budget: int,
    checkpoint=None,
) -> LookupResult:
    unresolved = sorted(enumerate(targets), key=lambda item: item[1], reverse=True)
    selected: list[Trade | None] = [None] * len(targets)
    attempted = set()
    requests = 0
    while unresolved and requests < request_budget:
        if checkpoint and not checkpoint(f"Binance {market} — recent trade request {requests + 1}/{request_budget}"):
            break
        pivot = unresolved[0][1]
        try:
            response = client.get(
                f"{base}/aggTrades",
                params={
                    "symbol": market,
                    "endTime": int(pivot.timestamp() * 1000),
                    "limit": 1000,
                },
            )
            requests += 1
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            response = exc.response if isinstance(exc, httpx.HTTPStatusError) else None
            retry_after = _rate_limit_retry_after(response)
            _cooldown("binance", retry_after)
            logger.warning(
                "price lookup deferred exchange=binance market=%s end=%s error=%s",
                market, pivot.isoformat(), exc,
            )
            attempted.update(index for index, _ in unresolved)
            return LookupResult(selected, frozenset(attempted), requests, retry_after)
        if not rows:
            attempted.update(index for index, _ in unresolved)
            break
        trades = sorted(
            [Trade(
                str(row.get("a")),
                _timestamp_from_number(float(row["T"])),
                float(row["p"]),
            ) for row in rows],
            key=lambda trade: trade.timestamp,
        )
        covered = [(index, target) for index, target in unresolved if target >= trades[0].timestamp]
        if not covered:
            attempted.add(unresolved.pop(0)[0])
            continue
        covered_targets = [target for _, target in covered]
        for (original_index, _), trade in zip(
            covered,
            _select_preceding_trades(trades, covered_targets),
            strict=True,
        ):
            selected[original_index] = trade
            attempted.add(original_index)
        unresolved = [(index, target) for index, target in unresolved if target < trades[0].timestamp]
        if len(rows) < 1000:
            attempted.update(index for index, _ in unresolved)
            break
    return LookupResult(selected, frozenset(attempted), requests)


# ==== Shared parsing helpers ====

def _rate_limit_retry_after(response: httpx.Response | None) -> datetime | None:
    if response is None or response.status_code not in {418, 429}:
        return None
    try:
        seconds = max(1, int(float(response.headers.get("Retry-After", "60"))))
    except ValueError:
        try:
            return max(datetime.now(timezone.utc) + timedelta(seconds=1), parsedate_to_datetime(response.headers["Retry-After"]))
        except (ValueError, TypeError, KeyError):
            seconds = 60
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _timestamp_from_number(value: float) -> datetime:
    if not math.isfinite(value):
        raise ValueError("Trade timestamp must be finite")
    while value > 10_000_000_000:
        value /= 1000.0
    return datetime.fromtimestamp(value, timezone.utc)


def _utc(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
