from datetime import datetime, timedelta, timezone

import pytest

from dashboard.analytics import dashboard_payload, pool_summaries
from dashboard.database import Database


def _configured_database(tmp_path):
    db = Database(tmp_path / "dashboard.duckdb")
    db.create_pool(
        {
            "id": "base-test",
            "mtm_mode": "as_is",
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
                "price_kind": "binance",
                "exchange": "binance",
                "market": "ETHUSDT",
                "market_type": "spot",
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
    return db


def test_dashboard_flow_and_mtm_match_windowed_swap_math(tmp_path):
    db = _configured_database(tmp_path)
    now = datetime.now(timezone.utc)

    def seed(connection):
        connection.execute(
            "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta, counterparty) VALUES (?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?)",
            [
                "base-test", "0x1", 101, now - timedelta(minutes=2), 1.0, -100.0,
                "0x0000000000000000000000000000000000000011",
                "base-test", "0x2", 102, now - timedelta(minutes=1), -0.5, 60.0,
                "0x0000000000000000000000000000000000000022",
            ],
        )
        for tx_hash, timestamp, p0 in (
            ("0x1", now - timedelta(minutes=2), 110.0),
            ("0x2", now - timedelta(minutes=1), 120.0),
        ):
            connection.execute(
                "INSERT INTO reference_prices VALUES (?, ?, 0, 1, 'binance', 'ETHUSDT', 'spot', ?, ?, ?, ?, 10, ?)",
                ["base-test", tx_hash, timestamp, tx_hash, timestamp, p0, now],
            )
            connection.execute(
                "INSERT INTO reference_prices VALUES (?, ?, 1, 1, 'fixed', NULL, NULL, ?, NULL, ?, 1.0, 0, ?)",
                ["base-test", tx_hash, timestamp, timestamp, now],
            )
        connection.execute(
            "INSERT INTO balance_snapshots VALUES ('base-test', 0, 102, ?, 10.0), ('base-test', 1, 102, ?, 1000.0)",
            [now, now],
        )

    db.write(seed)
    db.connection.execute("UPDATE swaps SET tx_index = 0")
    db.connection.execute("UPDATE pool_assets SET quote_verified = TRUE, quote_asset = 'USDT' WHERE price_kind = 'binance'")
    payload = dashboard_payload(db, "base-test", 24)

    assert payload["metrics"]["flow_pnl_usd"] == pytest.approx(10.0)
    assert payload["metrics"]["mtm_pnl_usd"] == pytest.approx(20.0)
    assert payload["metrics"]["volume_usd"] == pytest.approx(170.0)
    assert payload["metrics"]["price_coverage_pct"] == 100.0
    assert payload["metrics"]["mtm_priced_swap_count"] == 2
    assert payload["metrics"]["mtm_coverage_pct"] == 100.0
    assert payload["metrics"]["mtm_mark"] == "external"
    assert payload["pnl_series"][0]["flow"] == pytest.approx(10.0)
    assert payload["pnl_series"][1]["mtm"] == pytest.approx(20.0)
    assert payload["swap_page"]["items"][0]["edge_bps"] == pytest.approx(0.0)
    assert payload["swap_page"]["items"][1]["edge_bps"] == pytest.approx(909.090909)
    assert payload["swap_page"]["items"][0]["counterparty"].endswith("22")
    assert payload["swap_page"]["items"][0]["volume_usd"] == pytest.approx(60.0)
    assert payload["explorer_address_url"] == "https://basescan.org/address/{address}"

    summaries = pool_summaries(db, db.list_pools())
    summary = summaries["pools"]["base-test"]
    assert summary == {
        "flow_complete": True, "mtm_complete": True, "volume_complete": True,
        "volume_usd": 170.0,
        "flow_pnl_usd": 10.0,
        "flow_apr_pct": 165.91,
        "mtm_pnl_usd": 20.0,
        "mtm_apr_pct": 331.82,
        "tvl_usd": 2200.0,
        "apr_tvl_usd": 2200.0,
        "swap_count": 2, "mtm_baseline_available": None,
        "last_swap_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
    }
    assert summaries["chains"]["base"] == {**{key: value for key, value in summary.items() if key not in {"last_swap_at", "swap_count", "mtm_baseline_available"}}, "tvl_complete": True, "apr_tvl_complete": True}
    assert summaries["total"] == summaries["chains"]["base"]
    assert pool_summaries(db, db.list_pools(), 48)["pools"]["base-test"]["flow_apr_pct"] == 82.95
    assert pool_summaries(db, db.list_pools(), 48)["pools"]["base-test"]["mtm_apr_pct"] == 165.91

    # After a withdrawal, APR uses the window's peak TVL (at current marks), not the smaller current TVL.
    db.write(lambda connection: connection.execute(
        "INSERT INTO balance_points VALUES ('base-test', 50, ?, 100.0, 10000.0), ('base-test', 60, ?, 20.0, 2000.0), ('base-test', 102, ?, 10.0, 1000.0)",
        [now - timedelta(hours=40), now - timedelta(hours=30), now],
    ))
    day = pool_summaries(db, db.list_pools())
    assert day["pools"]["base-test"]["tvl_usd"] == 2200.0
    assert day["pools"]["base-test"]["apr_tvl_usd"] == 4400.0  # carried in at the window start; block 50 is older
    assert day["pools"]["base-test"]["flow_apr_pct"] == 82.95
    assert day["total"]["apr_tvl_usd"] == 4400.0 and day["total"]["flow_apr_pct"] == 82.95
    two_days = pool_summaries(db, db.list_pools(), 48)["pools"]["base-test"]
    assert (two_days["apr_tvl_usd"], two_days["flow_apr_pct"], two_days["mtm_apr_pct"]) == (22000.0, 8.3, 16.59)
    db.close()


def test_unpriced_swaps_are_visible_but_excluded_from_pnl(tmp_path):
    db = _configured_database(tmp_path)
    now = datetime.now(timezone.utc)
    db.write(lambda connection: connection.execute(
        "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES ('base-test', '0xmissing', 101, ?, 1.0, -100.0)", [now]
    ))

    db.connection.execute("UPDATE swaps SET tx_index = 0")
    db.connection.execute("UPDATE pool_assets SET quote_verified = TRUE, quote_asset = 'USDT' WHERE price_kind = 'binance'")
    payload = dashboard_payload(db, "base-test", 24)

    assert payload["metrics"]["swap_count"] == 1
    assert payload["metrics"]["priced_swap_count"] == 0
    assert payload["metrics"]["flow_pnl_usd"] is None
    assert payload["swap_page"]["items"][0]["flow_pnl"] is None
    assert payload["swap_page"]["items"][0]["edge_bps"] is None
    assert payload["swap_page"]["items"][0]["counterparty"] is None
    assert payload["swap_page"]["items"][0]["volume_usd"] is None
    db.close()


def test_thin_market_swaps_settle_flow_pnl_and_keep_mtm_inventory(tmp_path):
    db = _configured_database(tmp_path)
    now = datetime.now(timezone.utc)

    def seed(connection):
        connection.execute(
            "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta, tx_index) VALUES "
            "('base-test', '0xpriced', 101, ?, 1.0, -100.0, 0), ('base-test', '0xthin', 102, ?, 1.0, -105.0, 0)",
            [now - timedelta(minutes=2), now - timedelta(minutes=1)],
        )
        for tx_hash, timestamp in (("0xpriced", now - timedelta(minutes=2)), ("0xthin", now - timedelta(minutes=1))):
            connection.execute(
                "INSERT INTO reference_prices VALUES ('base-test', ?, 1, 1, 'fixed', NULL, NULL, ?, NULL, ?, 1.0, 0, ?)",
                [tx_hash, timestamp, timestamp, now],
            )
        connection.execute(
            "INSERT INTO reference_prices VALUES ('base-test', '0xpriced', 0, 1, 'binance', 'ETHUSDT', 'spot', ?, 't', ?, 110.0, 10, ?)",
            [now - timedelta(minutes=2), now - timedelta(minutes=2), now],
        )
        connection.execute("INSERT INTO price_enrichment_attempts VALUES ('base-test', '0xthin', 0, 1, ?, 'infinity')", [now])

    db.write(seed)
    db.connection.execute("UPDATE pool_assets SET quote_verified = TRUE, quote_asset = 'USDT' WHERE price_kind = 'binance'")
    metrics = dashboard_payload(db, "base-test", 24)["metrics"]

    assert metrics["thin_market_count"] == 1 and metrics["priced_swap_count"] == 1
    assert metrics["flow_pnl_usd"] == pytest.approx(10.0) and metrics["flow_complete"]
    # Both swaps' inventory (+2 WETH, -205 USDC) at the latest external mark of 110.
    assert metrics["mtm_pnl_usd"] == pytest.approx(15.0) and metrics["mtm_complete"]
    assert metrics["volume_usd"] == pytest.approx(215.0) and metrics["volume_complete"]
    summary = pool_summaries(db, db.list_pools())["pools"]["base-test"]
    assert summary["flow_complete"] and summary["mtm_complete"]
    db.close()


def test_mtm_uses_pool_cross_price_when_only_one_asset_has_external_prices(tmp_path):
    db = _configured_database(tmp_path)
    now = datetime.now(timezone.utc)

    def seed(connection):
        connection.execute(
            """
            UPDATE pool_assets
            SET symbol = 'AI', price_kind = 'none', exchange = NULL, market = NULL,
                market_type = NULL, fixed_price = NULL
            WHERE pool_id = 'base-test' AND position = 0
            """
        )
        connection.execute(
            """
            UPDATE pool_assets
            SET symbol = 'WETH', price_kind = 'binance', exchange = 'binance',
                market = 'ETHUSDT', market_type = 'spot', fixed_price = NULL
            WHERE pool_id = 'base-test' AND position = 1
            """
        )
        connection.execute(
            "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES (?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?)",
            [
                "base-test", "0x1", 101, now - timedelta(minutes=2), 100.0, -1.0,
                "base-test", "0x2", 102, now - timedelta(minutes=1), -40.0, 0.6,
            ],
        )
        for tx_hash, timestamp, price in (
            ("0x1", now - timedelta(minutes=2), 2_000.0),
            ("0x2", now - timedelta(minutes=1), 2_200.0),
        ):
            connection.execute(
                "INSERT INTO reference_prices VALUES (?, ?, 1, 1, 'binance', 'ETHUSDT', 'spot', ?, ?, ?, ?, 10, ?)",
                ["base-test", tx_hash, timestamp, tx_hash, timestamp, price, now],
            )
        connection.execute(
            "INSERT INTO balance_snapshots VALUES ('base-test', 0, 102, ?, 1000.0), ('base-test', 1, 102, ?, 10.0)",
            [now, now],
        )

    db.write(seed)
    db.connection.execute("UPDATE swaps SET tx_index = 0")
    db.connection.execute("UPDATE pool_assets SET quote_verified = TRUE, quote_asset = 'USDT' WHERE price_kind = 'binance'")
    payload = dashboard_payload(db, "base-test", 24)

    assert payload["metrics"] == {
        "ordering_pending_count": 0, "thin_market_count": 0,
        "flow_complete": False, "mtm_complete": True, "volume_complete": True,
        "swap_count": 2,
        "priced_swap_count": 0,
        "price_coverage_pct": 0.0,
        "mtm_priced_swap_count": 2,
        "mtm_coverage_pct": 100.0,
        "volume_usd": pytest.approx(3_320.0),
        "flow_pnl_usd": None,
        "mtm_pnl_usd": pytest.approx(1_100.0),
        "mtm_as_of": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "mtm_mark": "pool_implied",
    }
    assert [point["flow"] for point in payload["pnl_series"]] == [None, None]
    assert payload["pnl_series"][0]["price0"] == pytest.approx(20.0)
    assert payload["pnl_series"][1]["price0"] == pytest.approx(33.0)
    assert payload["pnl_series"][1]["mtm"] == pytest.approx(1_100.0)
    assert payload["swap_page"]["items"][0]["asset0_price"] is None
    assert payload["swap_page"]["items"][0]["asset1_price"] == pytest.approx(2_200.0)
    assert payload["swap_page"]["items"][0]["flow_pnl"] is None
    assert payload["swap_page"]["items"][0]["edge_bps"] is None
    assert payload["swap_page"]["items"][0]["price_lag_ms"] == 10

    summary = pool_summaries(db, db.list_pools())["pools"]["base-test"]
    assert summary["volume_usd"] == pytest.approx(3_320.0)
    assert summary["flow_pnl_usd"] is None
    assert summary["mtm_pnl_usd"] == pytest.approx(1_100.0)
    assert summary["tvl_usd"] == pytest.approx(55_000.0)
    assert summary["mtm_apr_pct"] == pytest.approx(730.0)

    db.write(lambda connection: connection.execute(
        "UPDATE swaps SET timestamp = timestamp - INTERVAL 2 HOUR WHERE pool_id = 'base-test'"
    ))
    empty_window = pool_summaries(db, db.list_pools(), 1)["pools"]["base-test"]
    assert empty_window["volume_usd"] is None
    assert empty_window["mtm_pnl_usd"] is None
    assert empty_window["tvl_usd"] == pytest.approx(55_000.0)
    db.close()


def test_dashboard_missing_pool_and_window_bounds(tmp_path):
    db = _configured_database(tmp_path)
    with pytest.raises(KeyError):
        dashboard_payload(db, "missing")
    db.connection.execute("UPDATE swaps SET tx_index = 0")
    db.connection.execute("UPDATE pool_assets SET quote_verified = TRUE, quote_asset = 'USDT' WHERE price_kind = 'binance'")
    payload = dashboard_payload(db, "base-test", 10_000)
    assert payload["window_hours"] == 720.0
    db.close()


def test_counterparties_group_by_chosen_side_and_count_unique_signers(tmp_path):
    db = _configured_database(tmp_path)
    now = datetime.now(timezone.utc)
    router, alice_out, bob_out = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
    # Three swaps through one router: two wallets sign, and the pool pays out to two recipients.
    swaps = [("0x1", router, alice_out, "0xalice"), ("0x2", router, alice_out, "0xalice"), ("0x3", router, bob_out, "0xbob")]
    db.write(lambda c: [c.execute(
        "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta, tx_index, sender, recipient, signer) "
        "VALUES ('base-test', ?, 101, ?, 1.0, -100.0, 0, ?, ?, ?)", [tx, now - timedelta(minutes=1), sender, recipient, signer])
        for tx, sender, recipient, signer in swaps])

    by_recipient = dashboard_payload(db, "base-test", 24)["counterparties"]
    assert by_recipient["side"] == "recipient" and by_recipient["total_counterparties"] == 2
    assert {row["address"]: (row["swap_count"], row["unique_signers"]) for row in by_recipient["rows"]} == {
        alice_out: (2, 1), bob_out: (1, 1)}

    by_sender = dashboard_payload(db, "base-test", 24, counterparty_side="sender")["counterparties"]
    assert [(row["address"], row["swap_count"], row["unique_signers"]) for row in by_sender["rows"]] == [(router, 3, 2)]

    with pytest.raises(ValueError, match="counterparty_side"):
        dashboard_payload(db, "base-test", 24, counterparty_side="signer; DROP TABLE swaps")
