import threading
from datetime import datetime, timezone

import pytest

from conftest import ASSETS, POOL, configured_database
from dashboard import ingestion
from dashboard.database import Database
from dashboard.envio import EnvioClient
from dashboard.rpc import RpcError, RpcStatus, TRANSFER_TOPIC


def topic(address):
    return "0x" + address.lower()[2:].rjust(64, "0")


def transfer_log(*, token, sender, recipient, amount, tx="0xtx", index=0, block=101, block_hash="0xblock"):
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, topic(sender), topic(recipient)],
        "data": hex(amount),
        "transactionHash": tx,
        "logIndex": hex(index),
        "blockNumber": hex(block),
        "blockHash": block_hash,
        "transactionIndex": "0x0",
    }


class ValidationClient:
    def __init__(self, *, missing=None):
        self.missing = missing
        self.closed = False

    def code(self, address):
        return "0x" if address == self.missing else "0x6000"

    def token_metadata(self, address):
        return ("WETH", 18) if address.endswith("01") else ("USDC", 6)

    def close(self):
        self.closed = True


class DiscoveryClient(ValidationClient):
    def __init__(self, logs, *, invalid_token=None):
        super().__init__()
        self.logs = logs
        self.invalid_token = invalid_token
        self.ranges = []

    def pool_token_addresses(self, _pool_address):
        return None

    def token_metadata(self, address):
        if address == self.invalid_token:
            raise RpcError("not ERC-20")
        return super().token_metadata(address)


def test_effective_rpc_urls_and_pool_validation(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    assert ingestion.effective_rpc_urls(db, "base")[0] == "https://mainnet.base.org"
    db.set_rpc_override("base", "https://private.test/key")
    assert ingestion.effective_rpc_urls(db, "base") == ["https://private.test/key"]

    client = ValidationClient()
    status = RpcStatus("https://rpc.test", "https://rpc.test", 8453, 200_000)
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args: (client, status))
    result = ingestion.validate_pool_input(
        db,
        chain_key="base",
        pool_address=POOL["address"].upper().replace("0X", "0x"),
        token_addresses=[asset["address"] for asset in ASSETS],
        start_block=None,
    )
    assert result["pool_address"] == POOL["address"]
    assert result["assets"][0] == {"address": ASSETS[0]["address"], "symbol": "WETH", "decimals": 18}
    assert result["start_block"] == 70_380
    assert client.closed

    with pytest.raises(ValueError, match="Unsupported chain"):
        ingestion.validate_pool_input(db, chain_key="nope", pool_address=POOL["address"], token_addresses=[], start_block=0)
    with pytest.raises(ValueError, match="Exactly two"):
        ingestion.validate_pool_input(db, chain_key="base", pool_address=POOL["address"], token_addresses=[ASSETS[0]["address"]], start_block=0)
    with pytest.raises(ValueError, match="must be different"):
        ingestion.validate_pool_input(db, chain_key="base", pool_address=POOL["address"], token_addresses=[ASSETS[0]["address"]] * 2, start_block=0)

    missing = ValidationClient(missing=POOL["address"])
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args: (missing, status))
    with pytest.raises(ValueError, match="Pool address has no contract code"):
        ingestion.validate_pool_input(db, chain_key="base", pool_address=POOL["address"], token_addresses=[asset["address"] for asset in ASSETS], start_block=100)
    assert missing.closed

    token_missing = ValidationClient(missing=ASSETS[1]["address"])
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args: (token_missing, status))
    with pytest.raises(ValueError, match="Token address"):
        ingestion.validate_pool_input(db, chain_key="base", pool_address=POOL["address"], token_addresses=[asset["address"] for asset in ASSETS], start_block=100)

    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args: (ValidationClient(), status))
    with pytest.raises(ValueError, match="between 0 and confirmed"):
        ingestion.validate_pool_input(db, chain_key="base", pool_address=POOL["address"], token_addresses=[asset["address"] for asset in ASSETS], start_block=300_000)
    db.close()


