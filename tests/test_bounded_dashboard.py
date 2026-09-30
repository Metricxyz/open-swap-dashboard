from datetime import datetime, timedelta, timezone
import json
import time

import pytest
from fastapi.testclient import TestClient

from benchmark_dashboard import seed
from dashboard.analytics import dashboard_payload, pool_summaries, swap_page
from dashboard.database import Database
from dashboard.server import create_app


@pytest.fixture
def populated(tmp_path):
    db = Database(tmp_path / 'bounded.duckdb')
    seed(db, swaps=5000)
    yield db
    db.close()


def test_dense_charts_preserve_exact_metrics_extrema_and_zoom_baseline(populated):
    db = populated
    db.write(lambda c: c.execute("UPDATE reference_prices SET price = 4000 WHERE asset_position = 0 AND tx_hash = '0x' || lpad(to_hex(2111),64,'0')"))
    data = dashboard_payload(db, 'bench-0', plots=('pnl', 'price', 'balance', 'volume'))
    assert data['metrics']['swap_count'] == data['swap_page']['total'] == 5000
    assert len(data['swap_page']['items']) == 100
    assert all(n['returned'] <= 2000 for n in data['series_counts'].values())
    assert len(json.dumps(data).encode()) < 2 * 1024 * 1024
    assert any(p['tx_hash'].lower().endswith('083f') for p in data['price_series'])
    assert data['pnl_series'][-1]['flow'] == pytest.approx(data['metrics']['flow_pnl_usd'])
    assert data['volume_series'][-1]['volume'] == pytest.approx(data['metrics']['volume_usd'])
    bounds = db.row('SELECT min(timestamp) AS first, max(timestamp) AS last FROM swaps')
    zoom = dashboard_payload(db, 'bench-0', window_end=datetime.fromisoformat(data['window_end']),
                             range_start=bounds['last'] - timedelta(seconds=2), range_end=bounds['last'], plots=('pnl', 'volume'))
    assert zoom['metrics'] == data['metrics']
    assert 1 < zoom['swap_page']['total'] < 100
    assert zoom['volume_series'][0]['volume'] > 1000000
    assert zoom['volume_series'][-1]['volume'] == data['volume_series'][-1]['volume']
    assert zoom['counterparties']['total_swaps'] == zoom['swap_page']['total']
    filtered = dashboard_payload(db, 'bench-0', minimum_swap_usd=100000, plots=('price',))
    assert filtered['price_series'] == []
    assert filtered['reference_series']
    assert filtered['metrics'] == data['metrics']
    assert filtered['counterparties'] == data['counterparties']


def test_pages_are_exact_and_revision_protects_against_changed_history(populated):
    db = populated
    data = dashboard_payload(db, 'bench-0')
    kwargs = dict(window_end=datetime.fromisoformat(data['window_end']), revision=data['data_revision'])
    page = swap_page(db, 'bench-0', 24, page=1, **kwargs)
    first = {r['tx_hash'] for r in data['swap_page']['items']}
    assert len(page['items']) == 100
    assert not first.intersection(r['tx_hash'] for r in page['items'])
    assert swap_page(db, 'bench-0', 24, page=999, **kwargs)['page'] == 49
    assert dashboard_payload(db, 'bench-0', window_end=kwargs['window_end'], swap_page_index=1)['swap_page'] == page
    bounds = db.row('SELECT min(timestamp) AS first, max(timestamp) AS last FROM swaps')
    page = swap_page(db, 'bench-0', 24, range_start=bounds['first'], range_end=bounds['first'] + timedelta(seconds=.36), **kwargs)
    assert page['total'] == 2  # Both boundaries are inclusive.
    db.write(lambda c: c.execute("DELETE FROM swaps WHERE pool_id = 'bench-0' AND block_number = 100"))
    with pytest.raises(ValueError, match='data changed'):
        swap_page(db, 'bench-0', 24, **kwargs)


