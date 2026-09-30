import csv
import io
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import ASSETS, POOL, configured_database
from dashboard import prices


class JsonResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed", request=httpx.Request("GET", "https://exchange.test"),
                response=httpx.Response(self.status_code, headers=self.headers),
            )

    def json(self):
        return self.payload


class ExchangeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url, params=None):
        self.calls.append((url, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_market_validation_for_binance_and_invalid_inputs(monkeypatch):
    binance = ExchangeClient([JsonResponse({"symbols": [{"symbol": "ETHUSDT", "quoteAsset": "USDT", "quoteCoin": "USDT"}]})])
    monkeypatch.setattr(prices.httpx, "Client", lambda **_kwargs: binance)
    prices.validate_market("BINANCE", "ethusdt", "spot")
    assert binance.calls[0][0].endswith("/api/v3/exchangeInfo")

    with pytest.raises(ValueError, match="retired"):
        prices.validate_market("bybit", "ETHUSDT", "futures")

    missing = ExchangeClient([JsonResponse({"symbols": []})])
    monkeypatch.setattr(prices.httpx, "Client", lambda **_kwargs: missing)
    with pytest.raises(ValueError, match="futures market NOPE does not exist"):
        prices.validate_market("binance", "NOPE", "futures")
    invalid = ExchangeClient([JsonResponse({"code": -1121, "msg": "Invalid symbol."}, status_code=400)])
    monkeypatch.setattr(prices.httpx, "Client", lambda **_kwargs: invalid)
    with pytest.raises(ValueError, match="spot market NOPEUSDT does not exist"):
        prices.validate_market("binance", "NOPEUSDT", "spot")
    monkeypatch.setattr(prices.httpx, "Client", lambda **_kwargs: ExchangeClient([]))
    with pytest.raises(ValueError, match="must be binance"):
        prices.validate_market("unknown", "ETHUSDT", "spot")


def test_binance_market_suggestion_prefers_spot_then_futures(monkeypatch):
    futures = {"symbols": [{"symbol": "HYPEUSDT", "quoteAsset": "USDT"}]}
    client = ExchangeClient([JsonResponse({}, status_code=400), JsonResponse({}, status_code=400), JsonResponse(futures)])
    monkeypatch.setattr(prices.httpx, "Client", lambda **_kwargs: client)
    assert prices.suggest_binance_market("hype") == {
        "price_kind": "binance", "exchange": "binance", "market": "HYPEUSDT", "market_type": "futures"}
    assert [(url.split("/")[2], params["symbol"]) for url, params in client.calls] == [
        ("api.binance.com", "HYPEUSDT"), ("api.binance.com", "HYPEUSDC"), ("fapi.binance.com", "HYPEUSDT")]
    assert prices.suggest_binance_market("WETH")["market"] == "ETHUSDC"
    client.responses = [JsonResponse({}, status_code=400)] * 2 + [JsonResponse({"symbols": []})] * 2
    assert prices.suggest_binance_market("NOPE") is None
    assert prices.suggest_binance_market("A.B") is None


def test_binance_archive_timestamp_units(tmp_path):
    zip_path = tmp_path / "trades.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("ETHUSDT-aggTrades.csv", "1,2000,1,1,1,1767268800000,true,true\n")
    binance = list(prices._iter_binance_zip(zip_path))
    assert binance == [
        prices.Trade("1", datetime(2026, 1, 1, 12, tzinfo=timezone.utc), 2000.0)
    ]

    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w"):
        pass
    assert list(prices._iter_binance_zip(empty_zip)) == []


def test_download_is_atomic_cached_and_cleans_partial_files(monkeypatch, tmp_path):
    class Stream:
        def __init__(self, status=200, fail=False):
            self.status_code = status
            self.fail = fail

        def __enter__(self):
            if self.fail:
                raise httpx.ReadError("offline")
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield b"abc"
            yield b"def"

    path = tmp_path / "cache" / "file.zip"
    monkeypatch.setattr(prices.httpx, "stream", lambda *_args, **_kwargs: Stream())
    assert prices._download("https://archive.test/file", path)
    assert path.read_bytes() == b"abcdef"
    monkeypatch.setattr(prices.httpx, "stream", lambda *_args, **_kwargs: pytest.fail("cache was not reused"))
    assert prices._download("https://archive.test/file", path)

    missing = tmp_path / "missing.zip"
    monkeypatch.setattr(prices.httpx, "stream", lambda *_args, **_kwargs: Stream(status=404))
    assert not prices._download("https://archive.test/missing", missing)
    monkeypatch.setattr(prices.httpx, "stream", lambda *_args, **_kwargs: Stream(fail=True))
    assert not prices._download("https://archive.test/fail", missing)
    assert not missing.with_suffix(".zip.part").exists()


def test_enrichment_writes_fixed_and_preceding_exchange_trades_idempotently(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    day = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=2)

    def seed(connection):
        connection.execute(
            "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES (?, '0x1', 101, ?, 1, -2000), (?, '0x2', 102, ?, -1, 2100)",
            [POOL["id"], day + timedelta(seconds=5), POOL["id"], day + timedelta(seconds=10)],
        )

    db.write(seed)
    archive = [
        prices.Trade("a", day + timedelta(seconds=4), 2000.0),
        prices.Trade("b", day + timedelta(seconds=8), 2100.0),
        prices.Trade("future", day + timedelta(seconds=11), 9999.0),
    ]
    monkeypatch.setattr(prices, "_trades_for_day", lambda *_args, **_kwargs: iter(archive))
    activity = []

    prices.enrich_missing_prices(
        db, db.get_pool(POOL["id"]), tmp_path / "market",
        report=lambda *args, **_kwargs: activity.append(args),
    )
    rows = db.rows("SELECT * FROM reference_prices ORDER BY tx_hash, asset_position")
    assert len(rows) == 4
    assert [(row["tx_hash"], row["asset_position"], row["price"]) for row in rows] == [
        ("0x1", 0, 2000.0), ("0x1", 1, 1.0), ("0x2", 0, 2100.0), ("0x2", 1, 1.0)
    ]
    assert [phase for phase, _message, _pool_id in activity] == ["pricing", "waiting", "pricing", "progress"]
    assert "SAVING OBSERVATIONS" in activity[2][1]
    assert "Committed 4 price observations" in activity[3][1]
    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market")
    assert len(db.rows("SELECT * FROM reference_prices")) == 4
    db.close()


@pytest.mark.parametrize("mutation", ["reconfigure", "delete"])
def test_enrichment_discards_work_invalidated_during_lookup(
    monkeypatch, tmp_path, mutation
):
    db = configured_database(tmp_path)
    target = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)
    )
    db.write(lambda connection: connection.execute(
        "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES (?, '0xstale', 101, ?, 1, -2000)",
        [POOL["id"], target],
    ))
    started = threading.Event()
    release = threading.Event()
    errors = []

    def archive(*_args, **_kwargs):
        started.set()
        assert release.wait(2)
        return iter([prices.Trade("trade", target - timedelta(seconds=1), 2_000.0)])

    monkeypatch.setattr(prices, "_trades_for_day", archive)
    pool = db.get_pool(POOL["id"])

    def enrich():
        try:
            prices.enrich_missing_prices(db, pool, tmp_path / "market")
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=enrich)
    worker.start()
    assert started.wait(2)
    if mutation == "reconfigure":
        db.update_pool(
            POOL["id"], POOL["name"], POOL["start_block"],
            [{**ASSETS[0], "market": "ETHUSDC"}, ASSETS[1]],
        )
    else:
        db.delete_pool(POOL["id"])
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors == []
    assert db.rows("SELECT * FROM reference_prices") == []
    assert db.rows("SELECT * FROM price_enrichment_attempts") == []
    db.close()