def test_asset_discovery_uses_frequency_metadata_and_remembered_prices(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    pool_address = "0x00000000000000000000000000000000000000aA"
    invalid = "0x0000000000000000000000000000000000000003"
    logs = [
        {"address": invalid, "blockNumber": "0xb4"},
        {"address": invalid, "blockNumber": "0xb3"},
        {"address": ASSETS[0]["address"], "blockNumber": "0xb2"},
        {"address": ASSETS[0]["address"], "blockNumber": "0xb1"},
        {"address": ASSETS[1]["address"], "blockNumber": "0xb0"},
    ]
    client = DiscoveryClient(logs, invalid_token=invalid)
    status = RpcStatus("https://rpc.test", "https://rpc.test", 8453, 200)
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args: (client, status))
    db.set_envio_settings(api_key="test-key", rpm_limit=30)

    class DiscoveryEnvio:
        def __init__(self, *_args):
            self.closed = False

        def transfer_pages(self, chain_id, owner, start, end):
            assert (chain_id, owner, start, end) == (8453, pool_address.lower(), 81, 180)
            yield {"logs": logs}

        def close(self):
            self.closed = True

    monkeypatch.setattr(ingestion, "EnvioClient", DiscoveryEnvio)

    result = ingestion.discover_pool_assets(
        db, chain_key="base", pool_address=pool_address, max_blocks=100
    )

    assert client.ranges == []
    assert result["generated_name"] == "WETH/USDC 0x0000...00aA"
    assert [asset["symbol"] for asset in result["assets"]] == ["WETH", "USDC"]
    assert result["assets"][0]["price_kind"] == "binance"
    assert result["assets"][0]["market"] == "ETHUSDT"
    assert result["assets"][1]["price_kind"] == "fixed"
    assert result["assets"][1]["fixed_price"] == 1.0
    assert client.closed

    with pytest.raises(ValueError, match="Unsupported chain"):
        ingestion.discover_pool_assets(db, chain_key="nope", pool_address=POOL["address"])
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args: (DiscoveryClient([]), status))
    with pytest.raises(ValueError, match="Discovery block"):
        ingestion.discover_pool_assets(db, chain_key="base", pool_address=POOL["address"], end_block=181)

    class EmptyEnvio(DiscoveryEnvio):
        def transfer_pages(self, *_args):
            yield {"logs": []}

    monkeypatch.setattr(ingestion, "EnvioClient", EmptyEnvio)
    with pytest.raises(ValueError, match="Could not detect two"):
        ingestion.discover_pool_assets(db, chain_key="base", pool_address=POOL["address"], max_blocks=1)
    db.close()


def test_asset_discovery_prefers_contract_token_metadata(monkeypatch, tmp_path):
    class DirectDiscoveryClient(DiscoveryClient):
        def pool_token_addresses(self, _pool_address):
            return [ASSETS[0]["address"], ASSETS[1]["address"]]

    db = configured_database(tmp_path)
    client = DirectDiscoveryClient([])
    status = RpcStatus("https://rpc.test", "https://rpc.test", 1, 200)
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args: (client, status))

    result = ingestion.discover_pool_assets(
        db,
        chain_key="ethereum",
        pool_address="0x00000000000000000000000000000000000000aA",
    )

    assert [asset["symbol"] for asset in result["assets"]] == ["WETH", "USDC"]
    assert [asset["transfer_count"] for asset in result["assets"]] == [None, None]
    assert result["assets"][0]["market"] == "ETHUSDT"
    assert result["assets"][1]["fixed_price"] == 1.0
    assert client.ranges == []
    assert client.closed

    db.create_pool(
        {
            **POOL,
            "id": "ethereum-existing",
            "chain_key": "ethereum",
            "address": "0x00000000000000000000000000000000000000bb",
        },
        [
            {**ASSETS[0], "price_kind": "bybit", "exchange": "bybit", "market": "ETHUSDC"},
            {**ASSETS[1], "fixed_price": 0.99},
        ],
    )
    exact = ingestion.discover_pool_assets(
        db,
        chain_key="ethereum",
        pool_address="0x00000000000000000000000000000000000000aA",
    )
    assert exact["assets"][0]["price_kind"] == "binance"
    assert exact["assets"][1]["fixed_price"] == 0.99
    db.close()



def test_asset_discovery_defaults_usd_named_tokens_to_fixed_one_dollar(monkeypatch, tmp_path):
    class DirectDiscoveryClient(DiscoveryClient):
        def pool_token_addresses(self, _pool_address):
            return [ASSETS[0]["address"], ASSETS[1]["address"]]

    db = Database(tmp_path / "fresh.duckdb")
    status = RpcStatus("https://rpc.test", "https://rpc.test", 8453, 200)
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args: (DirectDiscoveryClient([]), status))

    result = ingestion.discover_pool_assets(db, chain_key="base", pool_address="0x00000000000000000000000000000000000000aA")

    assert [(a["symbol"], a["price_kind"], a["fixed_price"]) for a in result["assets"]] == [
        ("WETH", "binance", None), ("USDC", "fixed", 1.0),
    ]
    assert (result["assets"][0]["market"], result["assets"][0]["market_type"]) == ("ETHUSDC", "spot")
    db.close()

