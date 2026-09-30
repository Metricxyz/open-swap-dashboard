from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from conftest import ASSETS, POOL, configured_database
from dashboard.database import Database
from dashboard import ingestion, prices, scheduler
from test_trustworthiness import swap_logs, commit


def test_commit_counts_deduplicated_swaps_and_distinguishes_stale(tmp_path):
    db = configured_database(tmp_path)
    logs = swap_logs()
    assert commit(db, logs + logs) == (1, 1)
    assert commit(db, []) == (0, 0)
    pool = db.get_pool(POOL['id'])
    db.set_paused(pool['id'], True)
    assert ingestion._commit_range(db, pool, pool['assets'], [], [], 101, 101, 101, 'fixture') is None
    db.close()


@pytest.mark.parametrize('prior', [False, True])
def test_schema_failure_rolls_back_and_restart_recovers(monkeypatch, tmp_path, prior):
    path = tmp_path / 'schema.duckdb'
    original_connect = duckdb.connect
    connection = original_connect(str(path))
    if prior:
        connection.execute((Path(__file__).parent / 'fixtures/schema_v6.sql').read_text())
    original_tables = connection.execute('SHOW TABLES').fetchall()
    failure = 'ALTER TABLE pools' if prior else 'CREATE TABLE IF NOT EXISTS balance_points'

    class Interrupted:
        def execute(self, sql, *args, **kwargs):
            if failure in sql:
                raise RuntimeError('injected schema failure')
            return connection.execute(sql, *args, **kwargs)

        def close(self):
            connection.close()

    with monkeypatch.context() as patch:
        patch.setattr('dashboard.database.duckdb.connect', lambda *_a, **_k: Interrupted())
        with pytest.raises(RuntimeError, match='injected'):
            Database(path)
    connection = original_connect(str(path))
    assert connection.execute('SHOW TABLES').fetchall() == original_tables
    if prior:
        assert connection.execute('SELECT version FROM schema_meta').fetchone() == (6,)
        assert 'incarnation' not in {r[1] for r in connection.execute('DESCRIBE pools').fetchall()}
    connection.close()
    db = Database(path)
    db.create_pool(POOL, ASSETS)
    assert db.get_pool(POOL['id'])['incarnation']
    db.close()


def test_incomplete_v8_initialization_is_repaired_once(tmp_path):
    path = tmp_path / 'incomplete.duckdb'
    old = duckdb.connect(str(path))
    old.execute((Path(__file__).parent / 'fixtures/schema_v6.sql').read_text())
    old.execute('UPDATE schema_meta SET version=8')
    old.close()
    db = Database(path)
    db.create_pool(POOL, ASSETS)
    incarnation = db.get_pool(POOL['id'])['incarnation']
    assert db.sync_candidates()[0]['scan_next_block'] is None
    db.close()
    db = Database(path)
    assert db.get_pool(POOL['id'])['incarnation'] == incarnation
    assert not db.rows('SELECT * FROM repairs')
    db.close()


def test_future_schema_rejected_without_mutation(tmp_path):
    path = tmp_path / 'future.duckdb'
    old = duckdb.connect(str(path))
    old.execute('CREATE TABLE schema_meta(version INTEGER); INSERT INTO schema_meta VALUES (999)')
    old.close()
    with pytest.raises(RuntimeError, match='Unsupported'):
        Database(path)
    old = duckdb.connect(str(path))
    assert old.execute('SHOW TABLES').fetchall() == [('schema_meta',)]
    assert old.execute('SELECT version FROM schema_meta').fetchone() == (999,)
    old.close()


@pytest.mark.parametrize('mutation', ['pause', 'delete', 'replace', 'failure'])
def test_pool_repair_races_do_not_stop_other_pools(monkeypatch, tmp_path, mutation):
    db = configured_database(tmp_path)
    db.create_pool({**POOL, 'id': 'second'}, ASSETS)
    worker = scheduler.Scheduler(db, tmp_path)
    synced = []

    def repair(database, pool):
        if pool['id'] != POOL['id']:
            return False
        if mutation == 'failure':
            raise ValueError('repair failed for this pool')
        if mutation == 'pause':
            database.set_paused(pool['id'], True)
        else:
            database.delete_pool(pool['id'])
            if mutation == 'replace':
                database.create_pool(POOL, ASSETS)
        return True

    def sync(database, pool, **kwargs):
        synced.append(pool['id'])
        worker._stop.set()

    monkeypatch.setattr(scheduler, 'repair_pool', repair)
    monkeypatch.setattr(scheduler, 'sync_pool', sync)
    worker._guarded_run()
    assert synced == ['second']
    assert worker._fatal_error is None
    if mutation == 'failure':
        assert db.row('SELECT error FROM stage_status WHERE pool_id = ?', [POOL['id']])['error'] == 'repair failed for this pool'
    db.close()


