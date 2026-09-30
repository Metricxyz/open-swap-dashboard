import asyncio
import logging
import threading
import traceback
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import ASSETS, POOL, configured_database
from dashboard import ingestion, prices, scheduler
from dashboard.analytics import dashboard_payload, pool_summaries
from dashboard.database import Database
from dashboard.repairs import repair_pool
from dashboard.rpc import RpcClient, RpcError
from dashboard.server import AssetInput, _normalize_assets, create_app
from test_ingestion_pipeline import transfer_log, install_sync_clients, SyncClient


PEER = '0x00000000000000000000000000000000000000bb'


def swap_logs(tx='0xz', tx_index=0, block=101):
    return [dict(transfer_log(token=a['address'], sender=PEER if i == 0 else POOL['address'],
                             recipient=POOL['address'] if i == 0 else PEER,
                             amount=(1 if i == 0 else 100) * 10 ** a['decimals'],
                             index=tx_index * 2 + i, tx=tx, block=block), transactionIndex=hex(tx_index))
            for i, a in enumerate(ASSETS)]


def commit(db, logs, timestamp=None, start=101, end=101):
    pool = db.get_pool(POOL['id'])
    stamp = timestamp or int(datetime.now(timezone.utc).timestamp()) - 30
    return ingestion._commit_range(db, pool, pool['assets'], logs,
        [{'number': end, 'hash': '0xblock', 'timestamp': stamp}], start, end, end, 'https://rpc.test')


def price_swaps(db):
    pool = db.get_pool(POOL['id'])
    for swap in db.rows('SELECT * FROM swaps'):
        for position, price in ((0, 110), (1, 1)):
            db.connection.execute('INSERT OR REPLACE INTO reference_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                prices._price_row(pool, swap['tx_hash'], position, 'fixed', None, None,
                                  swap['timestamp'], None, swap['timestamp'], price))


def test_self_transfers_are_zero_without_losing_source(tmp_path):
    db = configured_database(tmp_path)
    log = transfer_log(token=ASSETS[0]['address'], sender=POOL['address'], recipient=POOL['address'], amount=10**18)
    commit(db, [log])
    assert db.row('SELECT signed_raw, signed_amount FROM pool_logs') == {'signed_raw': '0', 'signed_amount': 0}
    assert len(db.rows('SELECT * FROM raw_logs')) == 1
    assert not db.rows('SELECT * FROM swaps')
    db.close()


@pytest.mark.parametrize('change', ['replacement', 'removed', 'timestamp', 'amount'])
def test_replay_reconciles_observations_and_matches_rebuild(tmp_path, change):
    db = configured_database(tmp_path)
    logs = swap_logs()
    stamp = int(datetime.now(timezone.utc).timestamp()) - 60
    commit(db, logs, stamp)
    price_swaps(db)
    commit(db, logs, stamp)
    assert len(db.rows('SELECT * FROM reference_prices')) == 2
    if change == 'replacement':
        logs = swap_logs('0xreplacement')
    elif change == 'removed':
        logs = logs[:1]
    elif change == 'timestamp':
        stamp += 1
    else:
        logs[0]['data'] = hex(2 * 10**18)
    commit(db, logs, stamp)
    assert not db.rows('SELECT * FROM reference_prices')
    expected = db.rows('SELECT * FROM swaps ORDER BY block_number, tx_index')
    class Anchor(SyncClient):
        def block_headers(self, numbers):
            return [{'number': 101, 'hash': '0xblock', 'timestamp': stamp}]
    pool = db.get_pool(POOL['id'])
    ingestion.rebuild_derived(db, pool, pool['assets'], Anchor(), 101)
    assert db.rows('SELECT * FROM swaps ORDER BY block_number, tx_index') == expected
    db.close()