def test_commit_range_normalizes_transfers_and_handles_reorg(tmp_path):
    db = configured_database(tmp_path)
    pool = db.get_pool(POOL["id"])
    sender = "0x00000000000000000000000000000000000000bb"
    logs = [
        transfer_log(token=ASSETS[0]["address"], sender=sender, recipient=POOL["address"], amount=10**18, index=0),
        transfer_log(token=ASSETS[1]["address"], sender=POOL["address"], recipient=sender, amount=2_000 * 10**6, index=1),
        {**transfer_log(token=ASSETS[0]["address"], sender=sender, recipient=sender, amount=1, index=2), "topics": [TRANSFER_TOPIC]},
    ]
    ingestion._commit_range(
        db, pool, pool["assets"], logs,
        [{"number": 101, "hash": "0xblock", "timestamp": 1_767_268_800}],
        101, 101, 200, "https://rpc.test",
    )
    deltas = db.rows("SELECT asset_position, signed_amount FROM pool_logs ORDER BY asset_position")
    assert deltas == [{"asset_position": 0, "signed_amount": 1.0}, {"asset_position": 1, "signed_amount": -2000.0}]
    assert len(db.rows("SELECT * FROM raw_logs")) == 2
    assert db.row("SELECT asset0_delta, asset1_delta FROM swaps") == {
        "asset0_delta": 1.0, "asset1_delta": -2000.0
    }
    state = db.row("SELECT status, progress, active_rpc FROM sync_state")
    assert {"status": state["status"], "active_rpc": state["active_rpc"]} == {
        "status": "syncing", "active_rpc": "https://rpc.test"
    }
    assert state["progress"] == pytest.approx(2 / 101)

    ingestion._commit_range(
        db, pool, pool["assets"], [],
        [{"number": 101, "hash": "0xreorg", "timestamp": 1_767_268_801}],
        101, 101, 200, "https://rpc.test",
    )
    assert db.rows("SELECT * FROM pool_logs") == []
    assert db.rows("SELECT * FROM swaps") == []
    assert db.row("SELECT block_hash FROM blocks")["block_hash"] == "0xreorg"
    db.close()


def test_rebuild_uses_the_incremental_swap_rule(tmp_path):
    db = configured_database(tmp_path)
    pool = db.get_pool(POOL["id"])
    sender = "0x00000000000000000000000000000000000000bb"
    logs = [
        transfer_log(token=ASSETS[0]["address"], sender=sender, recipient=POOL["address"], amount=10**18, tx="0xswap", index=0),
        transfer_log(token=ASSETS[0]["address"], sender=sender, recipient=POOL["address"], amount=2 * 10**18, tx="0xswap", index=1),
        transfer_log(token=ASSETS[1]["address"], sender=POOL["address"], recipient=sender, amount=3 * 10**6, tx="0xswap", index=2),
        transfer_log(token=ASSETS[0]["address"], sender=sender, recipient=POOL["address"], amount=4 * 10**18, tx="0xdeposit", index=3),
        transfer_log(token=ASSETS[1]["address"], sender=sender, recipient=POOL["address"], amount=4 * 10**6, tx="0xdeposit", index=4),
        transfer_log(token=ASSETS[0]["address"], sender=sender, recipient=POOL["address"], amount=5 * 10**18, tx="0xone-sided", index=5),
    ]
    header = {"number": 101, "hash": "0xblock", "timestamp": 1_767_268_800}
    ingestion._commit_range(
        db, pool, pool["assets"], logs, [header], 101, 101, 101, "https://rpc.test"
    )
    incremental = db.rows(
        "SELECT tx_hash, asset0_delta, asset1_delta FROM swaps ORDER BY tx_hash"
    )

    class RebuildClient:
        def block_headers(self, _numbers):
            return [header]

        def token_balance(self, *_args):
            return 0

    ingestion.rebuild_derived(db, pool, pool["assets"], RebuildClient(), 101)

    assert incremental == [{"tx_hash": "0xswap", "asset0_delta": 3.0, "asset1_delta": -3.0}]
    assert db.rows(
        "SELECT tx_hash, asset0_delta, asset1_delta FROM swaps ORDER BY tx_hash"
    ) == incremental
    db.close()


