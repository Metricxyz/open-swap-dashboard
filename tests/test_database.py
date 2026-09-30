from datetime import datetime, timedelta, timezone

import pytest

from dashboard.database import Database
from dashboard.scheduler import _is_due
from conftest import ASSETS, POOL, configured_database


def _pool_and_assets():
    return (
        {
            "id": "base-test",
            "name": "Test pool",
            "chain_key": "base",
            "address": "0x00000000000000000000000000000000000000aa",
            "start_block": 100,
            "contract_type": "two_asset_transfers",
        },
        [
            {
                "symbol": "WETH",
                "address": "0x0000000000000000000000000000000000000001",
                "decimals": 18,
                "price_kind": "fixed",
                "fixed_price": 100.0,
            },
            {
                "symbol": "USDC",
                "address": "0x0000000000000000000000000000000000000002",
                "decimals": 6,
                "price_kind": "fixed",
                "fixed_price": 1.0,
            },
        ],
    )


def test_price_mapping_change_invalidates_observations_and_queues_sync(tmp_path):
    db = Database(tmp_path / "dashboard.duckdb")
    pool, assets = _pool_and_assets()
    db.create_pool(pool, assets)
    now = datetime.now(timezone.utc)
    db.write(lambda connection: connection.execute(
        "INSERT INTO reference_prices VALUES (?, '0xtx', 0, 1, 'fixed', NULL, NULL, ?, NULL, ?, 100, 0, ?)",
        [pool["id"], now, now, now],
    ))

    changed_assets = [{**assets[0], "fixed_price": 110.0}, assets[1]]
    db.update_pool(pool["id"], pool["name"], pool["start_block"], changed_assets)

    assert db.rows("SELECT * FROM reference_prices") == []
    updated = db.get_pool(pool["id"])
    assert updated["config_revision"] == 2
    assert updated["assets"][0]["fixed_price"] == 110.0
    assert db.price_preferences("base", [assets[0]["address"]])[assets[0]["address"]]["fixed_price"] == 110.0
    assert db.row("SELECT status FROM sync_state WHERE pool_id = ?", [pool["id"]])["status"] == "queued"
    db.close()


def test_scheduler_retries_errors_after_refresh_interval():
    now = datetime.now(timezone.utc)
    assert not _is_due({"status": "error", "sync_updated_at": now}, 60)
    assert _is_due({"status": "error", "sync_updated_at": now - timedelta(seconds=61)}, 60)
    assert _is_due({"status": "queued", "sync_updated_at": now}, 60)
    assert _is_due({"status": "caught_up", "sync_updated_at": None}, 60)
    assert _is_due({"status": "caught_up", "sync_updated_at": (now - timedelta(seconds=61)).replace(tzinfo=None)}, 60)
    assert _is_due({"status": "caught_up", "rebuild_requested": True, "sync_updated_at": now}, 60)


def test_database_pool_rpc_pause_rebuild_and_delete_lifecycle(tmp_path):
    db = configured_database(tmp_path)
    assert db.get_pool("missing") is None
    assert db.list_pools()[0]["assets"][0]["symbol"] == "WETH"
    assert set(db.price_preferences("base")) == {ASSETS[0]["address"], ASSETS[1]["address"]}

    db.set_rpc_override("base", "https://rpc-one.test/key")
    assert db.rpc_overrides() == {"base": "https://rpc-one.test/key"}
    db.set_rpc_override("base", "https://rpc-two.test/key")
    assert db.rpc_overrides()["base"].endswith("two.test/key")
    db.clear_rpc_override("base")
    assert db.rpc_overrides() == {}

    assert db.envio_settings() == {"api_key": None, "rpm_limit": 30}
    db.set_envio_settings(api_key="first-key", rpm_limit=45)
    db.set_envio_settings(api_key=None, rpm_limit=20)
    assert db.envio_settings() == {"api_key": "first-key", "rpm_limit": 20}
    db.clear_envio_key()
    assert db.envio_settings() == {"api_key": None, "rpm_limit": 20}
    with pytest.raises(ValueError, match="API key is required"):
        db.set_envio_settings(api_key=None, rpm_limit=30)

    db.set_paused(POOL["id"], True)
    assert db.list_pools()[0]["paused"] is True
    assert db.row("SELECT status FROM sync_state")["status"] == "paused"
    db.set_paused(POOL["id"], False)
    db.request_rebuild(POOL["id"])
    candidate = db.sync_candidates()[0]
    assert candidate["status"] == "queued" and candidate["rebuild_requested"] is True

    now = datetime.now(timezone.utc)
    def seed(connection):
        connection.execute("INSERT INTO blocks VALUES (8453, 101, '0xblock', ?)", [now])
        connection.execute(
            "INSERT INTO raw_logs VALUES (8453, '0xtx', 0, 101, '0xblock', 0, ?, '0xtopic', NULL, NULL, NULL, '0x01', NULL)",
            [ASSETS[0]["address"]],
        )
        connection.execute(
            "INSERT INTO pool_logs VALUES (?, 8453, '0xtx', 0, 101, 0, '1', 1.0, '0xfrom', '0xto')",
            [POOL["id"]],
        )

    db.write(seed)
    db.delete_pool(POOL["id"])
    assert db.list_pools() == []
    assert db.rows("SELECT * FROM raw_logs") == []
    assert len(db.price_preferences("base")) == 2
    db.close()