def test_cache_skips_history_and_status_writes_but_invalidates_domain_writes(populated, monkeypatch):
    db = populated
    data = dashboard_payload(db, 'bench-0')
    captured = []
    original = db.rows
    def capture(sql, params=None):
        captured.append(sql)
        return original(sql, params)
    monkeypatch.setattr(db, 'rows', capture)
    db.write(lambda c: c.execute("UPDATE sync_state SET status = 'caught_up'"), invalidate=False)
    assert dashboard_payload(db, 'bench-0') == data
    assert not any('FROM swaps' in sql or 'dashboard_valued' in sql for sql in captured)
    revision = db.data_revision('bench-0')
    with pytest.raises(RuntimeError):
        def fail(c):
            c.execute('DELETE FROM reference_prices')
            raise RuntimeError('rollback')
        db.write(fail)
    assert db.data_revision('bench-0') == revision
    db.write(lambda c: c.execute('DELETE FROM reference_prices'))
    assert dashboard_payload(db, 'bench-0')['metrics']['priced_swap_count'] == 0
    assert db._analytics_cache_bytes <= 32 * 1024 * 1024


def test_expiring_window_and_future_observations_invalidate_cache(tmp_path):
    db = Database(tmp_path / 'expiry.duckdb')
    seed(db, swaps=1)
    db.write(lambda c: c.execute("UPDATE swaps SET timestamp = now() - INTERVAL 1 HOUR + INTERVAL 100 MILLISECOND"))
    assert dashboard_payload(db, 'bench-0', 1)['metrics']['swap_count'] == 1
    time.sleep(.12)
    assert dashboard_payload(db, 'bench-0', 1)['metrics']['swap_count'] == 0
    db.write(lambda c: c.execute("UPDATE swaps SET timestamp = now() + INTERVAL 100 MILLISECOND"))
    assert dashboard_payload(db, 'bench-0', 1)['metrics']['swap_count'] == 0
    time.sleep(.12)
    assert dashboard_payload(db, 'bench-0', 1)['metrics']['swap_count'] == 1
    db.close()


def test_http_etag_range_validation_and_page_conflict(tmp_path):
    app = create_app(database_path=tmp_path / 'http.duckdb', start_scheduler=False)
    seed(app.state.db, swaps=101)
    with TestClient(app) as client:
        url = '/api/pools/bench-0/dashboard'
        response = client.get(url)
        assert response.status_code == 200
        etag = response.headers['etag']
        data = response.json()
        unchanged = client.get(url, headers={'If-None-Match': etag})
        assert unchanged.status_code == 304 and not unchanged.content
        assert client.get(url, params={'range_start': data['window_start']}).status_code == 422
        assert client.get(url, params={'plots': 'invalid'}).status_code == 422
        assert client.get(url, params={'minimum_swap_usd': 'nan'}).status_code == 422
        args = dict(window_end=data['window_end'], revision=data['data_revision'], page=1)
        assert len(client.get('/api/pools/bench-0/swaps', params=args).json()['items']) == 1
        app.state.db.write(lambda c: c.execute('DELETE FROM reference_prices'))
        assert client.get('/api/pools/bench-0/swaps', params=args).status_code == 409
        assert client.get(url, headers={'If-None-Match': etag}).status_code == 200


def test_counterparty_totals_are_not_limited_to_first_page_or_chart_sample(populated):
    data = dashboard_payload(populated, 'bench-0')
    summary = data['counterparties']
    assert summary['total_counterparties'] == 12
    assert summary['total_swaps'] == 5000
    assert len(summary['rows']) == 10
    first = summary['rows'][0]
    assert first['address'].endswith('0001')
    assert first['swap_count'] == 417
    assert first['volume_usd'] == 417 * 2001
    assert first['swap_share_pct'] == pytest.approx(100 * 417 / 5000)
    assert first['avg_edge_bps'] == pytest.approx(5)


