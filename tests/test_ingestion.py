from datetime import datetime, timezone

import pytest

from dashboard.database import Database
from dashboard.ingestion import rebuild_derived


class FakeRpc:
    def block_headers(self, numbers):
        return [{"number": numbers[0], "hash": "0xabc", "timestamp": 1_767_268_800}]

    def token_balance(self, token_address, owner, block):
        return 10 * 10**18 if token_address.endswith("01") else 1_000 * 10**6


def test_rebuild_keeps_only_transactions_with_opposing_asset_legs(tmp_path):
    db = Database(tmp_path / "dashboard.duckdb")
    db.create_pool(
        {
            "id": "pool",
            "name": "Pool",
            "chain_key": "base",
            "address": "0x00000000000000000000000000000000000000aa",
            "start_block": 100,
            "contract_type": "two_asset_transfers",
        },
        [
            {"symbol": "A", "address": "0x0000000000000000000000000000000000000001", "decimals": 18, "price_kind": "none"},
            {"symbol": "B", "address": "0x0000000000000000000000000000000000000002", "decimals": 6, "price_kind": "none"},
        ],
    )
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def seed(connection):
        connection.execute("INSERT INTO blocks VALUES (8453, 101, '0xabc', ?)", [timestamp])
        rows = [
            ("0xswap", 0, 0, 1.0),
            ("0xswap", 1, 1, -100.0),
            ("0xnoise", 2, 0, 2.0),
            ("0xnoise", 3, 1, 200.0),
        ]
        for tx_hash, log_index, position, amount in rows:
            connection.execute(
                "INSERT INTO pool_logs VALUES ('pool', 8453, ?, ?, 101, ?, ?, ?, '0x1', '0x2')",
                [tx_hash, log_index, position, str(int(amount * 10 ** (18 if position == 0 else 6))), amount],
            )

    db.write(seed)
    db.connection.execute("""
        INSERT INTO raw_logs SELECT chain_id, tx_hash, log_index, block_number, '0xabc', 0,
        '0xtoken', '0xtopic', NULL, NULL, NULL, '0x0', NULL FROM pool_logs
    """)
    pool = db.get_pool("pool")
    rebuild_derived(db, pool, pool["assets"], FakeRpc(), 101)

    swaps = db.rows("SELECT * FROM swaps WHERE pool_id = 'pool'")
    assert len(swaps) == 1
    assert swaps[0]["tx_hash"] == "0xswap"
    assert swaps[0]["asset0_delta"] == 1.0
    assert swaps[0]["asset1_delta"] == -100.0
    assert swaps[0]["counterparty"] == "0x2"
    db.close()


def test_rebuild_selects_counterparty_by_frequency_outgoing_then_address(tmp_path):
    db = Database(tmp_path / "dashboard.duckdb")
    db.create_pool(
        {
            "id": "pool", "name": "Pool", "chain_key": "base",
            "address": "0x00000000000000000000000000000000000000aa",
            "start_block": 100, "contract_type": "two_asset_transfers",
        },
        [
            {"symbol": "A", "address": "0x0000000000000000000000000000000000000001", "decimals": 18, "price_kind": "none"},
            {"symbol": "B", "address": "0x0000000000000000000000000000000000000002", "decimals": 6, "price_kind": "none"},
        ],
    )
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool = "0x00000000000000000000000000000000000000aa"
    lower = "0x0000000000000000000000000000000000000010"
    higher = "0x0000000000000000000000000000000000000020"
    rows = [
        ("0xfrequency", 0, 0, 1, lower, pool),
        ("0xfrequency", 1, 0, 1, lower, pool),
        ("0xfrequency", 2, 1, -100, pool, higher),
        ("0xoutgoing", 3, 0, 1, lower, pool),
        ("0xoutgoing", 4, 1, -100, pool, higher),
        ("0xlexical", 5, 0, 2, lower, pool),
        ("0xlexical", 6, 1, -50, pool, lower),
        ("0xlexical", 7, 0, 1, higher, pool),
        ("0xlexical", 8, 1, -50, pool, higher),
    ]

    def seed(connection):
        connection.execute("INSERT INTO blocks VALUES (8453, 101, '0xabc', ?)", [timestamp])
        for tx_hash, log_index, position, amount, sender, recipient in rows:
            connection.execute(
                "INSERT INTO pool_logs VALUES ('pool', 8453, ?, ?, 101, ?, ?, ?, ?, ?)",
                [tx_hash, log_index, position, str(amount), amount, sender, recipient],
            )

    db.write(seed)
    db.connection.execute("""
        INSERT INTO raw_logs SELECT chain_id, tx_hash, log_index, block_number, '0xabc', 0,
        '0xtoken', '0xtopic', NULL, NULL, NULL, '0x0', 'signer-' || tx_hash FROM pool_logs
    """)
    configured = db.get_pool("pool")
    rebuild_derived(db, configured, configured["assets"], FakeRpc(), 101)

    # Each side keeps its most frequent peer, ties broken by address; the signer comes from the transaction.
    assert {row["tx_hash"]: (row["sender"], row["recipient"], row["signer"])
            for row in db.rows("SELECT tx_hash, sender, recipient, signer FROM swaps")} == {
        "0xfrequency": (lower, higher, "signer-0xfrequency"),
        "0xoutgoing": (lower, higher, "signer-0xoutgoing"),
        "0xlexical": (lower, lower, "signer-0xlexical"),
    }
    counterparties = {
        row["tx_hash"]: row["counterparty"]
        for row in db.rows("SELECT tx_hash, counterparty FROM swaps")
    }
    assert counterparties == {
        "0xfrequency": lower,
        "0xoutgoing": higher,
        "0xlexical": lower,
    }
    db.close()


