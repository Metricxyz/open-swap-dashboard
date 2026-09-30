"""Resumable page boundaries and fair sequential ingestion/pricing turns."""
from types import SimpleNamespace

import pytest

from conftest import ASSETS, POOL, configured_database
from dashboard import ingestion, prices, scheduler
from dashboard.database import Database
from test_ingestion_pipeline import install_sync_clients, transfer_log


def page(block, count=1):
    other = "0x00000000000000000000000000000000000000bb"
    logs = []
    for index in range(count):
        for position, asset in enumerate(ASSETS):
            logs.append(transfer_log(
                token=asset["address"], sender=other if position == 0 else POOL["address"],
                recipient=POOL["address"] if position == 0 else other,
                amount=10 ** asset["decimals"], tx=f"0x{block:x}{index:x}",
                index=index * 2 + position, block=block,
            ))
    return {"from_block": block, "to_block": block, "logs": logs,
            "headers": [{"number": block, "hash": f"0x{block:x}", "timestamp": 1767268800 + block}]}


def test_complete_pages_resume_after_restart_and_match_unbounded(monkeypatch, tmp_path):
    fixed_assets = [{**asset, "price_kind": "fixed", "fixed_price": 1.0} for asset in ASSETS]
    db = configured_database(tmp_path / "batched", assets=fixed_assets)
    db.request_rebuild(POOL["id"])
    monkeypatch.setattr(ingestion, "MAX_NEW_SWAPS_PER_TURN", 2)
    pages = [page(100, 3), page(101, 2), page(102, 1)]
    install_sync_clients(monkeypatch, db, pages, latest=122)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path)
    assert db.row("SELECT count(*) AS n FROM swaps")["n"] == 3
    assert db.row("SELECT next_block, scan_next_block, status, rebuild_requested FROM sync_state") == {
        "next_block": 101, "scan_next_block": 101, "status": "queued", "rebuild_requested": True}
    assert not db.rows("SELECT * FROM balance_snapshots")
    path = db.path
    db.close()
    db = Database(path)
    _, envio, _ = install_sync_clients(monkeypatch, db, pages[1:], latest=122)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert envio.calls[0][2] == 101
    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path)
    assert db.row("SELECT scan_next_block FROM sync_state")["scan_next_block"] == 102
    _, envio, _ = install_sync_clients(monkeypatch, db, pages[2:], latest=122)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert envio.calls[0][2] == 102
    prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path)
    assert db.row("SELECT status, scan_next_block, rebuild_requested FROM sync_state") == {
        "status": "caught_up", "scan_next_block": None, "rebuild_requested": False}
    reference = configured_database(tmp_path / "unbounded", assets=fixed_assets)
    reference.request_rebuild(POOL["id"])
    monkeypatch.setattr(ingestion, "MAX_NEW_SWAPS_PER_TURN", 100)
    install_sync_clients(monkeypatch, reference, pages, latest=122)
    ingestion.sync_pool(reference, reference.sync_candidates()[0])
    prices.enrich_missing_prices(reference, reference.get_pool(POOL["id"]), tmp_path)
    for table, columns in [
        ("swaps", "tx_hash, block_number, tx_index, timestamp, asset0_delta, asset1_delta"),
        ("balance_points", "*"), ("balance_snapshots", "*"),
        ("reference_prices", "tx_hash, asset_position, price"),
    ]:
        assert db.rows(f"SELECT {columns} FROM {table} ORDER BY ALL") == reference.rows(f"SELECT {columns} FROM {table} ORDER BY ALL")
    db.close()
    reference.close()