def test_cross_chain_price_preference_uses_latest_added_pool(tmp_path):
    db = configured_database(tmp_path)
    assert db.latest_cross_chain_price_preference("ethereum", "WETH", 18)["market"] == "ETHUSDT"
    assert db.latest_cross_chain_price_preference("ethereum", "USDC", 6)["fixed_price"] == 1.0
    assert db.latest_cross_chain_price_preference("base", "WETH", 18) is None

    conflicting_pool = {
        **POOL,
        "id": "arbitrum-test",
        "chain_key": "arbitrum",
        "address": "0x00000000000000000000000000000000000000bb",
    }
    conflicting_assets = [
        {**ASSETS[0], "price_kind": "bybit", "exchange": "bybit", "market": "ETHUSDC"},
        {**ASSETS[1], "price_kind": "none", "fixed_price": None},
    ]
    db.create_pool(conflicting_pool, conflicting_assets)
    db.connection.execute(
        "UPDATE pools SET created_at = created_at + INTERVAL 1 SECOND WHERE id = ?",
        [conflicting_pool["id"]],
    )

    assert db.latest_cross_chain_price_preference("ethereum", "WETH", 18)["price_kind"] == "binance"
    assert db.latest_cross_chain_price_preference("ethereum", "USDC", 6)["price_kind"] == "fixed"
    db.close()


def test_start_block_change_clears_pool_data_and_restarts_cursor(tmp_path):
    db = configured_database(tmp_path)
    now = datetime.now(timezone.utc)

    def seed(connection):
        connection.execute(
            "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) "
            "VALUES (?, '0xtx', 101, ?, 1, -1)",
            [POOL["id"], now],
        )
        connection.execute("INSERT INTO balance_points VALUES (?, 101, ?, 1, 2)", [POOL["id"], now])

    db.write(seed)
    db.update_pool(POOL["id"], "Renamed", 90, ASSETS)

    pool = db.get_pool(POOL["id"])
    state = db.row("SELECT * FROM sync_state WHERE pool_id = ?", [POOL["id"]])
    assert pool["name"] == "Renamed" and pool["start_block"] == 90
    assert state["next_block"] == 90 and state["status"] == "queued"
    assert db.rows("SELECT * FROM swaps") == []
    assert db.rows("SELECT * FROM balance_points") == []
    db.close()


def test_database_write_rolls_back_and_schema_version_is_guarded(tmp_path):
    path = tmp_path / "dashboard.duckdb"
    db = Database(path)

    def broken(connection):
        connection.execute("INSERT INTO chain_rpc_overrides VALUES ('base', 'https://rpc.test', now())")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        db.write(broken)
    assert db.rpc_overrides() == {}
    db.connection.execute("UPDATE schema_meta SET version = 999")
    db.close()

    with pytest.raises(RuntimeError, match="Unsupported database schema"):
        Database(path)


def test_work_generation_guards_background_writes_and_tracks_invalidations(tmp_path):
    db = configured_database(tmp_path)
    initial = db.get_pool(POOL["id"])["work_generation"]
    wrote = []

    assert db.write_if_current(
        POOL["id"], initial, lambda _connection: wrote.append("current")
    )
    db.update_pool(POOL["id"], "Renamed", POOL["start_block"], ASSETS)
    assert db.get_pool(POOL["id"])["work_generation"] == initial

    changed_assets = [{**ASSETS[0], "market": "ETHUSDC"}, ASSETS[1]]
    db.update_pool(POOL["id"], "Renamed", 90, changed_assets)
    changed = db.get_pool(POOL["id"])["work_generation"]
    assert changed == initial + 1
    assert not db.write_if_current(
        POOL["id"], initial, lambda _connection: wrote.append("stale")
    )

    db.set_paused(POOL["id"], True)
    db.set_paused(POOL["id"], False)
    db.request_rebuild(POOL["id"])
    assert db.get_pool(POOL["id"])["work_generation"] == changed + 3
    assert wrote == ["current"]

    db.delete_pool(POOL["id"])
    assert not db.write_if_current(
        POOL["id"], changed + 3, lambda _connection: wrote.append("deleted")
    )
    db.close()


def test_schema_one_migration_backfills_price_preferences(tmp_path):
    path = tmp_path / "v1.duckdb"
    db = Database(path)
    db.create_pool(POOL, ASSETS)
    db.connection.execute("DELETE FROM asset_price_preferences")
    db.connection.execute("UPDATE schema_meta SET version = 1")
    db.close()

    migrated = Database(path)
    assert migrated.row("SELECT version FROM schema_meta")["version"] == 11
    preferences = migrated.price_preferences("base")
    assert preferences[ASSETS[0]["address"]]["market"] == "ETHUSDT"
    assert preferences[ASSETS[1]["address"]]["fixed_price"] == 1.0
    migrated.close()