def test_asset_retry_eligibility_matches_status_and_execution(monkeypatch, tmp_path):
    db = configured_database(tmp_path)
    commit(db, swap_logs())
    db.connection.execute("INSERT INTO price_enrichment_attempts VALUES (?, '0xz', 0, 1, now(), now() + INTERVAL 6 HOUR)", [POOL['id']])
    monkeypatch.setattr(prices, '_trades_for_day', lambda *_a, **_kw: pytest.fail('Deferred exchange leg must not run'))
    worker = scheduler.Scheduler(db, tmp_path)
    worker._refresh_status()
    assert worker.status()['pools'][POOL['id']]['pricing']['state'] == 'queued'
    assert prices.enrich_missing_prices(db, db.get_pool(POOL['id']), tmp_path)
    assert db.rows('SELECT asset_position FROM reference_prices') == [{'asset_position': 1}]
    worker._refresh_status()
    stage = worker.status()['pools'][POOL['id']]['pricing']
    assert stage['state'] == 'waiting' and stage['pending_prices'] == 1
    assert not prices.enrich_missing_prices(db, db.get_pool(POOL['id']), tmp_path)
    db.connection.execute('UPDATE price_enrichment_attempts SET retry_after = now() - INTERVAL 1 SECOND')
    monkeypatch.setattr(prices, '_trades_for_day', lambda *_a, **_kw: None)
    monkeypatch.setattr(prices, '_recent_trade_lookup', lambda _e, _m, _k, targets, **_kw:
                        prices.LookupResult([prices.Trade('price', t, 2000) for t in targets], frozenset(range(len(targets)))))
    assert prices.enrich_missing_prices(db, db.get_pool(POOL['id']), tmp_path)
    assert db.row('SELECT count(*) AS n FROM reference_prices')['n'] == 2
    assert not db.rows('SELECT * FROM price_enrichment_attempts')
    db.close()


def test_recent_lookup_checkpoint_keeps_unattempted_targets_eligible():
    from test_prices_io import ExchangeClient, JsonResponse
    target = datetime.now(timezone.utc).replace(microsecond=0)
    response = JsonResponse([{'a': i, 'T': int(target.timestamp() * 1000), 'p': '2000'} for i in range(1000)])
    client = ExchangeClient([response])
    progress = []

    def checkpoint(message):
        progress.append(message)
        return len(progress) == 1

    result = prices._recent_binance_lookup(client, 'https://fixture', 'ETHUSDT',
        [target, target - timedelta(hours=1)], request_budget=50, checkpoint=checkpoint)
    assert len(client.calls) == 1
    assert result.attempted == frozenset({0}) and result.trades[1] is None
    assert len(progress) == 2 and 'request 2/50' in progress[-1]


