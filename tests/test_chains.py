from dashboard.chains import CHAINS, public_chain_rows


def test_chain_registry_is_complete_and_redacts_custom_rpc_credentials():
    rows = public_chain_rows({"base": "https://user:secret@example.test/key?token=secret"})

    assert {row["key"] for row in rows} == {
        "ethereum", "base", "arbitrum", "robinhood", "bsc", "polygon", "avalanche"
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
