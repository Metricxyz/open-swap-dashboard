from conftest import POOL, configured_database
from dashboard.analytics import dashboard_payload, pool_summaries
from dashboard.database import upsert_rows
from test_trustworthiness import commit, swap_logs, price_swaps
import pytest


def test_summary_captures_only_latest_marks_and_skips_chart_inputs(tmp_path):
    db = configured_database(tmp_path)
    commit(db, [log for index in range(40) for log in swap_logs(f'0x{index:04x}', index)])
    price_swaps(db)
    db.connection.execute("DELETE FROM reference_prices WHERE NOT ((tx_hash = '0x0005' AND asset_position = 0) OR (tx_hash = '0x000a' AND asset_position = 1))")
    actual = pool_summaries(db, db.list_pools(), 168)['pools'][POOL['id']]
    expected = dashboard_payload(db, POOL['id'], 168)['metrics']
    for key in ('volume_usd', 'flow_pnl_usd', 'mtm_pnl_usd', 'flow_complete', 'mtm_complete', 'volume_complete'):
        assert actual[key] == expected[key]
    db.close()


def test_bulk_upserts_remain_atomic_and_replace_existing_rows(tmp_path):
    from datetime import datetime, timezone
    db = configured_database(tmp_path)
    now = datetime.now(timezone.utc)
    row = [1, 101, '0xold', now]
    db.write(lambda c: upsert_rows(c, 'blocks', [row]))
    with pytest.raises(RuntimeError, match='rollback'):
        def failed(c):
            upsert_rows(c, 'blocks', [[1, 101, '0xnew', now], [1, 102, '0xsecond', now]])
            raise RuntimeError('rollback')
        db.write(failed)
    assert db.rows('SELECT block_number, block_hash FROM blocks') == [{'block_number': 101, 'block_hash': '0xold'}]
    db.write(lambda c: upsert_rows(c, 'blocks', [[1, 101, '0xnew', now], [1, 102, '0xsecond', now]]))
    assert db.rows('SELECT block_hash FROM blocks ORDER BY block_number') == [{'block_hash': '0xnew'}, {'block_hash': '0xsecond'}]
    db.close()


def test_pricing_releases_lock_between_commits_and_respects_pause(monkeypatch, tmp_path):
    import threading
    from conftest import ASSETS
    from dashboard import prices
    db = configured_database(tmp_path, assets=[dict(a, price_kind='fixed', fixed_price=1) for a in ASSETS])
    commit(db, [log for index in range(4) for log in swap_logs(f'0x{index}', index)])
    monkeypatch.setattr(prices, 'MAX_PRICE_OBSERVATIONS_PER_COMMIT', 2)
    observed = []

    def report(phase, *_args, **_kwargs):
        if phase != 'progress':
            return
        def reader():
            observed.append(db.row('SELECT count(*) AS n FROM reference_prices')['n'])
            db.set_paused(POOL['id'], True)
        thread = threading.Thread(target=reader)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive(), 'progress must release the database lock for readers'

    assert not prices.enrich_missing_prices(db, db.get_pool(POOL['id']), tmp_path, report=report)
    assert observed == [2]
    assert db.row('SELECT count(*) AS n FROM reference_prices')['n'] == 2
    db.set_paused(POOL['id'], False)
    assert prices.enrich_missing_prices(db, db.get_pool(POOL['id']), tmp_path)
    assert db.row('SELECT count(*) AS n FROM reference_prices')['n'] == 8
    db.close()