@pytest.mark.parametrize('reason', ['pause', 'shutdown', 'deadline'])
def test_recent_enrichment_stops_between_requests(monkeypatch, tmp_path, reason):
    import threading
    from test_prices_io import ExchangeClient, JsonResponse
    db = configured_database(tmp_path)
    # Pricing runs per UTC day; midday keeps both swaps in one day whatever the clock says.
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    for i in range(2):
        commit(db, swap_logs(f'0x{i}', block=101+i), int((now - timedelta(hours=i)).timestamp()), start=101+i, end=101+i)
    stop = threading.Event()
    clock = [0]
    monkeypatch.setattr(prices.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(prices, '_trades_for_day', lambda *_a, **_k: None)

    class Client(ExchangeClient):
        def get(self, *args, **kwargs):
            result = super().get(*args, **kwargs)
            if reason == 'pause':
                db.set_paused(POOL['id'], True)
            elif reason == 'shutdown':
                stop.set()
            else:
                clock[0] = 31
            return result

    client = Client([JsonResponse([{'a': i, 'T': int(now.timestamp() * 1000), 'p': '2000'} for i in range(1000)])])
    monkeypatch.setattr(prices.httpx, 'Client', lambda **_k: client)
    progress = []
    prices.enrich_missing_prices(db, db.get_pool(POOL['id']), tmp_path, stop=stop, report=lambda *args, **_k: progress.append(args))
    assert len(client.calls) == 1
    assert any('recent trade request' in event[1] for event in progress)
    assert not db.rows('SELECT * FROM price_enrichment_attempts')
    # A budget yield commits completed fixed legs and the resolved exchange leg;
    # pause/shutdown discard this in-flight batch using the existing safeguards.
    assert db.row('SELECT count(*) AS n FROM reference_prices')['n'] == (3 if reason == 'deadline' else 0)
    db.close()


def test_archive_scan_reports_progress_and_does_not_finalize_partial_scan(tmp_path):
    import zipfile
    path = tmp_path / 'large.zip'
    target = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('trades.csv', '1,2000,1,1,1,1767225600000,true\n' * 20001)
    reports = []

    def checkpoint(message):
        reports.append(message)
        return len(reports) < 3

    with pytest.raises(InterruptedError):
        prices._select_preceding_trades(prices._iter_binance_zip(path, checkpoint=checkpoint), [target])
    assert len(reports) == 3 and '20,000 rows' in reports[-1]


@pytest.mark.parametrize('phase', ['download', 'scan'])
@pytest.mark.parametrize('outcome', ['complete', 'pause', 'shutdown', 'reconfigure'])
def test_archive_finishes_past_budget_but_remains_cancellable(monkeypatch, tmp_path, phase, outcome):
    import io
    import threading
    import zipfile
    from contextlib import contextmanager
    from types import SimpleNamespace

    db = configured_database(tmp_path)
    target = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    commit(db, swap_logs(), int(target.timestamp()))
    commit(db, swap_logs('0xnext', block=102), int((target + timedelta(days=1)).timestamp()), start=102, end=102)
    pool = db.get_pool(POOL['id'])
    stop = threading.Event()
    clock = [0]
    parsed = [0]
    requests = []
    progress = []
    monkeypatch.setattr(prices.time, 'monotonic', lambda: clock[0])

    def cross_deadline():
        clock[0] = 31
        if outcome == 'pause':
            db.set_paused(pool['id'], True)
        elif outcome == 'shutdown':
            stop.set()
        elif outcome == 'reconfigure':
            db.update_pool(pool['id'], pool['name'], pool['start_block'],
                           [pool['assets'][0], {**pool['assets'][1], 'fixed_price': 2}])

    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, 'w') as archive:
        archive.writestr('trades.csv', '1,2000,1,1,1,1767268800000,true\n' * 20001)
    payload = archive_bytes.getvalue()

    def chunks():
        yield payload[:len(payload) // 2]
        if phase == 'download':
            cross_deadline()
        yield payload[len(payload) // 2:]

    @contextmanager
    def stream(_method, url, **_kwargs):
        requests.append(url)
        yield SimpleNamespace(status_code=200, iter_bytes=chunks)

    parse = prices._timestamp_from_number

    def parse_row(value):
        parsed[0] += 1
        if phase == 'scan' and parsed[0] == 10000:
            cross_deadline()
        return parse(value)

    monkeypatch.setattr(prices.httpx, 'stream', stream)
    monkeypatch.setattr(prices, '_timestamp_from_number', parse_row)
    prices.enrich_missing_prices(db, pool, tmp_path, stop=stop,
                                report=lambda *args, **_kwargs: progress.append(args[1]))
    observations = db.rows('SELECT tx_hash, asset_position, price FROM reference_prices ORDER BY tx_hash, asset_position')
    if outcome == 'complete':
        assert observations == [
            {'tx_hash': '0xnext', 'asset_position': 1, 'price': 1.0},
            {'tx_hash': '0xz', 'asset_position': 0, 'price': 2000.0},
            {'tx_hash': '0xz', 'asset_position': 1, 'price': 1.0},
        ]
        assert parsed[0] == 20001
        assert any('20,000 rows' in message for message in progress)
    else:
        assert observations == []
        assert parsed[0] == (0 if phase == 'download' else 10000)
    assert len(requests) == 1  # The next market day waits for another turn.
    assert not db.rows('SELECT * FROM price_enrichment_attempts')
    assert not list(tmp_path.rglob('*.part'))
    db.close()


def test_legacy_bybit_upgrade_edit_and_replacement_preserve_history(monkeypatch, tmp_path):
    from dashboard import server
    from dashboard.analytics import dashboard_payload
    from test_server import endpoint
    from test_trustworthiness import price_swaps
    legacy = [{**ASSETS[0], 'price_kind': 'bybit', 'exchange': 'bybit'}, ASSETS[1]]
    db = configured_database(tmp_path, assets=legacy)
    commit(db, swap_logs())
    price_swaps(db)
    observations = db.rows('SELECT * FROM reference_prices ORDER BY asset_position')
    db.connection.execute('UPDATE schema_meta SET version=8')
    db.close()
    app = server.create_app(database_path=tmp_path / 'dashboard.duckdb', start_scheduler=False)
    db = app.state.db
    pool = db.get_pool(POOL['id'])
    assert pool['assets'][0]['price_kind'] == 'bybit'
    assert not pool['assets'][0]['quote_verified']
    assert 'retired' in pool['assets'][0]['quote_error']
    assert db.rows('SELECT * FROM reference_prices ORDER BY asset_position') == observations
    assert ASSETS[0]['address'] not in db.price_preferences('base')
    assert db.latest_cross_chain_price_preference('ethereum', 'WETH', 18) is None
    assert dashboard_payload(db, POOL['id'])['metrics']['flow_pnl_usd'] is None
    monkeypatch.setattr(server, 'validate_pool_input', lambda *_a, **_k:
                        dict(assets=[{k: a[k] for k in ('address', 'symbol', 'decimals')} for a in ASSETS]))
    monkeypatch.setattr(server, 'validate_market', lambda *_a: pytest.fail('A name-only edit must not contact Bybit'))
    update = endpoint(app, '/api/pools/{pool_id}', 'PUT')
    inputs = [server.AssetInput(address=a['address'], price_kind=a['price_kind'], market=a.get('market'),
                               market_type=a.get('market_type') or 'spot', fixed_price=a.get('fixed_price')) for a in legacy]
    assert update(POOL['id'], server.PoolUpdateInput(name='Renamed', start_block=100, assets=inputs))['ok']
    assert db.get_pool(POOL['id'])['name'] == 'Renamed'
    assert db.rows('SELECT * FROM reference_prices ORDER BY asset_position') == observations
    for kind in ('fixed', 'none', 'binance'):
        monkeypatch.setattr(server, 'validate_market', lambda *_a: 'USDT')
        inputs[0] = server.AssetInput(address=ASSETS[0]['address'], price_kind=kind, fixed_price=2000, market='ETHUSDT')
        assert update(POOL['id'], server.PoolUpdateInput(name='Renamed', start_block=100, assets=inputs))['ok']
        asset = db.get_pool(POOL['id'])['assets'][0]
        assert asset['quote_error'] is None and asset['price_kind'] == kind
        assert not db.rows('SELECT * FROM reference_prices')
        assert db.row('SELECT count(*) AS n FROM raw_logs')['n'] == 2
        assert db.row('SELECT count(*) AS n FROM swaps')['n'] == 1
    db.close()


def test_bad_envio_page_leaves_committed_cursor_and_source_unchanged(monkeypatch, tmp_path):
    from dashboard import envio
    from test_envio import response_page, HttpClient, Response
    from test_ingestion_pipeline import install_sync_clients
    db = configured_database(tmp_path)
    commit(db, swap_logs())
    source = db.rows('SELECT * FROM raw_logs ORDER BY log_index')
    cursor = db.row('SELECT next_block, scan_next_block FROM sync_state')
    install_sync_clients(monkeypatch, db, [])
    page = response_page()
    del page['data']['logs'][0]['transaction_index']
    monkeypatch.setattr(ingestion, 'EnvioClient', envio.EnvioClient)
    monkeypatch.setattr(envio.httpx, 'Client', lambda **_k: HttpClient([Response(page)]))
    monkeypatch.setattr(envio._RATE_LIMITER, 'acquire', lambda *_a, **_k: None)
    ingestion.sync_pool(db, db.sync_candidates()[0])
    assert db.row('SELECT next_block, scan_next_block FROM sync_state') == cursor
    assert db.rows('SELECT * FROM raw_logs ORDER BY log_index') == source
    assert 'invalid log' in db.row('SELECT error FROM sync_state')['error']
    db.close()


@pytest.mark.parametrize('outcome', ['verified', 'invalid', 'cooldown'])
def test_legacy_quote_verification_persists_only_terminal_results(monkeypatch, tmp_path, outcome):
    from dashboard import repairs
    db = configured_database(tmp_path)
    db.connection.execute('UPDATE pool_assets SET quote_verified=FALSE WHERE position=0')
    calls = []

    def validate(*args):
        calls.append(args)
        if outcome != 'verified':
            raise ValueError('temporary cooldown' if outcome == 'cooldown' else 'unsupported quote')
        return 'USDT'

    monkeypatch.setattr(repairs, 'validate_market', validate)
    monkeypatch.setattr(repairs, 'provider_deadline', lambda _exchange:
                        datetime.now(timezone.utc) + timedelta(seconds=60) if outcome == 'cooldown' else None)
    if outcome == 'verified':
        repairs.repair_price_configuration(db, db.get_pool(POOL['id']), lambda *_a: None)
        repairs.repair_price_configuration(db, db.get_pool(POOL['id']), lambda *_a: None)
        assert len(calls) == 1
        assert db.get_pool(POOL['id'])['assets'][0]['quote_verified']
    else:
        with pytest.raises(ValueError):
            repairs.repair_price_configuration(db, db.get_pool(POOL['id']), lambda *_a: None)
        asset = db.get_pool(POOL['id'])['assets'][0]
        assert not asset['quote_verified']
        assert asset['quote_error'] == (None if outcome == 'cooldown' else 'unsupported quote')
    db.close()