def test_advancing_head_replays_previous_tail_and_preserves_cursor(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    db.connection.execute('UPDATE sync_state SET next_block=1001, finalized_block=1000')
    _, envio, _ = install_sync_clients(monkeypatch, db, [], latest=1030)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert envio.calls[0][2] < 1001
    assert envio.calls[0][3] == 1010
    assert db.row('SELECT next_block FROM sync_state')['next_block'] == 1001
    db.close()


def test_incarnation_blocks_delete_recreate_and_paused_pricing(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    old = db.get_pool(POOL['id'])
    db.delete_pool(POOL['id'])
    db.create_pool(POOL, ASSETS)
    new = db.get_pool(POOL['id'])
    assert new['incarnation'] != old['incarnation']
    assert not db.write_if_current(old['id'], old['work_generation'], lambda c: pytest.fail('stale write'), incarnation=old['incarnation'])
    monkeypatch.setattr(prices, '_trades_for_day', lambda *_a, **_k: pytest.fail('pricing must not start'))
    assert not prices.enrich_missing_prices(db, old, tmp_path)
    db.set_paused(POOL['id'], True)
    assert not prices.enrich_missing_prices(db, new, tmp_path)
    db.close()


def test_same_block_swaps_use_transaction_order_not_hash(tmp_path):
    db = configured_database(tmp_path)
    commit(db, swap_logs('0xz', 0) + swap_logs('0xa', 1))
    price_swaps(db)
    payload = dashboard_payload(db, POOL['id'])
    assert [(s['tx_hash'], s['tx_index']) for s in payload['swap_page']['items']] == [('0xa', 1), ('0xz', 0)]
    db.close()


def test_partial_aggregates_and_unverified_quotes_are_not_trusted(tmp_path):
    db = configured_database(tmp_path)
    commit(db, swap_logs('0xz', 0) + swap_logs('0xa', 1))
    price_swaps(db)
    db.connection.execute("DELETE FROM reference_prices WHERE tx_hash='0xa'")
    result = pool_summaries(db, db.list_pools())
    assert result['total']['flow_pnl_usd'] == 10
    assert result['total']['flow_complete'] is False
    assert result['total']['flow_apr_pct'] is None
    assert result['total']['volume_complete'] is False
    db.write(lambda c: c.execute('UPDATE pool_assets SET quote_verified=FALSE'))
    assert dashboard_payload(db, POOL['id'])['metrics']['flow_pnl_usd'] is None
    db.write(lambda c: c.execute('DELETE FROM reference_prices'))
    assert pool_summaries(db, db.list_pools())['total']['volume_usd'] is None
    db.close()


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), 0, -1])
def test_nonfinite_prices_rejected(value):
    assets = [AssetInput(address=a['address'], price_kind='fixed', fixed_price=value) for a in ASSETS]
    with pytest.raises(ValueError, match='positive'):
        _normalize_assets(assets, ASSETS)
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match='finite and positive'):
        prices._price_row(dict(POOL, config_revision=1), 'tx', 0, 'fixed', None, None, now, None, now, value)
    assert prices._select_preceding_trades([prices.Trade('invalid', now, value)], [now]) == [None]


@pytest.mark.parametrize('quote', ['BTC', 'ETH', 'USD', None])
def test_non_usd_proxy_quotes_rejected(monkeypatch, quote):
    from test_prices_io import ExchangeClient, JsonResponse
    client = ExchangeClient([JsonResponse({'symbols': [{'symbol': 'ETHBTC', 'quoteAsset': quote}]})])
    monkeypatch.setattr(prices.httpx, 'Client', lambda **_kwargs: client)
    with pytest.raises(ValueError, match='USDT or USDC'):
        prices.validate_market('binance', 'ETHBTC', 'spot')


def test_provider_cooldown_covers_other_assets_and_new_work(monkeypatch):
    prices._PROVIDER_DEADLINES.clear()
    request = httpx.Request('GET', 'https://api.binance.com/api/v3/aggTrades')
    class Client:
        def get(self, *_args, **_kwargs):
            return httpx.Response(429, headers={'Retry-After': '90'}, request=request)
    now = datetime.now(timezone.utc)
    result = prices._recent_binance_lookup(Client(), 'https://api.binance.com/api/v3', 'ETHUSDT', [now], request_budget=2)
    assert result.requests == 1
    monkeypatch.setattr(prices.httpx, 'Client', lambda **_k: pytest.fail('cooldown must cover new market'))
    other = prices._recent_trade_lookup('binance', 'BTCUSDC', 'futures', [now + timedelta(seconds=1)], request_budget=2)
    assert other.requests == 0 and other.retry_after == result.retry_after
    prices._PROVIDER_DEADLINES.clear()


@pytest.mark.parametrize('operation', ['call', 'status', 'block_headers'])
def test_actual_httpx_error_redacts_formatted_exception_chain(operation):
    secret_url = 'https://alice:password@rpc.test/pathsecret?key=querysecret'
    client = RpcClient(secret_url)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, request=req)))
    with pytest.raises(RpcError) as caught:
        if operation == 'call':
            client.call('eth_chainId')
        elif operation == 'status':
            client.status()
        else:
            client.block_headers([1])
    formatted = ''.join(traceback.format_exception(caught.value))
    for secret in ('alice', 'password', 'pathsecret', 'querysecret'):
        assert secret not in formatted
    client.close()


