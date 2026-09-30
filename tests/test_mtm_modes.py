from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from conftest import ASSETS, POOL
from dashboard import analytics, server
from dashboard.database import Database


@pytest.fixture
def history(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    original_window = analytics._window
    monkeypatch.setattr(analytics, '_window', lambda hours, window_end=None, range_start=None, range_end=None:
                        original_window(hours, window_end or now, range_start, range_end))
    start = now - timedelta(hours=12)
    db = Database(tmp_path / 'mtm.duckdb')
    db.create_pool(POOL, ASSETS)
    for block, stamp, d0, d1, price in [
        (100, start-timedelta(minutes=1), -1, 95, 100),
        (101, start+timedelta(hours=1), 1, -90, 110),
        (102, start+timedelta(hours=11), -.5, 70, 120),
    ]:
        db.connection.execute('INSERT INTO swaps VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, NULL)',
                              [POOL['id'], f'0x{block}', block, stamp, d0, d1])
        for pos, p in [(0,price), (1,1)]:
            db.connection.execute("INSERT INTO reference_prices VALUES (?, ?, ?, 1, 'fixed', NULL, NULL, ?, NULL, ?, ?, 0, ?)",
                                  [POOL['id'], f'0x{block}', pos, stamp, stamp, p, now])
    db.connection.execute('INSERT INTO balance_points VALUES (?, 100, ?, 10, 200)', [POOL['id'], start-timedelta(minutes=1)])
    # Includes a deposit of 5 WETH, which must not enter either swap-only MTM.
    db.connection.execute('INSERT INTO balance_points VALUES (?, 102, ?, 15.5, 180)', [POOL['id'], now-timedelta(hours=1)])
    for pos, q in [(0,15.5),(1,180)]:
        db.connection.execute('INSERT INTO balance_snapshots VALUES (?, ?, 102, ?, ?)', [POOL['id'],pos,now,q])
    yield db, start, now
    db.close()


def test_both_modes_apply_to_metrics_charts_zoom_summaries_aprs_and_cache(history):
    db, start, end = history
    pool = db.get_pool(POOL['id'])
    assert pool['mtm_mode'] == 'balanced'
    revision = db.data_revision(POOL['id'])
    for mode, expected, first in [('balanced',120,60), ('as_is',40,20), ('balanced',120,60)]:
        db.update_pool(pool['id'],pool['name'],pool['start_block'],pool['assets'],mtm_mode=mode)
        data = analytics.dashboard_payload(db,pool['id'],12,window_end=end)
        assert data['pool']['mtm_mode'] == mode
        assert data['metrics']['mtm_pnl_usd'] == pytest.approx(expected)
        assert data['metrics']['mtm_complete']
        assert data['metrics']['flow_pnl_usd'] == 30
        assert data['pnl_series'][0]['mtm'] == pytest.approx(first)
        assert data['pnl_series'][-1]['mtm'] == pytest.approx(expected)
        zoom = analytics.dashboard_payload(db,pool['id'],12,window_end=end,
            range_start=start+timedelta(hours=10),range_end=end)
        assert zoom['metrics'] == data['metrics']
        assert zoom['pnl_series'][-1]['mtm'] == pytest.approx(expected)
        summaries = analytics.pool_summaries(db,db.list_pools(),12)
        for summary in [summaries['pools'][pool['id']],summaries['chains']['base'],summaries['total']]:
            assert summary['mtm_pnl_usd'] == pytest.approx(expected)
            assert summary['mtm_apr_pct'] == pytest.approx(round(expected/2040*730*100,2))
        assert db.get_pool(pool['id'])['config_revision'] == pool['config_revision']
        assert db.get_pool(pool['id'])['work_generation'] == pool['work_generation']
        assert db.row('SELECT count(*) AS n FROM reference_prices')['n'] == 6
        assert db.data_revision(pool['id']) != revision


def test_mixed_pool_modes_roll_up_the_selected_results(history):
    db, start, end = history
    db.create_pool({**POOL,'id':'second','mtm_mode':'as_is'},ASSETS)
    for table in ['swaps','reference_prices','balance_points','balance_snapshots']:
        db.connection.execute(f"INSERT INTO {table} SELECT * REPLACE ('second' AS pool_id) FROM {table} WHERE pool_id=?",[POOL['id']])
    result = analytics.pool_summaries(db,db.list_pools(),12)
    assert result['pools'][POOL['id']]['mtm_pnl_usd'] == 120
    assert result['pools']['second']['mtm_pnl_usd'] == 40
    assert result['chains']['base']['mtm_pnl_usd'] == result['total']['mtm_pnl_usd'] == 160


@pytest.mark.parametrize('opening_problem', ['no_balances','no_prices','negative_balance'])
def test_missing_opening_data_does_not_silently_use_as_is(history,opening_problem):
    db, start, end = history
    sql = {
        'no_balances':'DELETE FROM balance_points',
        'no_prices':"DELETE FROM reference_prices WHERE tx_hash='0x100'",
        'negative_balance':'UPDATE balance_points SET asset0_balance=-1',
    }[opening_problem]
    db.write(lambda c:c.execute(sql))
    data = analytics.dashboard_payload(db,POOL['id'],12,window_end=end)
    assert data['metrics']['mtm_pnl_usd'] is None
    assert not data['metrics']['mtm_complete']
    assert not data['metrics']['mtm_baseline_available']
    assert data['metrics']['flow_pnl_usd'] == 30
    assert all(p['mtm'] is None for p in data['pnl_series'])
    summary = analytics.pool_summaries(db,db.list_pools(),12)
    assert summary['pools'][POOL['id']]['mtm_apr_pct'] is None
    assert not summary['total']['mtm_complete']


def test_opening_price_prefers_cex_and_falls_back_to_swap_without_future_marks(history):
    db, start, end = history
    before = analytics.dashboard_payload(db,POOL['id'],12,window_end=end)
    assert before['metrics']['mtm_baseline_mark'] == 'external'
    db.write(lambda c:c.execute("DELETE FROM reference_prices WHERE tx_hash='0x100' AND asset_position=0"))
    data = analytics.dashboard_payload(db,POOL['id'],12,window_end=end)
    # Prior swap implied 95 USD/WETH: V=1150, benchmark=(1150/190,575).
    expected = 40 + (10-1150/190)*120 + (200-575)
    assert data['metrics']['mtm_pnl_usd'] == pytest.approx(round(expected,2))
    assert data['metrics']['mtm_baseline_mark'] == 'pool_implied'
    assert datetime.fromisoformat(data['metrics']['mtm_baseline_as_of']) < start
    assert data['pnl_series'][-1]['mtm'] == pytest.approx(expected)
    assert analytics.pool_summaries(db,db.list_pools(),12)['total']['mtm_pnl_usd'] == pytest.approx(round(expected,2))


def test_balanced_partial_pricing_uses_last_valued_mark_and_stays_partial(history):
    db, _, end = history
    db.write(lambda c:c.execute("DELETE FROM reference_prices WHERE tx_hash='0x102'"))
    data = analytics.dashboard_payload(db,POOL['id'],12,window_end=end)
    assert data['metrics']['mtm_pnl_usd'] == 60
    assert not data['metrics']['mtm_complete']
    assert data['metrics']['mtm_coverage_pct'] == 50
    assert data['pnl_series'][-1]['mtm'] == 60
    summary = analytics.pool_summaries(db,db.list_pools(),12)['pools'][POOL['id']]
    assert summary['mtm_pnl_usd'] == 60
    assert summary['mtm_apr_pct'] is None


def test_first_indexed_block_undoes_all_transfers_before_splitting_inventory(history):
    db, start, end = history
    def update(c):
        c.execute('DELETE FROM swaps WHERE block_number=100')
        c.execute('DELETE FROM balance_points')
        stamp = start+timedelta(hours=1)
        # First block: +1/-90 swap plus +5 WETH deposit. Opening inventory is 10/200.
        c.execute('INSERT INTO balance_points VALUES (?,101,?,16,110)',[POOL['id'],stamp])
        c.execute("INSERT INTO blocks VALUES (8453,101,'0xblock',?)",[stamp])
        for index,pos,amount in [(0,0,1),(1,1,-90),(2,0,5)]:
            c.execute("INSERT INTO pool_logs VALUES (?,8453,?, ?,101,?, '1', ?, 'a','b')",
                      [POOL['id'],f'0xlog{index}',index,pos,amount])
    db.write(update)
    data = analytics.dashboard_payload(db,POOL['id'],12,window_end=end)
    expected = 40+(10-1300/220)*120+(200-650)
    assert data['metrics']['mtm_baseline_available']
    assert data['metrics']['mtm_pnl_usd'] == pytest.approx(round(expected,2))
    assert data['pnl_series'][-1]['mtm'] == pytest.approx(expected)
    assert data['metrics']['mtm_baseline_as_of'] == (start+timedelta(hours=1)).isoformat().replace('+00:00','Z')


def test_pool_created_in_window_opens_empty_and_counts_only_swaps(history):
    db, start, end = history
    def update(c):
        c.execute('DELETE FROM swaps WHERE block_number=100')
        c.execute('DELETE FROM balance_points')
        stamp = start+timedelta(hours=1)
        # Creation block: a 0.1 + 0.2 WETH / 200 USDC deposit plus the +1/-90 swap. Undoing it leaves float dust.
        c.execute('INSERT INTO balance_points VALUES (?,101,?,1.3,110)',[POOL['id'],stamp])
        c.execute("INSERT INTO blocks VALUES (8453,101,'0xblock',?)",[stamp])
        for index,pos,amount in [(0,0,.1),(1,0,.2),(2,1,200),(3,0,1),(4,1,-90)]:
            c.execute("INSERT INTO pool_logs VALUES (?,8453,?, ?,101,?, '1', ?, 'a','b')",
                      [POOL['id'],f'0xlog{index}',index,pos,amount])
    db.write(update)
    data = analytics.dashboard_payload(db,POOL['id'],12,window_end=end)
    # Swaps net +0.5 WETH / -20 USDC, marked at 120.
    assert data['metrics']['mtm_baseline_available']
    assert data['metrics']['mtm_baseline_mark'] == 'empty'
    assert data['metrics']['mtm_pnl_usd'] == pytest.approx(40)
    assert data['metrics']['mtm_complete']
    assert data['pnl_series'][-1]['mtm'] == pytest.approx(40)
    assert analytics.pool_summaries(db,db.list_pools(),12)['pools'][POOL['id']]['mtm_pnl_usd'] == pytest.approx(40)


def test_schema_nine_migrates_existing_pools_to_balanced_once(history,monkeypatch):
    db, _, _ = history
    path = db.path
    before = db.rows('SELECT * FROM reference_prices ORDER BY tx_hash,asset_position')
    db.connection.execute('ALTER TABLE pools DROP COLUMN mtm_mode')
    db.connection.execute('UPDATE schema_meta SET version=9')
    db.close()
    migrated = Database(path)
    assert migrated.get_pool(POOL['id'])['mtm_mode'] == 'balanced'
    assert migrated.rows('SELECT * FROM reference_prices ORDER BY tx_hash,asset_position') == before
    pool = migrated.get_pool(POOL['id'])
    migrated.update_pool(pool['id'],pool['name'],pool['start_block'],pool['assets'],mtm_mode='as_is')
    migrated.close()
    monkeypatch.setattr(Database,'_upgrade',lambda *_:pytest.fail('Repeated migration'))
    reopened = Database(path)
    assert reopened.get_pool(POOL['id'])['mtm_mode'] == 'as_is'
    reopened.close()


def test_mode_api_validation_and_partial_update_preserves_selection(history):
    db, _, _ = history
    assert server.PoolInput(name='p',chain_key='base',address='0x1',assets=[]).mtm_mode == 'balanced'
    with pytest.raises(ValidationError):
        server.PoolUpdateInput(name='p',start_block=100,assets=[],mtm_mode='bad')
    pool=db.get_pool(POOL['id'])
    with pytest.raises(ValueError,match='MTM'):
        db.update_pool(pool['id'],pool['name'],pool['start_block'],pool['assets'],mtm_mode='bad')
    db.update_pool(pool['id'],pool['name'],pool['start_block'],pool['assets'],mtm_mode='as_is')
    db.update_pool(pool['id'],'Renamed',pool['start_block'],pool['assets'])
    assert db.get_pool(pool['id'])['mtm_mode']=='as_is'