class SyncClient:
    url = "https://rpc.test"
    def __init__(self):
        self.closed = False

    def block_headers(self, numbers):
        return [
            {"number": number, "hash": f"0x{number:x}", "timestamp": 1_767_268_800 + number}
            for number in sorted(set(numbers))
        ]

    def token_balance(self, token, _owner, _block):
        return 10 * 10**18 if token == ASSETS[0]["address"] else 1_000 * 10**6

    def close(self):
        self.closed = True


class FakeEnvio:
    def __init__(self, pages, *, started=None, release=None):
        self.pages = pages
        self.started = started
        self.release = release
        self.calls = []
        self.closed = False

    def transfer_pages(self, chain_id, owner, start, end, tokens):
        self.calls.append((chain_id, owner, start, end, tokens))
        if self.started:
            self.started.set()
            assert self.release.wait(2)
        yield from self.pages

    def close(self):
        self.closed = True


def install_sync_clients(monkeypatch, db, pages, *, latest=121, started=None, release=None):
    db.set_envio_settings(api_key="test-key", rpm_limit=30)
    rpc = SyncClient()
    envio = FakeEnvio(pages, started=started, release=release)
    status = RpcStatus("https://rpc.test", "https://rpc.test", 8453, latest)
    monkeypatch.setattr(ingestion, "connect_rpc", lambda *_args, **_kwargs: (rpc, status))
    monkeypatch.setattr(ingestion, "EnvioClient", lambda *_args: envio)
    return rpc, envio, status


@pytest.mark.parametrize("mutation", ["delete", "start_block", "rebuild", "pause"])
def test_sync_pool_discards_work_invalidated_during_envio(monkeypatch, tmp_path, mutation):
    db = configured_database(tmp_path)
    started = threading.Event()
    release = threading.Event()
    sender = "0x00000000000000000000000000000000000000bb"
    log = transfer_log(
        token=ASSETS[0]["address"], sender=sender, recipient=POOL["address"],
        amount=10**18, block=101,
    )
    page = {
        "from_block": 100, "to_block": 101, "logs": [log],
        "headers": [{"number": 101, "hash": "0xblock", "timestamp": 1_767_268_901}],
    }
    rpc, _, _ = install_sync_clients(
        monkeypatch, db, [page], started=started, release=release
    )
    worker = threading.Thread(target=ingestion.sync_pool, args=(db, db.sync_candidates()[0]))
    worker.start()
    assert started.wait(2)

    if mutation == "delete":
        db.delete_pool(POOL["id"])
    elif mutation == "start_block":
        db.update_pool(POOL["id"], POOL["name"], 90, ASSETS)
    elif mutation == "rebuild":
        db.request_rebuild(POOL["id"])
    else:
        db.set_paused(POOL["id"], True)
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert db.rows("SELECT * FROM pool_logs") == []
    if mutation == "delete":
        assert db.get_pool(POOL["id"]) is None
    else:
        state = db.row("SELECT * FROM sync_state")
        assert state["work_generation"] == 2
        if mutation == "start_block":
            assert state["next_block"] == 90 and state["status"] == "queued"
        elif mutation == "rebuild":
            assert state["rebuild_requested"] is True and state["status"] == "queued"
        else:
            assert state["status"] == "paused"
    assert rpc.closed
    db.close()