def test_pool_specific_commits_preserve_other_pools_cache(populated):
    db = populated
    from conftest import POOL, ASSETS
    db.create_pool({**POOL, 'id': 'another'}, ASSETS)
    data = dashboard_payload(db, 'another')
    pool = db.get_pool('bench-0')
    db.write_if_current('bench-0', pool['work_generation'], lambda c: c.execute("DELETE FROM reference_prices WHERE pool_id = 'bench-0'"), incarnation=pool['incarnation'])
    assert dashboard_payload(db, 'another') == data
    assert any(key[1] == ('another',) for key in db._analytics_cache)
    assert not any(key[1] == ('bench-0',) for key in db._analytics_cache)


def test_balance_reconstruction_uses_retained_checkpoint_after_restart(tmp_path, monkeypatch):
    from conftest import configured_database, POOL, ASSETS
    from dashboard.ingestion import _commit_range, rebuild_derived
    from test_batch_turns import page
    db = configured_database(tmp_path)
    pool = db.get_pool(POOL['id'])

    class Rpc:
        def block_headers(self, numbers):
            return [{'number': n, 'timestamp': 1767268800 + n} for n in numbers]
        def token_balance(self, token, _owner, block):
            return (block - 90) * 10**18 if token == ASSETS[0]['address'] else (1100 - block) * 10**6

    for block in (100, 101):
        p = page(block)
        _commit_range(db, pool, pool['assets'], p['logs'], p['headers'], block, block, 101, 'fixture')
    rebuild_derived(db, pool, pool['assets'], Rpc(), 101)
    prefix = db.rows('SELECT * FROM balance_points ORDER BY block_number')
    p = page(102)
    _commit_range(db, pool, pool['assets'], p['logs'], p['headers'], 102, 102, 102, 'fixture')
    # The anchored snapshot survives the replay so TVL stays available until the next anchor.
    assert {r['block_number'] for r in db.rows('SELECT * FROM balance_snapshots')} == {101}
    path = db.path
    db.close()
    db = Database(path)
    pool = db.get_pool(POOL['id'])
    bounds = []
    original = db.write_if_current

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql, params=None):
            if 'CREATE OR REPLACE TEMP TABLE desired_balance_points' in sql:
                bounds.append((params[1], params[2]))
            return self.connection.execute(sql, params or [])

    def observe(pool_id, generation, operation, **kwargs):
        return original(pool_id, generation, lambda c: operation(ObservedConnection(c)), **kwargs)
    monkeypatch.setattr(db, 'write_if_current', observe)
    rebuild_derived(db, pool, pool['assets'], Rpc(), 102, rebuild_swaps=False)
    assert bounds == [(101, 102)]
    incremental = db.rows('SELECT * FROM balance_points ORDER BY block_number')
    assert incremental[:2] == prefix
    assert incremental[-1]['asset0_balance'] == 12
    rebuild_derived(db, pool, pool['assets'], Rpc(), 102)
    assert db.rows('SELECT * FROM balance_points ORDER BY block_number') == incremental
    db.close()


def test_sampling_caps_duplicate_order_keys_without_losing_extrema(tmp_path):
    from dashboard.analytics import _sample
    db = Database(tmp_path / 'ties.duckdb')
    rows, count = _sample(db, 'SELECT now() AS timestamp, 0 AS ordering, i AS value FROM range(5000) t(i)',
                          'timestamp, value', [('value', 'min'), ('value', 'max')])
    assert count == 5000
    assert len(rows) <= 2000
    assert {0, 4999} <= {r['value'] for r in rows}
    db.close()


def test_latest_tvl_marks_resolve_sparse_historical_prices(populated):
    db = populated
    db.write(lambda c: c.execute("DELETE FROM reference_prices WHERE NOT (asset_position = 0 AND tx_hash = '0x' || lpad(to_hex(0),64,'0'))"))
    db.write(lambda c: c.execute("INSERT INTO balance_snapshots VALUES ('bench-0', 0, 6000, now(), 10), ('bench-0', 1, 6000, now(), 1000)"))
    summary = pool_summaries(db, db.list_pools())['pools']['bench-0']
    assert summary['tvl_usd'] == 21000
    assert summary['flow_complete'] is False
    latest = db.row("SELECT max(timestamp) AS t FROM swaps")["t"]
    assert summary["last_swap_at"] == latest.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