def test_enrichment_uses_previous_utc_day_and_rejects_stale_price(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    target = (datetime.now(timezone.utc) - timedelta(days=2)).replace(hour=0, minute=0, second=2, microsecond=0)
    db.write(lambda connection: connection.execute(
        "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES (?, '0xmidnight', 101, ?, 1, -1)", [POOL["id"], target]
    ))

    preceding = prices.Trade("previous", target - timedelta(seconds=3), 123.0)

    def archive(_exchange, _market, _market_type, day, _root, **_kwargs):
        return iter([preceding]) if day == target.date() - timedelta(days=1) else iter([])

    monkeypatch.setattr(prices, "_trades_for_day", archive)
    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market")
    row = db.row("SELECT * FROM reference_prices WHERE asset_position = 0")
    assert row["source_trade_id"] == "previous" and row["lag_ms"] == 3000

    db.write(lambda connection: connection.execute(
        "DELETE FROM reference_prices WHERE pool_id = ?", [POOL["id"]]
    ))
    stale = prices.Trade("stale", target - timedelta(seconds=301), 123.0)
    monkeypatch.setattr(prices, "_trades_for_day", lambda *_args, **_kwargs: iter([stale]))
    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market")
    assert db.rows("SELECT * FROM reference_prices WHERE asset_position = 0") == []
    assert db.row("SELECT provider FROM reference_prices WHERE asset_position = 1")["provider"] == "fixed"
    # A stale preceding trade is final (thin market), so the leg is never retried or reported pending.
    assert db.row("SELECT isinf(retry_after) AS thin FROM price_enrichment_attempts WHERE asset_position = 0")["thin"]
    assert db.rows("WITH pending AS (" + prices.PENDING_PRICES_SQL + ") SELECT * FROM pending", [None, POOL["id"]]) == []
    db.close()


def test_current_day_live_trade_is_not_shadowed_by_previous_day(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    target = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
    db.write(lambda connection: connection.execute(
        """
            INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES
            (?, '0xlive1', 101, ?, 1, -1),
            (?, '0xlive2', 101, ?, 2, -2)
        """,
        [POOL["id"], target, POOL["id"], target],
    ))
    live = prices.Trade("live", target - timedelta(seconds=1), 2_001.0)
    requested = []

    monkeypatch.setattr(prices, "_trades_for_day", lambda *_args, **_kwargs: None)

    def lookup(_exchange, _market, _type, targets, **_kwargs):
        requested.extend(targets)
        return prices.LookupResult([live] * len(targets), frozenset(range(len(targets))))

    monkeypatch.setattr(prices, "_recent_trade_lookup", lookup)

    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market")

    rows = db.rows("SELECT * FROM reference_prices WHERE asset_position = 0")
    assert len(requested) == 1
    assert len(rows) == 2
    assert all(row["source_trade_id"] == "live" for row in rows)
    assert all(row["price"] == 2_001.0 and row["lag_ms"] == 1_000 for row in rows)
    db.close()


def test_enrichment_processes_at_most_one_swap_batch(tmp_path):
    fixed_assets = [
        {**ASSETS[0], "price_kind": "fixed", "fixed_price": 2_000.0,
         "exchange": None, "market": None},
        {**ASSETS[1], "price_kind": "fixed", "fixed_price": 1.0},
    ]
    db = configured_database(tmp_path, assets=fixed_assets)
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.write(lambda connection: connection.execute(
        """
            INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES
            (?, '0x1', 101, ?, 1, -2000),
            (?, '0x2', 102, ?, 1, -2000),
            (?, '0x3', 103, ?, 1, -2000)
        """,
        [POOL["id"], timestamp, POOL["id"], timestamp, POOL["id"], timestamp],
    ))

    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market", limit=2)
    assert db.row("SELECT count(*) AS count FROM reference_prices")["count"] == 4

    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market", limit=2)
    assert db.row("SELECT count(*) AS count FROM reference_prices")["count"] == 6
    db.close()


def test_failed_prices_are_deferred_without_starving_new_swaps(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    db.write(lambda connection: connection.execute(
        """
            INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES
            (?, '0xold', 101, ?, 1, -2000),
            (?, '0xnew', 102, ?, 1, -2000)
        """,
        [POOL["id"], now - timedelta(seconds=20), POOL["id"], now - timedelta(seconds=10)],
    ))
    monkeypatch.setattr(prices, "_trades_for_day", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        prices, "_recent_trade_lookup",
        lambda _exchange, _market, _type, targets, **_kwargs: prices.LookupResult(
            [None] * len(targets), frozenset(range(len(targets)))
        ),
    )

    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market", limit=1)
    assert db.row("SELECT tx_hash FROM reference_prices")["tx_hash"] == "0xnew"
    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market", limit=1)
    assert {row["tx_hash"] for row in db.rows("SELECT tx_hash FROM reference_prices")} == {
        "0xnew", "0xold"
    }
    assert db.row("SELECT count(*) AS count FROM price_enrichment_attempts")["count"] == 2

    db.write(lambda connection: connection.execute(
        "UPDATE price_enrichment_attempts SET retry_after = now() - INTERVAL 1 SECOND WHERE tx_hash = '0xnew'"
    ))
    monkeypatch.setattr(
        prices,
        "_recent_trade_lookup",
        lambda _exchange, _market, _type, targets, **_kwargs: prices.LookupResult(
            [
                prices.Trade("recovered", target - timedelta(seconds=1), 2_000.0)
                for target in targets
            ],
            frozenset(range(len(targets))),
        ),
    )
    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path / "market", limit=1)
    assert db.row(
        "SELECT price FROM reference_prices WHERE tx_hash = '0xnew' AND asset_position = 0"
    )["price"] == 2_000.0
    assert db.rows(
        "SELECT * FROM price_enrichment_attempts WHERE tx_hash = '0xnew'"
    ) == []
    db.close()


def test_retired_provider_cannot_start_recent_requests(monkeypatch):
    monkeypatch.setattr(prices.httpx, 'Client', lambda **_kw: pytest.fail('No retired provider requests'))
    with pytest.raises(ValueError, match='must be binance'):
        prices._recent_trade_lookup('bybit', 'ETHUSDT', 'spot', [datetime.now(timezone.utc)], request_budget=10)


def test_recent_binance_reuses_each_preceding_page_and_jumps_to_uncovered_target():
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    later = start + timedelta(minutes=10)
    targets = [start + timedelta(seconds=5), later + timedelta(seconds=5), later + timedelta(seconds=45)]
    later_rows = [
        {
            "a": index,
            "T": int((later + timedelta(seconds=index / 100)).timestamp() * 1000),
            "p": "2001",
        }
        for index in range(1000)
    ]
    earlier_rows = [
        {"a": 1001, "T": int((start + timedelta(seconds=4)).timestamp() * 1000), "p": "2000"}
    ]
    client = ExchangeClient([JsonResponse(later_rows), JsonResponse(earlier_rows)])

    result = prices._recent_binance_lookup(
        client, "https://binance.test", "ETHUSDT", targets, request_budget=10
    )

    assert [trade.price for trade in result.trades] == [2000.0, 2001.0, 2001.0]
    assert result.attempted == frozenset(range(3))
    assert result.requests == 2
    assert client.calls[0][1] == {
        "symbol": "ETHUSDT", "endTime": int(targets[-1].timestamp() * 1000), "limit": 1000
    }
    assert client.calls[1][1]["endTime"] == int(targets[0].timestamp() * 1000)


def test_recent_binance_request_budget_and_rate_limit_stop_cleanly():
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    later = start + timedelta(minutes=10)
    full_page = [
        {
            "a": index,
            "T": int((later + timedelta(seconds=index / 100)).timestamp() * 1000),
            "p": "2001",
        }
        for index in range(1000)
    ]
    client = ExchangeClient([JsonResponse(full_page)])
    result = prices._recent_binance_lookup(
        client, "https://binance.test", "ETHUSDT", [start, later], request_budget=1
    )
    assert result.trades[0] is None
    assert result.trades[1].price == 2001.0
    assert result.attempted == frozenset({1})
    assert result.requests == 1

    limited = ExchangeClient([JsonResponse({}, status_code=429, headers={"Retry-After": "120"})])
    before = datetime.now(timezone.utc)
    result = prices._recent_binance_lookup(
        limited, "https://binance.test", "ETHUSDT", [start, later], request_budget=10
    )
    assert result.trades == [None, None]
    assert result.attempted == frozenset({0, 1})
    assert result.requests == 1
    assert result.retry_after >= before + timedelta(seconds=119)
    assert len(limited.calls) == 1


def test_preceding_trade_stream_stops_after_last_target():
    target = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def stream():
        yield prices.Trade("before", target - timedelta(seconds=1), 2000.0)
        yield prices.Trade("after", target + timedelta(seconds=1), 2001.0)
        raise AssertionError("archive should stop once every target is resolved")

    assert prices._select_preceding_trades(stream(), [target])[0].trade_id == "before"


def test_trade_archive_routing_and_utc_parser(monkeypatch, tmp_path):
    downloaded = []
    monkeypatch.setattr(prices, "_download", lambda url, path, **_kwargs: downloaded.append((url, path)) or False)
    old_day = datetime.now(timezone.utc).date() - timedelta(days=2)
    assert prices._trades_for_day("binance", "ETHUSDT", "futures", old_day, tmp_path) is None
    assert "/futures/um/" in downloaded[-1][0]
    for provider in ("bybit", "unknown"):
        with pytest.raises(ValueError, match="must be binance"):
            prices._trades_for_day(provider, "ETHUSDT", "spot", old_day, tmp_path)
    assert prices._trades_for_day("binance", "ETHUSDT", "spot", datetime.now(timezone.utc).date(), tmp_path) is None
    assert prices._utc("2026-01-01T12:00:00Z") == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
