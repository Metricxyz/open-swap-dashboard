"""Isolated benchmark: uv run python tests/benchmark_dashboard.py --pools 10 --swaps 100000."""
import argparse
import json
import math
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import uvicorn
from conftest import POOL, ASSETS, browser_plotly
from dashboard.server import create_app


def seed(db, pools=1, swaps=10000, retained=0):
    for i in range(pools):
        pool_id = f'bench-{i}'
        db.create_pool({**POOL, 'id': pool_id}, ASSETS)
        db.write(lambda c: c.execute("""
            INSERT INTO swaps (pool_id, tx_hash, block_number, timestamp, asset0_delta, asset1_delta, tx_index, counterparty, recipient)
            SELECT ?, '0x' || lpad(to_hex(i), 64, '0'), i + 100,
                CASE WHEN i < ? THEN now() - INTERVAL 40 DAY ELSE now() - INTERVAL 1 HOUR END
                    + ((i - ?)::DOUBLE / ?) * INTERVAL '30 minute',
                CASE WHEN i % 2 = 0 THEN 1.0 ELSE -1.0 END,
                CASE WHEN i % 2 = 0 THEN -1999.0 ELSE 2001.0 END, 0,
                '0x' || lpad(to_hex(i % 12), 40, '0'), '0x' || lpad(to_hex(i % 12), 40, '0')
            FROM range(?) t(i)
        """, [pool_id, retained, retained, swaps, swaps + retained]))
        db.write(lambda c: c.execute("""
            INSERT INTO reference_prices SELECT s.pool_id, s.tx_hash, a.position, 1, 'fixed', NULL, NULL,
                s.timestamp, NULL, s.timestamp, CASE WHEN a.position = 0 THEN 2000.0 ELSE 1.0 END, 0, now()
            FROM swaps s JOIN pool_assets a ON a.pool_id = s.pool_id WHERE s.pool_id = ?
        """, [pool_id]))
        db.write(lambda c: c.execute("""
            INSERT INTO balance_points SELECT pool_id, block_number, timestamp, 100.0, 200000.0 FROM swaps WHERE pool_id = ?
        """, [pool_id]))
        print(f'Seeded pool {i + 1}/{pools}: {swaps:,} window swaps, {retained:,} older swaps', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pools', type=int, default=10)
    parser.add_argument('--swaps', type=int, default=100000)
    parser.add_argument('--retained', type=int, default=0)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--threads', type=int)
    parser.add_argument('--memory-limit')
    args = parser.parse_args()
    if min(args.pools, args.swaps, args.runs) < 1 or args.retained < 0:
        parser.error("pools, swaps, and runs must be positive; retained must be nonnegative")
    with tempfile.TemporaryDirectory(prefix='dashboard-benchmark-') as folder:
        app = create_app(database_path=Path(folder) / 'bench.duckdb', start_scheduler=False)
        db = app.state.db
        if args.threads:
            db.connection.execute("SET threads = ?", [args.threads])
        if args.memory_limit:
            db.connection.execute("SET memory_limit = ?", [args.memory_limit])
        seed(db, args.pools, args.swaps, args.retained)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError('Benchmark server stopped during startup')
            time.sleep(0.01)
        base = f'http://127.0.0.1:{port}'
        try:
            with httpx.Client(base_url=base, timeout=60) as client:
                for route in ('/api/bootstrap', '/api/pools/bench-0/dashboard?plots=pnl,price,balance,volume'):
                    times = []
                    before = int(Path('/proc/self/statm').read_text().split()[1]) * 4096
                    for run in range(args.runs):
                        with db._lock:
                            db._analytics_cache.clear()
                            db._analytics_cache_bytes = 0
                        start = time.perf_counter()
                        response = client.get(route)
                        response.raise_for_status()
                        times.append(time.perf_counter() - start)
                        print(f'{route}: uncached run {run + 1}/{args.runs} = {times[-1]:.3f}s', flush=True)
                    after = int(Path('/proc/self/statm').read_text().split()[1]) * 4096
                    start = time.perf_counter()
                    cached = client.get(route, headers={'If-None-Match': response.headers.get('etag', '')})
                    print(json.dumps(dict(route=route, p95_seconds=sorted(times)[math.ceil(.95 * len(times)) - 1], payload_MiB=len(response.content)/2**20,
                                          request_RSS_growth_MiB=(after-before)/2**20, cached_seconds=time.perf_counter()-start,
                                          cached_status=cached.status_code, cache_MiB=db._analytics_cache_bytes/2**20)), flush=True)
                if args.browser:
                    from playwright.sync_api import sync_playwright
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        page = browser.new_page()
                        plotly = browser_plotly()
                        page.route('**/plotly-*.min.js', lambda route: route.fulfill(path=plotly, content_type='application/javascript'))
                        page.add_init_script("localStorage.setItem('openPoolPlots', 'pnl,price,balance,volume')")
                        start = time.perf_counter()
                        page.goto(base)
                        page.wait_for_function("document.querySelector('#volumeChart').data?.length > 0")
                        cdp = page.context.new_cdp_session(page)
                        print(json.dumps(dict(browser_initial_seconds=time.perf_counter()-start, browser_heap=cdp.send('Runtime.getHeapUsage'))), flush=True)
                        start = time.perf_counter()
                        page.evaluate("Plotly.relayout('pnlChart', {'xaxis.range': [new Date(Date.now()-3000000).toISOString(), new Date(Date.now()-2400000).toISOString()]})")
                        page.wait_for_timeout(200)
                        page.wait_for_function("async () => !(await import(document.querySelector('script[type=module]').src)).state.refreshStarted")
                        print(json.dumps(dict(browser_zoom_seconds=time.perf_counter()-start, browser_heap=cdp.send('Runtime.getHeapUsage'))), flush=True)
                        browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()


if __name__ == '__main__':
    main()