def test_sync_pool_commits_envio_pages_and_advances_cursor(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    sender = "0x00000000000000000000000000000000000000bb"
    logs = [
        transfer_log(token=ASSETS[0]["address"], sender=sender, recipient=POOL["address"], amount=10**18),
        transfer_log(token=ASSETS[1]["address"], sender=POOL["address"], recipient=sender, amount=2_000 * 10**6, index=1),
    ]
    pages = [
        {"from_block": 100, "to_block": 100, "logs": [], "headers": []},
        {"from_block": 101, "to_block": 101, "logs": logs,
         "headers": [{"number": 101, "hash": "0xblock", "timestamp": 1_767_268_901}]},
    ]
    rpc, envio, _ = install_sync_clients(monkeypatch, db, pages)
    activity = []

    ingestion.sync_pool(db, db.sync_candidates()[0], report=lambda *args, **_kwargs: activity.append(args))

    assert envio.calls[0][2:4] == (100, 101)
    assert db.row("SELECT next_block, status, progress FROM sync_state") == {
        "next_block": 102, "status": "caught_up", "progress": 1.0,
    }
    assert db.row("SELECT count(*) AS count FROM swaps")["count"] == 1
    assert len(db.rows("SELECT * FROM balance_snapshots")) == 2
    assert any("ENVIO LOGS" in message for _phase, message, _pool in activity)
    assert rpc.closed and envio.closed
    db.close()


def test_sync_pool_marks_empty_range_and_records_missing_key(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    rpc, _, status = install_sync_clients(
        monkeypatch, db, [{"from_block": 100, "to_block": 101, "logs": [], "headers": []}]
    )
    ingestion.sync_pool(db, db.sync_candidates()[0])
    state = db.row("SELECT * FROM sync_state")
    assert state["status"] == "caught_up" and state["last_success_at"] is not None
    ingestion._mark_caught_up(db, POOL["id"], state["work_generation"], status, 101)
    assert db.row("SELECT status FROM sync_state")["status"] == "caught_up"

    db.clear_envio_key()
    monkeypatch.setattr(ingestion, "EnvioClient", EnvioClient)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    error = db.row("SELECT status, error FROM sync_state")
    assert error["status"] == "error" and "Configure an Envio API key" in error["error"]
    assert rpc.closed
    db.close()


def test_sync_pool_keeps_partial_envio_range_pending(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    install_sync_clients(
        monkeypatch, db, [{"from_block": 100, "to_block": 100, "logs": [], "headers": []}]
    )

    ingestion.sync_pool(db, db.sync_candidates()[0])

    assert db.row("SELECT next_block, status, error FROM sync_state") == {
        "next_block": 101, "status": "waiting", "error": None,
    }
    assert db.rows("SELECT * FROM balance_snapshots") == []
    db.close()


def test_sync_pool_only_rebuilds_all_swaps_when_requested(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    install_sync_clients(
        monkeypatch, db, [{"from_block": 100, "to_block": 101, "logs": [], "headers": []}]
    )
    rebuild_modes = []
    monkeypatch.setattr(
        ingestion,
        "rebuild_derived",
        lambda *_args, **kwargs: rebuild_modes.append(kwargs["rebuild_swaps"]) or True,
    )

    ingestion.sync_pool(db, db.sync_candidates()[0])
    db.request_rebuild(POOL["id"])
    install_sync_clients(
        monkeypatch, db, [{"from_block": 100, "to_block": 101, "logs": [], "headers": []}]
    )
    ingestion.sync_pool(db, db.sync_candidates()[0])

    assert rebuild_modes == [False, True]
    db.close()


def test_sync_records_endpoint_selected_after_rpc_rotation(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    rpc, _, _ = install_sync_clients(monkeypatch, db, [{
        'from_block': 100, 'to_block': 101, 'logs': [], 'headers': [],
    }])
    original = rpc.block_headers
    def rotated(numbers):
        rpc.url = 'https://backup.test'
        return original(numbers)
    monkeypatch.setattr(rpc, 'block_headers', rotated)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert db.row('SELECT active_rpc, status FROM sync_state') == {'active_rpc': 'https://backup.test', 'status': 'caught_up'}
    db.close()


def test_asset_discovery_switches_to_the_chain_that_has_the_contract(monkeypatch, tmp_path):
    class DirectDiscoveryClient(DiscoveryClient):
        def pool_token_addresses(self, _pool_address):
            return [ASSETS[0]["address"], ASSETS[1]["address"]]

    db = configured_database(tmp_path)
    pool_address = "0x00000000000000000000000000000000000000aA"
    connected = []

    def connect(urls, chain_id):
        connected.append(chain_id)
        client = DirectDiscoveryClient([])
        client.missing = None if chain_id == 8453 else pool_address.lower()
        return client, RpcStatus("https://rpc.test", "https://rpc.test", chain_id, 200)

    probed = []

    class ProbeClient:
        def __init__(self, urls, *, timeout, expected_chain_id):
            self.chain_id = expected_chain_id

        def code(self, _address):
            probed.append(self.chain_id)
            if self.chain_id == 42161:
                raise RpcError("endpoint down")
            return "0x6000" if self.chain_id == 8453 else "0x"

        def close(self):
            pass

    monkeypatch.setattr(ingestion, "connect_rpc", connect)
    monkeypatch.setattr(ingestion, "RpcClient", ProbeClient)

    result = ingestion.discover_pool_assets(db, chain_key="ethereum", pool_address=pool_address)

    assert result["chain_key"] == "base"
    assert result["generated_name"] == "WETH/USDC 0x0000...00aA"
    assert connected == [1, 8453]
    assert 1 not in probed and 42161 in probed

    monkeypatch.setattr(ProbeClient, "code", lambda self, _address: "0x")
    with pytest.raises(ValueError, match="no contract code on Ethereum or any other supported chain"):
        ingestion.discover_pool_assets(db, chain_key="ethereum", pool_address=pool_address)
    db.close()