def test_rebuild_reconciles_balances_without_rewriting_unchanged_rows(tmp_path):
    db = Database(tmp_path / "dashboard.duckdb")
    db.create_pool(
        {
            "id": "pool",
            "name": "Pool",
            "chain_key": "base",
            "address": "0x00000000000000000000000000000000000000aa",
            "start_block": 100,
            "contract_type": "two_asset_transfers",
        },
        [
            {"symbol": "A", "address": "0x0000000000000000000000000000000000000001", "decimals": 18, "price_kind": "none"},
            {"symbol": "B", "address": "0x0000000000000000000000000000000000000002", "decimals": 6, "price_kind": "none"},
        ],
    )
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def seed(connection):
        connection.execute("INSERT INTO blocks VALUES (8453, 101, '0x101', ?)", [timestamp])
        connection.execute(
            "INSERT INTO pool_logs VALUES ('pool', 8453, '0xswap', 0, 101, 0, '1', 1, '0x1', '0x2')"
        )
        connection.execute(
            "INSERT INTO pool_logs VALUES ('pool', 8453, '0xswap', 1, 101, 1, '-100', -100, '0x1', '0x2')"
        )

    db.write(seed)
    db.connection.execute("""
        INSERT INTO raw_logs SELECT chain_id, tx_hash, log_index, block_number, '0xabc', 0,
        '0xtoken', '0xtopic', NULL, NULL, NULL, '0x0', NULL FROM pool_logs
    """)
    pool = db.get_pool("pool")
    rebuild_derived(db, pool, pool["assets"], FakeRpc(), 101)
    db.connection.execute("CHECKPOINT")
    stored_rows = db.row(
        "SELECT estimated_size FROM duckdb_tables() WHERE table_name = 'balance_points'"
    )["estimated_size"]

    rebuild_derived(
        db, pool, pool["assets"], FakeRpc(), 101, rebuild_swaps=False
    )
    db.connection.execute("CHECKPOINT")
    assert db.row(
        "SELECT estimated_size FROM duckdb_tables() WHERE table_name = 'balance_points'"
    )["estimated_size"] == stored_rows

    db.write(lambda connection: connection.execute(
        "UPDATE balance_points SET asset0_balance = asset0_balance + 5e-12"
    ))
    db.connection.execute("CHECKPOINT")
    stored_rows = db.row(
        "SELECT estimated_size FROM duckdb_tables() WHERE table_name = 'balance_points'"
    )["estimated_size"]
    rebuild_derived(
        db, pool, pool["assets"], FakeRpc(), 101, rebuild_swaps=False
    )
    db.connection.execute("CHECKPOINT")
    assert db.row(
        "SELECT estimated_size FROM duckdb_tables() WHERE table_name = 'balance_points'"
    )["estimated_size"] == stored_rows
    assert db.row("SELECT asset0_balance FROM balance_points")["asset0_balance"] == 10.000000000005

    db.write(lambda connection: connection.execute(
        "UPDATE balance_points SET asset0_balance = asset0_balance + 1e-6"
    ))
    rebuild_derived(
        db, pool, pool["assets"], FakeRpc(), 101, rebuild_swaps=False
    )
    assert db.row("SELECT asset0_balance FROM balance_points")["asset0_balance"] == 10.0

    def add_point(connection):
        connection.execute("INSERT INTO blocks VALUES (8453, 102, '0x102', ?)", [timestamp])
        connection.execute(
            "INSERT INTO pool_logs VALUES ('pool', 8453, '0xdeposit', 0, 102, 0, '5', 5, '0x1', '0x2')"
        )

    db.write(add_point)
    rebuild_derived(
        db, pool, pool["assets"], FakeRpc(), 102, rebuild_swaps=False
    )
    assert db.rows(
        "SELECT block_number, asset0_balance FROM balance_points ORDER BY block_number"
    ) == [
        {"block_number": 101, "asset0_balance": 5.0},
        {"block_number": 102, "asset0_balance": 10.0},
    ]

    db.write(lambda connection: connection.execute(
        "DELETE FROM pool_logs WHERE block_number = 102"
    ))
    rebuild_derived(
        db, pool, pool["assets"], FakeRpc(), 101, rebuild_swaps=False
    )
    assert db.rows(
        "SELECT block_number, asset0_balance FROM balance_points ORDER BY block_number"
    ) == [{"block_number": 101, "asset0_balance": 10.0}]
    db.close()


