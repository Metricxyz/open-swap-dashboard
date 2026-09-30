from __future__ import annotations

import logging
import copy
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .database import Database
from .chains import CHAINS
from .ingestion import sync_pool
from .repairs import repair_pool, repair_price_configuration
from .rpc import sanitize_error
from .prices import MAX_PRICE_SWAPS_PER_SYNC, enrich_missing_prices, provider_deadline, PENDING_PRICES_SQL


logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, db: Database, data_dir: Path, *, refresh_seconds: int = 60):
        self.db = db
        self.data_dir = data_dir
        self.refresh_seconds = max(10, int(refresh_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._activity_lock = threading.Lock()
        self._pools = {}
        self._stage = "ingestion"
        self._fatal_error = None
        self._activity = {
            "phase": "idle",
            "message": "IDLE — SCHEDULER NOT STARTED",
            "pool_id": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def report(self, phase: str, message: str, pool_id: str | None = None, **measurements) -> None:
        if phase == "progress" and self._stage == "ingestion" and pool_id:
            self._refresh_status(pool_id)
        with self._activity_lock:
            self._activity = {
                "phase": phase,
                "message": message,
                "pool_id": pool_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

            if pool_id in self._pools:
                pricing = self._pools[pool_id]["pricing"]
                pricing["waiting_for_ingestion"] = self._stage == "ingestion" and phase != "error"
                if pricing["waiting_for_ingestion"] and not pricing["error"] and pricing["state"] != "paused":
                    pricing.update(state="waiting", wait_reason="Waiting for the current history batch", next_attempt_at=None)
                stage = self._pools[pool_id][self._stage]
                stage.update(state="waiting" if phase in {"waiting", "committing"} else "error" if phase == "error" else "working",
                             action=message, wait_reason=message if phase == "waiting" else None,
                             next_attempt_at=None)
                stage.update(measurements)
                if phase == "progress":
                    stage["last_progress_at"] = datetime.now(timezone.utc).isoformat()
                    stage["committed_action"] = message

    def status(self) -> dict:
        # Never acquire the database lock from this endpoint.
        now = datetime.now(timezone.utc)
        with self._activity_lock:
            pools = copy.deepcopy(self._pools)
            activity = self._activity.copy()
        for pool in pools.values():
            for name in ("ingestion", "pricing"):
                stage = pool[name]
                deadline = stage.get("next_attempt_at")
                if stage["state"] in {"caught_up", "waiting"} and deadline and datetime.fromisoformat(deadline) <= now:
                    stage["state"] = "queued"
                    stage["wait_reason"] = "Eligible; waiting for the worker"
                anchor = max(filter(None, (stage.get("last_progress_at"), stage.get("started_at"))), default=None)
                elapsed = (now - datetime.fromisoformat(anchor)).total_seconds() if anchor else 0
                stage["elapsed_seconds"] = max(0, int(elapsed))
                stage["taking_longer_than_expected"] = stage["state"] == "working" and elapsed >= 60
                if stage["state"] == "queued" and activity.get("pool_id") and activity["pool_id"] != pool["id"]:
                    stage["wait_reason"] = "Queued behind " + pools.get(activity["pool_id"], {}).get("name", activity["pool_id"])
        return {"server_time": now.isoformat(), "worker_alive": bool(self._thread and self._thread.is_alive()),
                "worker_error": self._fatal_error, "operation": activity, "pools": pools}

    def _refresh_status(self, pool_id: str | None = None) -> None:
        pools = self.db.list_pools()
        if pool_id is not None:
            pools = [pool for pool in pools if pool["id"] == pool_id]
        repairs = {r["pool_id"]: r["reason"] for r in self.db.rows("SELECT * FROM repairs")}
        persisted = {(r["pool_id"], r["incarnation"], r["stage"]): r for r in self.db.rows("SELECT * FROM stage_status")}
        result = {}
        for pool in pools:
            item = {"id": pool["id"], "name": pool["name"], "incarnation": pool["incarnation"]}
            indexed = self.db.row("SELECT max(timestamp) AS timestamp FROM blocks WHERE chain_id = ? AND block_number = ?", [CHAINS[pool["chain_key"]].chain_id, pool["next_block"] - 1])
            pending = self.db.row("WITH pending AS (" + PENDING_PRICES_SQL + """
                ) SELECT count(*) AS n, min(eligible_at) AS retry,
                    count(*) FILTER (WHERE eligible_at <= now()) AS eligible,
                    (SELECT count(*) FROM swaps s JOIN pool_assets a ON a.pool_id = s.pool_id AND a.price_kind != 'none'
                     WHERE s.pool_id = ?) AS targets FROM pending
            """, [provider_deadline("binance"), pool["id"], pool["id"]])
            for name in ("ingestion", "pricing"):
                saved = persisted.get((pool["id"], pool["incarnation"], name), {})
                old = self._pools.get(pool["id"], {}) if self._pools.get(pool["id"], {}).get("incarnation") == pool["incarnation"] else {}
                error = saved.get("error") or (pool.get("error") if name == "ingestion" else None)
                state = "paused" if pool["paused"] else "error" if error else "queued"
                deadline = None
                if name == "ingestion" and pool["status"] == "caught_up":
                    state = "caught_up" if not error else state
                    deadline = pool.get("last_success_at")
                    if deadline:
                        deadline += timedelta(seconds=self.refresh_seconds)
                if name == "ingestion" and pool["status"] == "waiting" and not pool["paused"] and not error:
                    state = "waiting"
                    deadline = pool.get("last_success_at")
                    if deadline:
                        deadline += timedelta(seconds=self.refresh_seconds)
                if name == "pricing":
                    deadline = pending["retry"] if not pending["eligible"] else None
                    if not error and not pool["paused"]:
                        state = "caught_up" if not pending["n"] else "waiting" if deadline and deadline > datetime.now(timezone.utc) else "queued"
                item[name] = {**old.get(name, {}), "state": state, "error": error, "wait_reason": None,
                    "waiting_for_ingestion": False,
                    "repair": repairs.get(pool["id"]),
                    "indexed_at": indexed["timestamp"].isoformat() if indexed["timestamp"] else None,
                    "action": error or repairs.get(pool["id"]) or ("Waiting for Envio archive to reach the confirmed target" if state == "waiting" and name == "ingestion" else "Waiting for eligible retry" if state == "waiting" else state.replace('_', ' ')),
                    "last_success_at": str(saved.get("last_success_at") or pool.get("last_success_at") or '') if name == "ingestion" else str(saved.get("last_success_at") or ''),
                    "next_attempt_at": deadline.isoformat() if deadline else None,
                    "active_rpc": pool.get("active_rpc"), "indexed_block": pool["next_block"] - 1, "target_block": pool["finalized_block"],
                    "pending_prices": pending["n"] if name == "pricing" else None,
                    "price_targets": pending["targets"] if name == "pricing" else None}
            result[pool["id"]] = item
        with self._activity_lock:
            if pool_id is None:
                self._pools = result
            elif pool_id in result:
                self._pools[pool_id] = result[pool_id]

    def _finish_stage(self, pool: dict, error: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        self.db.write_if_current(pool["id"], pool["work_generation"], lambda c: c.execute("""
            INSERT INTO stage_status VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (pool_id, incarnation, stage) DO UPDATE
            SET error = excluded.error, last_success_at = coalesce(excluded.last_success_at, stage_status.last_success_at)
        """, [pool["id"], pool["incarnation"], self._stage, error, None if error else now]), incarnation=pool["incarnation"], invalidate=False)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._fatal_error = None
        self.report("starting", "STARTING INDEXER")
        logger.info("scheduler starting refresh_seconds=%s", self.refresh_seconds)
        self._thread = threading.Thread(target=self._guarded_run, name="open-metric-dashboard-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join()
        self.report("idle", "INDEXER STOPPED")
        logger.info("scheduler stopped")

    def wake(self) -> None:
        self._wake.set()

    def _guarded_run(self) -> None:
        try:
            self._run()
            if not self._stop.is_set():
                raise RuntimeError("Worker returned unexpectedly")
        except BaseException as exc:
            self._fatal_error = "Indexer stopped: " + sanitize_error(exc)
            self.report("error", self._fatal_error)
            logger.error(self._fatal_error)
            for pool in self.db.sync_candidates():
                self._finish_stage(pool, self._fatal_error)
            self._refresh_status()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.report("checking", "CHECKING POOLS FOR WORK")
            self._refresh_status()
            candidates = self.db.sync_candidates()
            logger.debug("scheduler cycle candidates=%s", len(candidates))
            did_work = False
            for pool in candidates:
                if self._stop.is_set():
                    break
                self._stage = "ingestion"
                try:
                    if repair_pool(self.db, pool):
                        old = pool
                        pool = next((p for p in self.db.sync_candidates() if p["id"] == old["id"]), None)
                        if not pool or pool["incarnation"] != old["incarnation"]:
                            continue
                        self._refresh_status(pool["id"])
                        self.report("rebuilding", "Repair complete; rebuilding derived history", pool["id"])
                except Exception as exc:
                    self._finish_stage(pool, sanitize_error(exc))
                    logger.exception("pool repair failed pool_id=%s", pool["id"])
                    continue
                if self._pools.get(pool["id"], {}).get("incarnation") != pool["incarnation"]:
                    continue
                if _is_due(pool, self.refresh_seconds):
                    with self._activity_lock:
                        self._pools[pool["id"]][self._stage]["started_at"] = datetime.now(timezone.utc).isoformat()
                    did_work = True
                    sync_pool(
                        self.db,
                        pool,
                        report=self.report, stop=self._stop,
                    )
                    result = self.db.row("SELECT error FROM sync_state WHERE pool_id = ?", [pool["id"]])
                    if not self._stop.is_set():
                        self._finish_stage(pool, result["error"] if result else None)
                    self._refresh_status()
                if self._stop.is_set():
                    break
                refreshed = self.db.get_pool(pool["id"])
                if refreshed and not refreshed.get("paused") and refreshed.get("incarnation") == pool.get("incarnation"):
                    self._stage = "pricing"
                    with self._activity_lock:
                        self._pools[pool["id"]][self._stage]["started_at"] = datetime.now(timezone.utc).isoformat()
                    self.report("pricing", "Pricing downloaded swaps", pool["id"])
                    try:
                        repair_price_configuration(self.db, refreshed, self.report)
                        priced_work = False
                        # Commit manageable batches, but drain eligible backlog before
                        # giving ingestion or another pool the worker again.
                        while not self._stop.is_set():
                            batch_work = enrich_missing_prices(
                                self.db,
                                refreshed,
                                self.data_dir / "market_cache",
                                limit=MAX_PRICE_SWAPS_PER_SYNC,
                                report=self.report, stop=self._stop,
                            )
                            priced_work = batch_work or priced_work
                            self._refresh_status(pool["id"])
                            if not batch_work:
                                break
                            self.report("pricing", "Continuing pricing backlog", pool["id"])
                        did_work = priced_work or did_work
                        if not self._stop.is_set() and (priced_work or not self._pools[pool["id"]]["pricing"]["pending_prices"]):
                            self._finish_stage(refreshed)
                    except Exception as exc:
                        self._finish_stage(refreshed, sanitize_error(exc))
                        self.report(
                            "error",
                            f"PRICE UPDATE FAILED — {pool.get('name') or pool['id']}",
                            pool["id"],
                        )
                        logger.exception(
                            "price enrichment failed pool_id=%s error=%s", pool["id"], exc
                        )
                    self._refresh_status(pool["id"])
            self._refresh_status()
            if not self._stop.is_set():
                self.report("idle", "IDLE — WAITING FOR NEXT REFRESH")
            if self._stop.is_set():
                break
            self._wake.wait(2 if did_work else 10)
            self._wake.clear()


def _is_due(pool: dict, refresh_seconds: int) -> bool:
    if pool.get("status") in {"queued", "syncing"} or (
        pool.get("rebuild_requested") and pool.get("status") not in {"waiting", "error"}
    ):
        return True
    updated = pool.get("sync_updated_at")
    if not isinstance(updated, datetime):
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds() >= refresh_seconds