def test_status_responds_while_database_write_is_held(tmp_path):
    app = create_app(database_path=tmp_path / 'http.duckdb', start_scheduler=False)
    db, worker = app.state.db, app.state.scheduler
    db.create_pool(POOL, ASSETS)
    worker._refresh_status()
    locked, release = threading.Event(), threading.Event()
    def hold(c):
        c.execute('UPDATE pools SET name=name')
        locked.set()
        assert release.wait(3)
    thread = threading.Thread(target=lambda: db.write(hold))
    thread.start()
    assert locked.wait(2)
    async def query():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            response = await asyncio.wait_for(client.get('/api/status'), timeout=0.5)
            assert response.status_code == 200
            assert response.json()['pools'][POOL['id']]['ingestion']['state'] == 'queued'
    try:
        asyncio.run(query())
    finally:
        release.set()
        thread.join(2)
        db.close()


def test_status_clock_waits_failure_and_recovery(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    worker = scheduler.Scheduler(db, tmp_path)
    worker._refresh_status()
    old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    worker._pools[POOL['id']]['ingestion'].update(state='caught_up', next_attempt_at=old)
    assert worker.status()['pools'][POOL['id']]['ingestion']['state'] == 'queued'
    worker._pools[POOL['id']]['ingestion']['started_at'] = old
    worker.report('syncing', 'Parsing transfers', POOL['id'])
    assert worker.status()['pools'][POOL['id']]['ingestion']['taking_longer_than_expected']
    worker.report('waiting', 'Waiting for RPC', POOL['id'])
    assert not worker.status()['pools'][POOL['id']]['ingestion']['taking_longer_than_expected']
    worker.report('progress', 'Committed a page', POOL['id'])
    assert worker.status()['pools'][POOL['id']]['ingestion']['elapsed_seconds'] < 2
    monkeypatch.setattr(worker, '_run', lambda: (_ for _ in ()).throw(RuntimeError('worker crash')))
    worker._guarded_run()
    assert 'Indexer stopped' in db.row('SELECT error FROM stage_status')['error']
    restarted = scheduler.Scheduler(db, tmp_path)
    restarted._refresh_status()
    assert restarted.status()['pools'][POOL['id']]['ingestion']['state'] == 'error'
    restarted._finish_stage(db.get_pool(POOL['id']))
    restarted._refresh_status()
    assert restarted.status()['pools'][POOL['id']]['ingestion']['error'] is None
    assert restarted.status()['pools'][POOL['id']]['ingestion']['last_success_at']
    db.close()


def test_pause_between_ingestion_and_pricing(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    worker = scheduler.Scheduler(db, tmp_path)
    def sync(*_args, **_kwargs):
        db.set_paused(POOL['id'], True)
        worker._stop.set()
    monkeypatch.setattr(scheduler, 'sync_pool', sync)
    monkeypatch.setattr(scheduler, 'enrich_missing_prices', lambda *_a, **_k: pytest.fail('paused pool priced'))
    worker._run()
    assert worker.status()['pools'][POOL['id']]['pricing']['state'] == 'paused'
    db.close()


def test_genuine_v6_schema_migration_repairs_and_restarts(monkeypatch, tmp_path):
    from pathlib import Path
    import duckdb
    path = tmp_path / 'prior.duckdb'
    old = duckdb.connect(str(path))
    old.execute((Path(__file__).parent / 'fixtures/schema_v6.sql').read_text())
    old.execute("INSERT INTO pools VALUES ('base-test', 'Prior pool', 'base', ?, 100, 'two_asset_transfers', FALSE, 1, now(), now())", [POOL['address']])
    old.execute("INSERT INTO sync_state VALUES ('base-test', 102, 121, 101, 'caught_up', 1, NULL, NULL, FALSE, 1, now(), now())")
    for i, asset in enumerate(ASSETS):
        old.execute('INSERT INTO pool_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    [POOL['id'], i, asset['symbol'], asset['address'], asset['decimals'], asset['price_kind'],
                     asset.get('exchange'), asset.get('market'), asset.get('market_type'), asset.get('fixed_price')])
    old.execute("INSERT INTO blocks VALUES (8453, 101, '0xblock', now())")
    for i, amount in enumerate((1, -100)):
        old.execute("INSERT INTO pool_logs VALUES ('base-test', 8453, '0xold', ?, 101, ?, ?, ?, ?, ?)",
                    [i, i, str(amount), amount, PEER, POOL['address']])
        old.execute("INSERT INTO raw_logs VALUES (8453, '0xold', ?, 101, '0xblock', 9, ?, ?, NULL, NULL, NULL, '0x0')",
                    [i, ASSETS[i]['address'], '0xtopic'])
    old.execute("INSERT INTO swaps VALUES ('base-test', '0xold', 101, now(), 1, -100, NULL)")
    old.close()
    db = Database(path)
    pool = db.get_pool(POOL['id'])
    assert pool['incarnation'] and pool['name'] == 'Prior pool'
    assert len(db.rows('SELECT * FROM raw_logs')) == 2
    assert not pool['assets'][0]['quote_verified']
    assert repair_pool(db, pool)
    assert not repair_pool(db, pool)
    assert db.row('SELECT tx_index FROM swaps')['tx_index'] == 9
    # Repair completion is persisted only when the sync has rebuilt history.
    _, _, _ = install_sync_clients(monkeypatch, db, [dict(from_block=100, to_block=101, logs=swap_logs(), headers=[dict(number=101, hash='0xblock', timestamp=1767268800)])])
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert not db.rows('SELECT * FROM repairs')
    incarnation = pool['incarnation']
    db.close()
    restarted = Database(path)
    assert restarted.get_pool(POOL['id'])['incarnation'] == incarnation
    assert not restarted.rows('SELECT * FROM repairs')
    restarted.close()


def test_rpc_failure_redaction_through_asgi(monkeypatch, tmp_path):
    from dashboard import server
    app = create_app(database_path=tmp_path / 'redaction.duckdb', start_scheduler=False)
    def connect(*_args):
        rpc = RpcClient('https://user:password@rpc.test/pathsecret?key=querysecret')
        rpc._client.close()
        rpc._client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, request=req)))
        try:
            rpc.status()
        finally:
            rpc.close()
    monkeypatch.setattr(server, 'connect_rpc', connect)
    async def query():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            response = await client.post('/api/rpc/test', json={'chain_key': 'base'}, headers={'X-CSRF-Token': app.state.csrf_token})
            assert response.status_code == 400
            assert all(secret not in response.text for secret in ('password', 'pathsecret', 'querysecret'))
    asyncio.run(query())
    app.state.db.close()