@pytest.mark.parametrize("asset0_raws, expected_delta", [
    # Nets to zero exactly, but float amounts sum to dust: a deposit, not a swap.
    ([10**17, 2 * 10**17, -3 * 10**17], None),
    # Nets to one raw unit that a float sum erases to 0.0.
    ([10**18, 1, -10**18], 1e-18),
    # 256-bit amounts exceed every DuckDB integer type.
    ([2**255, 2**255 - 1], (2**256 - 1) / 10**18),
])
def test_rebuild_nets_legs_on_exact_raw_amounts(tmp_path, asset0_raws, expected_delta):
    db = Database(tmp_path / "dashboard.duckdb")
    db.create_pool(
        {
            "id": "pool",
            "name": "Pool",
            "chain_key": "base",
            "address": "0x00000000000000000000000000000000000000aa",
            "start_block": 100,
            "contract_type": "two_asset_transfers",
        },
        [
            {"symbol": "A", "address": "0x0000000000000000000000000000000000000001", "decimals": 18, "price_kind": "none"},
            {"symbol": "B", "address": "0x0000000000000000000000000000000000000002", "decimals": 6, "price_kind": "none"},
        ],
    )
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [(0, raw) for raw in asset0_raws] + [(1, -5 * 10**6)]

    def seed(connection):
        connection.execute("INSERT INTO blocks VALUES (8453, 101, '0xabc', ?)", [timestamp])
        for log_index, (position, raw) in enumerate(rows):
            connection.execute(
                "INSERT INTO pool_logs VALUES ('pool', 8453, '0xtx', ?, 101, ?, ?, ?, '0x1', '0x2')",
                [log_index, position, str(raw), raw / 10 ** (18 if position == 0 else 6)],
            )

    db.write(seed)
    db.connection.execute("""
        INSERT INTO raw_logs SELECT chain_id, tx_hash, log_index, block_number, '0xabc', 0,
        '0xtoken', '0xtopic', NULL, NULL, NULL, '0x0', NULL FROM pool_logs
    """)
    pool = db.get_pool("pool")
    rebuild_derived(db, pool, pool["assets"], FakeRpc(), 101)

    swaps = db.rows("SELECT asset0_delta, asset1_delta FROM swaps WHERE pool_id = 'pool'")
    assert swaps == ([] if expected_delta is None else [{"asset0_delta": expected_delta, "asset1_delta": -5.0}])
    db.close()
