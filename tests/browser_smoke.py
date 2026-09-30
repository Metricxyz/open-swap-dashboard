"""Run against an isolated seeded server: uv run --with playwright python tests/browser_smoke.py."""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from conftest import BROWSER_DIR, browser_plotly


async def main():
    plotly = browser_plotly()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(timezone_id='Etc/GMT+3', viewport={'width': 1400, 'height': 1000})
        page = await context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.route('**/plotly-*.min.js', lambda route: route.abort())
        await page.goto('http://127.0.0.1:8053')
        await page.wait_for_selector('#poolTitle:text("Test pool")')
        await page.wait_for_function("document.querySelector('#activityStatus').textContent.includes('Showing Test pool')")
        assert 'unavailable' in (await page.locator('#chartAvailability').inner_text()).lower()
        print('PASS: initial dashboard, UTC-3 timezone, blocked chart feedback', flush=True)
        # Drag a header boundary, including a status refresh during the drag.
        headers = page.locator('.pool-table th')
        original = await headers.evaluate_all('(cells) => cells.map(cell => cell.getBoundingClientRect().width)')
        handle = page.locator('.column-resizer').first
        box = await handle.bounding_box()
        x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x - 35, y, steps=5)
        await page.evaluate("async () => (await import(document.querySelector('script[type=module]').src)).pollStatus()")
        await page.mouse.move(x - 50, y, steps=5)
        await page.mouse.up()
        resized = await headers.evaluate_all('(cells) => cells.map(cell => cell.getBoundingClientRect().width)')
        assert abs(resized[0] - original[0] + 50) < 2
        assert abs(resized[1] - original[1] - 50) < 2
        assert abs(sum(resized) - sum(original)) < 2
        await page.reload()
        await page.wait_for_selector('.column-resizer')
        restored = await headers.first.bounding_box()
        assert abs(restored['width'] - resized[0]) < 2
        await handle.focus()
        await handle.press('ArrowRight')
        assert abs((await headers.first.bounding_box())['width'] - resized[0] - 10) < 2
        for _ in range(40):
            await handle.press('ArrowLeft')
        assert (await headers.first.bounding_box())['width'] >= 39
        await page.evaluate("localStorage.removeItem('openPoolColumnWidths')")
        await page.reload()
        await page.wait_for_selector('#poolTitle:text("Test pool")')
        print('PASS: column drag, refresh during drag, reload persistence, keyboard and minimum width', flush=True)
        in_flight = 0
        peak = 0
        async def slow_dashboard(route):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(1)
            response = await route.fetch()
            await route.fulfill(response=response)
            in_flight -= 1
        await page.route('**/api/pools/*/dashboard?*', slow_dashboard)
        buttons = page.locator('#windowControls button[data-hours]')
        values = await buttons.evaluate_all('(buttons) => buttons.map(b => b.dataset.hours)')
        assert len(values) >= 2
        await page.locator(f'#windowControls button[data-hours="{values[0]}"]').click()
        await page.locator(f'#windowControls button[data-hours="{values[-1]}"]').click()
        assert await page.locator('#poolTitle').inner_text() == 'Test pool'
        activity = await page.locator('#activityStatus').inner_text()
        assert 'Still showing' not in activity and 'Display refresh' not in activity
        await page.wait_for_function("async () => !(await import(document.querySelector('script[type=module]').src)).state.refreshStarted")
        assert peak == 1
        print('PASS: rapid window selection, retained data, single-flight refresh', flush=True)
        await page.route('**/api/status', lambda route: route.abort())
        await page.wait_for_function("document.querySelector('#activityStatus').textContent.includes('Lost connection')", timeout=15000)
        assert await page.locator('#poolTitle').inner_text() == 'Test pool'
        await page.unroute('**/api/status')
        await page.wait_for_function("!document.querySelector('#activityStatus').textContent.includes('Lost connection')", timeout=15000)
        print('PASS: connection failure retains data and recovers', flush=True)
        async def rpc_error(route):
            await asyncio.sleep(1)
            await route.fulfill(status=400, json={'detail': 'Fixture RPC failure'})
        await page.route('**/api/rpc/test', rpc_error)
        await page.locator('#rpcConfigButton').click()
        test_button = page.locator('[data-rpc-action="test"]').first
        await test_button.click()
        assert await test_button.is_disabled()
        assert 'TESTING' in await page.locator('.rpc-row .rpc-result').first.inner_text()
        await page.wait_for_function("document.querySelector('.rpc-row .rpc-result').textContent.includes('Fixture RPC failure')")
        await asyncio.sleep(11)
        assert 'Fixture RPC failure' in await page.locator('.rpc-row .rpc-result').first.inner_text()
        print('PASS: slow RPC feedback, duplicate prevention, persistent configuration error', flush=True)
        await page.locator('#closeRpcSettings').click()
        await page.locator('#addPoolButton').click()
        assets = [dict(symbol='A', address='0x' + '01'.zfill(40), decimals=18, price_kind='none'),
                  dict(symbol='B', address='0x' + '02'.zfill(40), decimals=6, price_kind='none')]
        async def discover(route):
            await asyncio.sleep(1)
            await route.fulfill(json={'chain_key': 'base', 'generated_name': 'Discovered test', 'assets': assets, 'rpc': 'fixture'})
        await page.route('**/api/pools/discover', discover)
        await page.locator('#poolChainInput').select_option('base')
        await page.locator('#poolAddressInput').fill('0x' + 'bb'.zfill(40))
        await page.locator('#poolAddressInput').blur()
        await page.wait_for_function("document.querySelector('#previewPool').disabled")
        assert 'Discovering' in await page.locator('#poolFormStatus').inner_text()
        await page.wait_for_function("!document.querySelector('#savePool').disabled")
        async def save_error(route):
            await asyncio.sleep(1)
            await route.fulfill(status=400, json={'detail': 'Fixture save failure'})
        await page.route('**/api/pools', save_error)
        await page.locator('#savePool').click()
        assert await page.locator('#savePool').is_disabled()
        assert 'adding pool' in await page.locator('#poolFormStatus').inner_text()
        await page.wait_for_function("document.querySelector('#poolFormStatus').textContent.includes('Fixture save failure')")
        assert not await page.locator('#savePool').is_disabled()
        print('PASS: slow discovery and save feedback with persistent save failure', flush=True)
        await page.locator('#cancelEdit').click()
        # Exercise the same range parsing/filtering used by charts in this timezone.
        result = await page.evaluate("""async () => {
            const {timeRangeFromRelayout} = await import('/static/charts.js?v=28');
            return timeRangeFromRelayout({'xaxis.range': ['2026-01-01 00:00:00', '2026-01-01 01:00:00']});
        }""")
        assert result['range'] == ['2026-01-01T00:00:00.000Z', '2026-01-01T01:00:00.000Z']
        # Exercise the actual Plotly build with deterministic local CDN content.
        zoom_page = await context.new_page()
        await zoom_page.route('**/zoom-fixture', lambda route: route.fulfill(
            content_type='text/html', body='<div id="pnlChart"></div><div id="volumeChart"></div>'))
        await zoom_page.goto('http://127.0.0.1:8053/zoom-fixture')
        await zoom_page.add_script_tag(path=plotly)
        zoom = await zoom_page.evaluate("""async () => {
            const charts = await import('/static/charts.js?v=29');
            const points = [0, 1, 2].map(i => ({timestamp: `2026-01-01T0${i}:00:00Z`,
                flow: 100 + i, mtm: 110 + i, volume: 1000000 + i * 100000}));
            charts.renderPnlChart(points);
            charts.renderCumulativeVolumeChart(points);
            const pnl = document.getElementById('pnlChart');
            const volume = document.getElementById('volumeChart');
            const range = ['2026-01-01T00:30:00Z', '2026-01-01T01:30:00Z'];
            await Plotly.relayout(pnl, {'xaxis.range': range, 'yaxis.range': [102, 108]});
            await charts.setChartTimeRange('volumeChart', range);
            charts.renderCumulativeVolumeChart(points.slice(1), {range});
            charts.renderPnlChart(points.slice(1), {range});
            const zoomed = {pnl: [...pnl._fullLayout.yaxis.range],
                volume: [...volume._fullLayout.yaxis.range]};
            await Plotly.relayout(pnl, {'xaxis.autorange': true, 'yaxis.autorange': true});
            charts.renderPnlChart(points);
            const reset = [...pnl._fullLayout.yaxis.range];
            charts.renderPnlChart(points.map(row => ({...row, flow: 500, mtm: 600})), {revision: 'other-pool'});
            return {zoomed, reset, changedPool: pnl._fullLayout.yaxis.range};
        }""")
        assert zoom['zoomed']['pnl'] == [102, 108]
        assert zoom['zoomed']['volume'][1] > 1200000
        assert zoom['reset'][0] < 100 and zoom['reset'][1] > 112
        assert zoom['changedPool'][0] > 400 and zoom['changedPool'][1] > 600
        await zoom_page.close()
        print('PASS: independent Y scales, zoom retained through refresh, reset and pool change', flush=True)
        chart_page = await context.new_page()
        await chart_page.route('**/plotly-*.min.js', lambda route: route.fulfill(path=plotly, content_type='application/javascript'))
        await chart_page.goto('http://127.0.0.1:8053')
        await chart_page.wait_for_function("document.querySelector('#pnlChart').data?.length > 0")
        await chart_page.evaluate("Plotly.relayout('pnlChart', {'xaxis.range': ['2020-01-01 00:00:00', '2020-01-02 00:00:00']})")
        await chart_page.wait_for_function("document.querySelector('#swapsBody')?.textContent.includes('NO SWAPS') || document.querySelector('#swapRows')?.textContent.includes('NO SWAPS')")
        print('PASS: actual Plotly range filters swaps in UTC-3', flush=True)
        # A retired mapping remains visible and survives a name-only save until
        # the operator explicitly replaces it; no legacy provider is contacted.
        async def legacy_bootstrap(route):
            response = await route.fetch()
            payload = await response.json()
            payload['pools'][0]['assets'][0].update(price_kind='bybit', exchange='bybit')
            await route.fulfill(json=payload)
        saved = []
        async def save_legacy(route):
            saved.append(route.request.post_data_json)
            await route.fulfill(json={'ok': True})
        await chart_page.route('**/api/bootstrap?*', legacy_bootstrap)
        await chart_page.route('**/api/pools/base-test', save_legacy)
        await chart_page.reload()
        await chart_page.wait_for_selector('#poolTitle:text("Test pool")')
        await chart_page.locator('#editPoolButton').click()
        assert await chart_page.locator('.price-kind').first.input_value() == 'bybit'
        assert 'retired' in await chart_page.locator('#poolFormStatus').inner_text()
        await chart_page.locator('#poolNameInput').fill('Renamed legacy pool')
        await chart_page.locator('#savePool').click()
        await chart_page.wait_for_function("document.querySelector('#poolEditor').classList.contains('hidden')")
        assert saved[-1]['assets'][0]['price_kind'] == 'bybit'
        await chart_page.locator('#editPoolButton').click()
        await chart_page.locator('.price-kind').first.select_option('fixed')
        await chart_page.locator('.fixed-price').first.fill('2000')
        await chart_page.locator('#savePool').click()
        await chart_page.wait_for_function("document.querySelector('#poolEditor').classList.contains('hidden')")
        assert saved[-1]['assets'][0]['price_kind'] == 'fixed'
        print('PASS: retired Bybit mapping is visible, preserved on rename, and explicitly replaceable', flush=True)
        await chart_page.close()
        await page.set_viewport_size({'width': 900, 'height': 900})
        await page.locator('#sidebarToggle').click()
        await page.set_viewport_size({'width': 1000, 'height': 900})
        await page.screenshot(path=BROWSER_DIR / 'dashboard.png', full_page=True)
        assert not errors, errors
        print('PASS: UTC chart range and responsive sidebar; no JavaScript exceptions', flush=True)
        await browser.close()


asyncio.run(main())
