"""Isolated MTM editor smoke test: uv run --with playwright python tests/browser_mtm.py."""
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
import uvicorn
from benchmark_dashboard import seed
from conftest import BROWSER_DIR, browser_plotly
from dashboard.server import create_app


plotly = browser_plotly()
with tempfile.TemporaryDirectory(prefix='mtm-browser-') as folder:
    app = create_app(database_path=Path(folder)/'fixture.duckdb', start_scheduler=False)
    db = app.state.db
    seed(db, swaps=200)
    db.write(lambda c: c.execute("INSERT INTO balance_points VALUES ('bench-0',99,now()-INTERVAL 2 DAY,100,100000)"))
    db.write(lambda c: c.execute("UPDATE reference_prices SET price=2200 WHERE asset_position=0 AND tx_hash='0x' || lpad(to_hex(199),64,'0')"))
    listener = socket.socket()
    listener.bind(('127.0.0.1',0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
    thread = threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+10
        while not server.started:
            assert thread.is_alive() and time.monotonic()<deadline
            time.sleep(.02)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width':1400,'height':1000})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/plotly-*.min.js',lambda route:route.fulfill(path=plotly,content_type='application/javascript'))
            page.goto(f'http://127.0.0.1:{port}')
            page.wait_for_function("document.querySelector('#pnlChart').data?.[1]?.name === 'MTM 50/50'")
            assert page.locator('#mtmAsOf').inner_text().startswith('50/50')
            print('PASS default 50/50 metric and chart',flush=True)
            for mode,expected in [('as_is',200),('balanced',5200)]:
                page.locator('#editPoolButton').click()
                page.wait_for_selector('#mtmModeInput:visible')
                page.locator('#mtmModeInput').select_option(mode)
                page.screenshot(path=BROWSER_DIR/f'mtm-editor-{mode}.png',full_page=True)
                with page.expect_response(lambda r:r.request.method=='PUT' and r.url.endswith('/api/pools/bench-0')) as saved:
                    page.locator('#savePool').click()
                assert saved.value.status==200,saved.value.text()
                label='MTM AS IS' if mode=='as_is' else 'MTM 50/50'
                page.wait_for_function('(label)=>document.querySelector("#pnlChart").data?.[1]?.name===label',arg=label)
                assert abs(page.locator('#pnlChart').evaluate('(el)=>el.data[1].y.at(-1)')-expected)<.001
                page.reload()
                page.wait_for_function('(label)=>document.querySelector("#pnlChart").data?.[1]?.name===label',arg=label)
                payload=page.evaluate('async()=> (await fetch("/api/bootstrap")).json()')
                assert payload['pools'][0]['mtm_mode']==mode
                assert payload['pools'][0]['summary']['mtm_pnl_usd']==expected
                assert payload['total_summary']['mtm_pnl_usd']==expected
                page.locator('#editPoolButton').click()
                assert page.locator('#mtmModeInput').input_value()==mode
                page.locator('#cancelEdit').click()
                print(f'PASS {mode}: save, chart, pool summary, total, reload, editor persistence',flush=True)
            page.locator('#addPoolButton').click()
            assert page.locator('#mtmModeInput').input_value()=='balanced'
            assert not errors,errors
            browser.close()
            print('PASS new-pool default and no browser errors',flush=True)
    finally:
        server.should_exit=True
        thread.join(timeout=10)
