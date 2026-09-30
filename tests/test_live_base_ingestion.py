import os

import pytest

from dashboard import ingestion
from dashboard.database import Database
from dashboard.rpc import RpcClient, RpcStatus


LIVE_BLOCK = 45_353_459
POOL = "0xa6929e903c42a79394f09365f59e916cb0accfd9"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TX_HASH = "0x6b442aba085132a7dd929634b6ece7de19fb6c1311dab981df967dc41b0361b6"


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_INTEGRATION") != "1" or not os.getenv("ENVIO_API_TOKEN"),
    reason="set RUN_LIVE_INTEGRATION=1 and ENVIO_API_TOKEN",
)
def test_real_base_weth_usdc_ingestion_isolated_from_application_data(monkeypatch, tmp_path):
    database_path = tmp_path / "live-base.duckdb"
    db = Database(database_path)
    db.set_envio_settings(api_key=os.environ["ENVIO_API_TOKEN"], rpm_limit=30)
    db.create_pool(
        {
            "id": "live-base-weth-usdc",
            "name": "Main app Base WETH/USDC fixture",
            "chain_key": "base",
            "address": POOL,
            "start_block": LIVE_BLOCK,
            "contract_type": "two_asset_transfers",
        },
        [
            {"symbol": "WETH", "address": WETH, "decimals": 18, "price_kind": "none"},
            {"symbol": "USDC", "address": USDC, "decimals": 6, "price_kind": "none"},
        ],
    )

    rpc_url = ingestion.effective_rpc_urls(db, "base")[-1]
    client = RpcClient(rpc_url, timeout=30)
    fixed_status = RpcStatus(
        rpc_url, rpc_url, 8453, LIVE_BLOCK + 20
    )
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args, **_kwargs: (client, fixed_status))

    ingestion.sync_pool(db, db.sync_candidates()[0])

    assert database_path.is_relative_to(tmp_path)
    assert not any("configs" in part for part in database_path.parts)
    assert db.row("SELECT count(*) AS count FROM raw_logs")["count"] == 2
    assert db.row("SELECT count(*) AS count FROM pool_logs")["count"] == 2
    swap = db.row("SELECT * FROM swaps")
    assert swap["tx_hash"] == TX_HASH
    assert swap["block_number"] == LIVE_BLOCK
    assert swap["asset0_delta"] == pytest.approx(-0.002231701850677261)
    assert swap["asset1_delta"] == pytest.approx(5.006456)
    # The transaction's signer, via HyperSync's transaction join (checked with `cast tx ... from`).
    assert swap["signer"] == "0xac3cedb35e1ed7e3b31fc32ae02df388315b3194"
    snapshots = db.rows("SELECT * FROM balance_snapshots ORDER BY asset_position")
    assert len(snapshots) == 2
    assert all(row["block_number"] == LIVE_BLOCK and row["balance"] >= 0 for row in snapshots)
    state = db.row("SELECT * FROM sync_state")
    assert state["status"] == "caught_up"
    assert state["next_block"] == LIVE_BLOCK + 1
    assert state["active_rpc"] == rpc_url
    db.close()
    assert database_path.exists()
