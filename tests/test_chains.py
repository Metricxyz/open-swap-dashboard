import os

import pytest

from dashboard.chains import CHAINS, public_chain_rows
from dashboard.rpc import connect_rpc


def test_chain_registry_is_complete_and_redacts_custom_rpc_credentials():
    rows = public_chain_rows({"base": "https://user:secret@example.test/key?token=secret"})

    assert {row["key"] for row in rows} == {
        "ethereum", "base", "arbitrum", "robinhood", "bsc", "polygon", "avalanche",
        "hyperevm", "monad", "megaeth",
    }
    base = next(row for row in rows if row["key"] == "base")
    assert base["chain_id"] == 8453
    assert base["public_rpcs"] == [
        "https://mainnet.base.org",
        "https://base.gateway.tenderly.co",
    ]
    assert base["explorer_address_url"] == "https://basescan.org/address/{address}"
    assert base["has_custom_rpc"] is True
    assert base["custom_rpc_display"] == "https://example.test/…"
    robinhood = next(row for row in rows if row["key"] == "robinhood")
    assert robinhood["chain_id"] == 4663
    assert robinhood["native_symbol"] == "ETH"
    assert robinhood["public_rpcs"] == ["https://rpc.mainnet.chain.robinhood.com"]
    assert all(row["confirmations"] > 0 and row["overlap_blocks"] > 0 for row in rows)
    assert public_chain_rows()[0]["has_custom_rpc"] is False
    assert len(CHAINS) == len(rows)


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_LIVE_INTEGRATION") != "1", reason="set RUN_LIVE_INTEGRATION=1")
def test_robinhood_seconds_per_block_matches_chain():
    # seconds_per_block turns the default three-day backfill and the seven-day
    # discovery search into block counts, so it has to match the chain.
    chain = CHAINS["robinhood"]
    client, status = connect_rpc(chain.public_rpcs, chain.chain_id)
    try:
        start, end = client.block_headers([status.latest_block - 100_000, status.latest_block])
    finally:
        client.close()
    measured = (end["timestamp"] - start["timestamp"]) / (end["number"] - start["number"])
    assert chain.seconds_per_block == pytest.approx(measured, rel=0.25)