def test_schema_two_migration_adds_enrichment_attempt_queue(tmp_path):
    path = tmp_path / "v2.duckdb"
    db = Database(path)
    db.connection.execute("UPDATE schema_meta SET version = 2")
    db.close()

    migrated = Database(path)
    assert migrated.row("SELECT version FROM schema_meta")["version"] == 11
    assert migrated.rows("SELECT * FROM price_enrichment_attempts") == []
    migrated.close()


def test_schema_three_migration_adds_work_generation(tmp_path):
    path = tmp_path / "dashboard.duckdb"
    db = configured_database(tmp_path)
    db.connection.execute("ALTER TABLE sync_state DROP COLUMN work_generation")
    db.connection.execute("UPDATE schema_meta SET version = 3")
    db.close()

    migrated = Database(path)
    assert migrated.row("SELECT version FROM schema_meta")["version"] == 11
    assert migrated.row("SELECT work_generation FROM sync_state")["work_generation"] == 1
    migrated.close()


def test_schema_four_migration_removes_rpc_log_range(tmp_path):
    path = tmp_path / "dashboard.duckdb"
    db = Database(path)
    db.connection.execute("ALTER TABLE sync_state ADD COLUMN query_blocks INTEGER DEFAULT 1000")
    db.connection.execute("UPDATE schema_meta SET version = 4")
    db.close()

    migrated = Database(path)
    columns = {row["column_name"] for row in migrated.rows("DESCRIBE sync_state")}
    assert "query_blocks" not in columns
    assert migrated.row("SELECT version FROM schema_meta")["version"] == 11
    migrated.close()


def test_schema_five_migration_rebuilds_counterparties_from_retained_logs(tmp_path):
    path = tmp_path / "dashboard.duckdb"
    db = configured_database(tmp_path)
    now = datetime.now(timezone.utc)
    pool = POOL["address"]
    incoming = "0x0000000000000000000000000000000000000010"
    outgoing = "0x0000000000000000000000000000000000000020"

    def seed(connection):
        connection.execute("INSERT INTO blocks VALUES (8453, 101, '0xblock', ?)", [now])
        for position, asset in enumerate(ASSETS):
            connection.execute("INSERT INTO raw_logs VALUES (8453, '0xtx', ?, 101, '0xblock', 3, ?, '0xtopic', NULL, NULL, NULL, '0x0', NULL)", [position, asset['address']])
        connection.execute(
            "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta, counterparty) VALUES (?, '0xtx', 101, ?, 1, -100, NULL)",
            [POOL["id"], now],
        )
        connection.execute(
            "INSERT INTO pool_logs VALUES (?, 8453, '0xtx', 0, 101, 0, '1', 1, ?, ?)",
            [POOL["id"], incoming, pool],
        )
        connection.execute(
            "INSERT INTO pool_logs VALUES (?, 8453, '0xtx', 1, 101, 1, '-100', -100, ?, ?)",
            [POOL["id"], pool, outgoing],
        )

    db.write(seed)
    db.connection.execute("ALTER TABLE swaps DROP COLUMN counterparty")
    db.connection.execute("UPDATE schema_meta SET version = 5")
    db.close()

    migrated = Database(path)
    assert migrated.row("SELECT version FROM schema_meta")["version"] == 11
    assert migrated.row("SELECT counterparty FROM swaps")["counterparty"] is None
    assert migrated.row('SELECT reason FROM repairs')
    from dashboard.repairs import repair_pool
    from dashboard.ingestion import rebuild_derived
    class Anchor:
        def block_headers(self, _numbers):
            return [dict(timestamp=int(now.timestamp()))]

        def token_balance(self, *_args):
            return 0
    current = migrated.get_pool(POOL['id'])
    assert repair_pool(migrated, current)
    assert rebuild_derived(migrated, current, current['assets'], Anchor(), 101)
    assert migrated.row("SELECT counterparty FROM swaps")["counterparty"] == outgoing
    assert migrated.row("SELECT status FROM sync_state")["status"] == "queued"
    migrated.close()


def test_connection_settings_make_idle_pools_due_immediately(tmp_path):
    db = configured_database(tmp_path)
    set_idle = lambda: db.write(lambda c: c.execute("UPDATE sync_state SET status = 'error', error = 'no key', updated_at = now()"))
    for save in (lambda: db.set_envio_settings(api_key="new-key", rpm_limit=30),
                 lambda: db.set_rpc_override("base", "https://rpc.test")):
        set_idle()
        assert not _is_due(db.sync_candidates()[0], 60)
        save()
        assert _is_due(db.sync_candidates()[0], 60)
        assert db.row("SELECT error FROM sync_state")["error"] is None
    set_idle()
    db.set_rpc_override("ethereum", "https://rpc.test")
    assert not _is_due(db.sync_candidates()[0], 60)