def test_missing_source_repair_schedules_reingestion(tmp_path):
    db = configured_database(tmp_path)
    commit(db, swap_logs())
    db.connection.execute('DELETE FROM raw_logs')
    db.connection.execute("INSERT INTO repairs VALUES (?, 'Repair metadata')", [POOL['id']])
    assert repair_pool(db, db.get_pool(POOL['id']))
    assert db.row('SELECT next_block FROM sync_state')['next_block'] == POOL['start_block']
    assert 'reingestion' in db.row('SELECT reason FROM repairs')['reason']
    assert not repair_pool(db, db.get_pool(POOL['id']))
    db.close()


def test_pricing_backlog_refreshes_during_ingestion(tmp_path):
    db = configured_database(tmp_path)
    worker = scheduler.Scheduler(db, tmp_path)
    worker._refresh_status()
    assert worker.status()['pools'][POOL['id']]['pricing']['pending_prices'] == 0
    worker.report('waiting', 'Waiting for Envio response', POOL['id'])
    assert worker.status()['pools'][POOL['id']]['pricing']['waiting_for_ingestion']
    commit(db, swap_logs())
    worker.report('progress', 'Committed page', POOL['id'], indexed_block=101)
    stage = worker.status()['pools'][POOL['id']]['pricing']
    assert stage['pending_prices'] == 2 and stage['price_targets'] == 2
    assert stage['waiting_for_ingestion']
    worker._refresh_status()
    assert not worker.status()['pools'][POOL['id']]['pricing']['waiting_for_ingestion']
    assert worker.status()['pools'][POOL['id']]['pricing']['state'] == 'queued'
    db.close()


def test_summary_prices_use_equality_joins_and_preserve_quote_validation(monkeypatch, tmp_path):
    from dashboard.analytics import pool_summaries
    db = configured_database(tmp_path)
    commit(db, swap_logs())
    price_swaps(db)
    plans = []
    original = db.rows
    def capture(sql, params=None):
        if 'WITH priced AS' in sql:
            plans.append(db.connection.execute('EXPLAIN ' + sql, params or []).fetchall()[0][1])
        return original(sql, params)
    monkeypatch.setattr(db, 'rows', capture)
    summary = pool_summaries(db, db.list_pools(), 24)['pools'][POOL['id']]
    assert summary['flow_pnl_usd'] == 10
    db.write(lambda c: c.execute('UPDATE pool_assets SET quote_verified = FALSE'))
    summary = pool_summaries(db, db.list_pools(), 24)['pools'][POOL['id']]
    assert summary['flow_pnl_usd'] is None
    assert plans and all('BLOCKWISE_NL_JOIN' not in plan for plan in plans)
    db.close()