@pytest.mark.parametrize("limit", ["pages", "time", "interruption"])
def test_sparse_pages_yield_and_resume_committed_cursor(monkeypatch, tmp_path, limit):
    db = configured_database(tmp_path)
    monkeypatch.setattr(ingestion, "MAX_PAGES_PER_TURN", 1 if limit == "pages" else 10)
    clock = [0]
    monkeypatch.setattr(ingestion, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    import threading
    stop = threading.Event()

    def report(phase, *_args, **_kwargs):
        if phase == "progress":
            clock[0] = 31 if limit == "time" else 0
            if limit == "interruption":
                stop.set()

    install_sync_clients(monkeypatch, db, [page(100, 0), page(101, 0)], latest=122)
    ingestion.sync_pool(db, db.sync_candidates()[0], report=report, stop=stop)
    state = db.row("SELECT next_block, scan_next_block FROM sync_state")
    assert state == {"next_block": 101, "scan_next_block": 101}
    _, envio, _ = install_sync_clients(monkeypatch, db, [page(101, 0)], latest=122)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert envio.calls[0][2] == 101
    db.close()


def test_replay_pages_do_not_count_as_new_swaps_or_rewind_frontier(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    pages = [page(100, 3), page(101, 3)]
    install_sync_clients(monkeypatch, db, pages)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    monkeypatch.setattr(ingestion, "MAX_NEW_SWAPS_PER_TURN", 1)
    monkeypatch.setattr(ingestion, "MAX_PAGES_PER_TURN", 2)
    _, envio, _ = install_sync_clients(monkeypatch, db, [*pages, page(102)], latest=122)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert envio.calls[0][2] == 100
    assert db.row("SELECT next_block, scan_next_block FROM sync_state") == {"next_block": 102, "scan_next_block": 102}
    _, envio, _ = install_sync_clients(monkeypatch, db, [page(102)], latest=122)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert envio.calls[0][2] == 102
    assert db.row("SELECT count(*) AS n FROM swaps")["n"] == 7
    db.close()


def test_pricing_budget_commits_fixed_and_completed_days(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    install_sync_clients(monkeypatch, db, [page(100), page(101)])
    ingestion.sync_pool(db, db.sync_candidates()[0])
    db.connection.execute("UPDATE swaps SET timestamp = timestamp + INTERVAL '1 day' WHERE block_number = 101")
    clock = [0]
    calls = []
    monkeypatch.setattr(prices, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def trades(_exchange, _market, _kind, day, *_args, **_kwargs):
        calls.append(day)
        clock[0] = 31
        from datetime import datetime, timezone
        return [prices.Trade("1", datetime.combine(day, datetime.min.replace(hour=12).time(), tzinfo=timezone.utc), 2000)]

    monkeypatch.setattr(prices, "_trades_for_day", trades)
    assert prices.enrich_missing_prices(db, db.get_pool(POOL["id"]), tmp_path)
    assert len(calls) == 1
    assert db.rows("SELECT asset_position, count(*) AS n FROM reference_prices GROUP BY asset_position ORDER BY asset_position") == [
        {"asset_position": 0, "n": 1}, {"asset_position": 1, "n": 2}]
    db.close()


def test_worker_alternates_batches_and_pricing_across_pools(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    db.create_pool({**POOL, "id": "second", "address": "0x00000000000000000000000000000000000000bb"}, ASSETS)
    worker = scheduler.Scheduler(db, tmp_path)
    operations = []
    monkeypatch.setattr(scheduler, "sync_pool", lambda _db, pool, **_kw: operations.append(("ingest", pool["id"])))

    def price(_db, pool, *_args, **_kwargs):
        operations.append(("price", pool["id"]))
        if len(operations) == 8:
            worker._stop.set()
        return False  # Unavailable prices must not hold up the next pool or batch.

    monkeypatch.setattr(scheduler, "enrich_missing_prices", price)
    monkeypatch.setattr(worker._wake, "wait", lambda *_args: None)
    worker._run()
    assert [op for op, _ in operations] == ["ingest", "price"] * 4
    assert {pool for _, pool in operations[:4]} == {POOL["id"], "second"}
    assert {pool for _, pool in operations[4:]} == {POOL["id"], "second"}
    db.close()


def test_v7_upgrade_adds_cursor_without_repeating_repairs(tmp_path):
    db = configured_database(tmp_path)
    incarnation = db.get_pool(POOL["id"])["incarnation"]
    db.connection.execute("ALTER TABLE sync_state DROP COLUMN scan_next_block")
    db.connection.execute("UPDATE schema_meta SET version = 7")
    path = db.path
    db.close()
    db = Database(path)
    assert db.get_pool(POOL["id"])["incarnation"] == incarnation
    assert db.sync_candidates()[0]["scan_next_block"] is None
    assert db.rows("SELECT * FROM repairs") == []
    assert db.row("SELECT version FROM schema_meta")["version"] == 11
    db.close()


@pytest.mark.parametrize("mode", ["priced", "missing", "cooldown", "pause", "shutdown"])
def test_pricing_drains_batches_before_next_pool_and_yields_safely(monkeypatch, tmp_path, mode):
    from datetime import datetime, timedelta, timezone

    assets = [{**asset, "price_kind": "fixed", "fixed_price": 1.0} for asset in ASSETS]
    if mode in {"missing", "cooldown"}:
        assets[0] = ASSETS[0]
    db = configured_database(tmp_path, assets=assets)
    install_sync_clients(monkeypatch, db, [page(100, 5)])
    ingestion.sync_pool(db, db.sync_candidates()[0])
    db.connection.execute("UPDATE sync_state SET status = 'queued'")
    worker = scheduler.Scheduler(db, tmp_path)
    monkeypatch.setattr(scheduler, "MAX_PRICE_SWAPS_PER_SYNC", 2)
    operations = []
    monkeypatch.setattr(scheduler, "sync_pool", lambda _db, pool, **_kw: operations.append(("ingest", pool["id"])))
    monkeypatch.setattr(prices, "_trades_for_day", lambda *_args, **_kw: None)
    monkeypatch.setattr(prices, "_recent_trade_lookup", lambda _e, _m, _k, targets, **_kw:
                        prices.LookupResult([None] * len(targets), frozenset(range(len(targets)))))
    if mode == "cooldown":
        monkeypatch.setattr(prices, "provider_deadline", lambda _exchange: datetime.now(timezone.utc) + timedelta(minutes=1))
    batches = []

    def price(database, pool, *args, **kwargs):
        operations.append(("price", pool["id"]))
        result = prices.enrich_missing_prices(database, pool, *args, **kwargs)
        if pool["id"] == POOL["id"]:
            batches.append(result)
            if len(batches) == 1:
                if mode == "pause":
                    db.set_paused(pool["id"], True)
                if mode == "shutdown":
                    worker._stop.set()
        return result

    monkeypatch.setattr(scheduler, "enrich_missing_prices", price)
    # Add the next pool after the first so equal status order is deterministic.
    db.create_pool({**POOL, "id": "second", "address": "0x00000000000000000000000000000000000000bb"}, assets)
    monkeypatch.setattr(worker._wake, "wait", lambda *_args: worker._stop.set())
    worker._run()
    if mode in {"priced", "missing"}:
        assert batches == [True, True, True, False]
        assert operations[:5] == [("ingest", POOL["id"])] + [("price", POOL["id"])] * 4
        assert db.row("SELECT count(*) AS n FROM reference_prices")["n"] == (10 if mode == "priced" else 5)
        assert db.row("SELECT count(*) AS n FROM price_enrichment_attempts")["n"] == (0 if mode == "priced" else 5)
    elif mode == "shutdown":
        assert batches == [True]
        assert not any(pool == "second" for _, pool in operations)
    elif mode == "cooldown":
        assert batches == [True, True, True, False]
        assert db.row("SELECT count(*) AS n FROM reference_prices")["n"] == 5
        assert not db.rows("SELECT * FROM price_enrichment_attempts")
    else:
        assert batches == [True, False]
    if mode != "shutdown":
        assert operations[-2:] == [("ingest", "second"), ("price", "second")]
    db.close()
