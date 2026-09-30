import threading
from datetime import datetime, timezone

from dashboard import scheduler as scheduler_module


from conftest import ASSETS, POOL
from dashboard.database import Database


class FakeDatabase(Database):
    def __init__(self, candidate, tmp_path):
        super().__init__(tmp_path / "scheduler.duckdb")
        self.create_pool({**POOL, "id": candidate["id"]}, [dict(a, quote_verified=True) for a in ASSETS])
        self.connection.execute("UPDATE sync_state SET status = ?, updated_at = ?", [candidate.get("status", "queued"), candidate.get("sync_updated_at", datetime.now(timezone.utc))])


def test_scheduler_processes_candidate_and_stops_cleanly(monkeypatch, tmp_path):
    db = FakeDatabase({"id": "pool", "status": "queued"}, tmp_path)
    worker = scheduler_module.Scheduler(db, tmp_path, refresh_seconds=1)
    synced = []
    enriched = []
    done = threading.Event()

    def sync_pool(_db, pool, **kwargs):
        synced.append(pool["id"])
        assert kwargs["report"] == worker.report

    def enrich(_db, pool, cache, **kwargs):
        enriched.append((pool["id"], cache))
        assert kwargs["limit"] == 10_000
        assert kwargs["report"] == worker.report
        worker._stop.set()
        done.set()
        return True

    monkeypatch.setattr(scheduler_module, "sync_pool", sync_pool)
    monkeypatch.setattr(scheduler_module, "enrich_missing_prices", enrich)
    worker.start()
    first_thread = worker._thread
    worker.start()
    worker.wake()
    assert done.wait(2)
    worker.stop()

    assert worker.refresh_seconds == 10
    assert worker._thread is first_thread
    assert synced == ["pool"]
    assert enriched[0][0] == "pool"
    assert enriched[0][1] == tmp_path / "market_cache"


def test_scheduler_stop_waits_for_in_flight_work(monkeypatch, tmp_path):
    db = FakeDatabase({"id": "pool", "status": "queued"}, tmp_path)
    worker = scheduler_module.Scheduler(db, tmp_path)
    work_started = threading.Event()
    release_work = threading.Event()
    stop_returned = threading.Event()

    def sync_pool(*_args, **_kwargs):
        work_started.set()
        assert release_work.wait(2)

    monkeypatch.setattr(scheduler_module, "sync_pool", sync_pool)
    monkeypatch.setattr(
        scheduler_module,
        "enrich_missing_prices",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("shutdown must skip enrichment")),
    )
    worker.start()
    assert work_started.wait(2)

    stopper = threading.Thread(target=lambda: (worker.stop(), stop_returned.set()))
    stopper.start()
    assert not stop_returned.wait(0.05)
    assert worker._thread.is_alive()

    release_work.set()
    stopper.join(timeout=2)
    assert stop_returned.is_set()
    assert not worker._thread.is_alive()


def test_scheduler_advances_price_backlog_when_rpc_sync_is_not_due(monkeypatch, tmp_path):
    db = FakeDatabase({
        "id": "pool", "status": "caught_up", "sync_updated_at": datetime.now(timezone.utc)
    }, tmp_path)
    worker = scheduler_module.Scheduler(db, tmp_path, refresh_seconds=60)
    priced = threading.Event()

    monkeypatch.setattr(
        scheduler_module, "sync_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("RPC sync is not due")),
    )

    def enrich(*_args, **_kwargs):
        worker._stop.set()
        priced.set()
        return True

    monkeypatch.setattr(scheduler_module, "enrich_missing_prices", enrich)
    worker.start()
    assert priced.wait(2)
    worker.stop()


def test_scheduler_isolates_price_enrichment_failure(monkeypatch, tmp_path, caplog):
    db = FakeDatabase({"id": "pool", "status": "queued"}, tmp_path)
    worker = scheduler_module.Scheduler(db, tmp_path)

    monkeypatch.setattr(scheduler_module, "sync_pool", lambda *_args, **_kwargs: None)

    def fail(*_args, **_kwargs):
        worker._stop.set()
        raise RuntimeError("exchange offline")

    monkeypatch.setattr(scheduler_module, "enrich_missing_prices", fail)
    with caplog.at_level("ERROR", logger="dashboard.scheduler"):
        worker._run()
    assert "price enrichment failed pool_id=pool error=exchange offline" in caplog.text
    assert "RuntimeError: exchange offline" in caplog.text


def test_scheduler_activity_is_a_defensive_snapshot(tmp_path):
    worker = scheduler_module.Scheduler(FakeDatabase({"id": "pool"}, tmp_path), tmp_path)
    worker.report("syncing", "SYNCING POOL — FETCHING LOGS — 42%", "pool")

    snapshot = worker.status()["operation"]
    snapshot["message"] = "changed"

    assert worker.status()["operation"]["message"] == "SYNCING POOL — FETCHING LOGS — 42%"
    assert worker.status()["operation"]["pool_id"] == "pool"
