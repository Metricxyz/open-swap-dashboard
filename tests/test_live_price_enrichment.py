import os
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ASSETS, POOL, configured_database
from dashboard import prices


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_LIVE_INTEGRATION") != "1", reason="set RUN_LIVE_INTEGRATION=1")
def test_real_binance_enrichment_isolated_from_application_data(tmp_path):
    assets = [
        {
            **ASSETS[0],
            "price_kind": "binance",
            "exchange": "binance",
            "market": "ETHUSDC",
            "market_type": "spot",
        },
        {**ASSETS[1], "price_kind": "fixed", "fixed_price": 1.0},
    ]
    db = configured_database(tmp_path, assets=assets)
    target = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    db.write(lambda connection: connection.execute(
        "INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta) VALUES (?, '0xlive-price', 101, ?, 1, -1)",
        [POOL["id"], target],
    ))

    assert prices.enrich_missing_prices(
        db, db.get_pool(POOL["id"]), tmp_path / "market-cache", limit=1
    )

    rows = db.rows("SELECT * FROM reference_prices ORDER BY asset_position")
    assert len(rows) == 2
    assert rows[0]["provider"] == "binance"
    assert rows[0]["market"] == "ETHUSDC"
    assert rows[0]["price"] > 0
    assert rows[0]["source_timestamp"] <= rows[0]["target_timestamp"]
    assert 0 <= rows[0]["lag_ms"] <= prices.MAX_TRADE_LAG_SECONDS * 1000
    assert rows[1]["provider"] == "fixed" and rows[1]["price"] == 1.0
    assert db.path.is_relative_to(tmp_path)
    db.close()
