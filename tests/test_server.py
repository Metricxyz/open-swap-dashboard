import json
import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from dashboard import server
from dashboard.rpc import RpcStatus


POOL_ADDRESS = "0x00000000000000000000000000000000000000aa"
TOKEN0 = "0x0000000000000000000000000000000000000001"
TOKEN1 = "0x0000000000000000000000000000000000000002"


class ConnectedClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def detected_pool(start_block=100):
    return {
        "pool_address": POOL_ADDRESS,
        "assets": [
            {"address": TOKEN0, "symbol": "WETH", "decimals": 18},
            {"address": TOKEN1, "symbol": "USDC", "decimals": 6},
        ],
        "start_block": start_block,
        "latest_block": 200,
        "finalized_block": 180,
        "rpc": "https://rpc.test",
    }


def pool_input(name="WETH / USDC"):
    return server.PoolInput(
        name=name,
        chain_key="base",
        address=POOL_ADDRESS,
        start_block=100,
        assets=[
            server.AssetInput(address=TOKEN0, price_kind="binance", market="ETHUSDT", market_type="spot"),
            server.AssetInput(address=TOKEN1, price_kind="fixed", fixed_price=1.0),
        ],
    )


def endpoint(app, path, method):
    return next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set())
    )


def assert_http_error(status, call, *args, **kwargs):
    with pytest.raises(HTTPException) as raised:
        call(*args, **kwargs)
    assert raised.value.status_code == status


def request(method, path, headers=()):
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    })


def test_mutation_security_policy():
    assert server._mutation_rejection(request("GET", "/api/bootstrap"), "secret") is None
    missing = server._mutation_rejection(request("POST", "/api/rpc/test", [("host", "testserver")]), "secret")
    assert missing.status_code == 403
    assert json.loads(missing.body) == {"detail": "Invalid CSRF token"}
    cross = server._mutation_rejection(
        request("POST", "/api/rpc/test", [("host", "testserver"), ("origin", "https://attacker.test"), ("x-csrf-token", "secret")]),
        "secret",
    )
    assert cross.status_code == 403
    valid = request("POST", "/api/rpc/test", [("host", "testserver"), ("origin", "http://testserver"), ("x-csrf-token", "secret")])
    assert server._mutation_rejection(valid, "secret") is None


def test_operator_console_static_shell():
    html = (server.ROOT / "web" / "index.html").read_text()
    assert 'id="sidebar"' in html
    assert 'id="poolManagement"' in html
    assert 'id="sidebarToggle"' in html
    assert 'id="poolEditor"' in html
    assert 'id="rpcSettings"' in html
    assert 'id="rpcRows"' in html
    assert 'id="rpcChainInput"' not in html
    assert 'id="uiLock"' in html
    assert 'id="activityStatus"' in html
    assert 'id="chartAvailability"' in html
    assert 'class="statusbar mono"' in html
    assert 'id="backendActivity"' in html
    assert 'id="footerActivity"' in html
    assert '<button id="addPoolButton" class="caption-button primary" type="button">+ ADD POOL</button>' in html
    assert '<div class="pool-list-controls" aria-label="Pool list display controls">' in html
    assert 'id="editPoolButton"' in html
    assert 'id="removePoolButton"' in html
    assert 'id="pausePoolButton"' not in html
    assert 'id="rebuildPoolButton"' not in html
    assert '<details class="panel table-panel collapsible-panel">' in html
    assert '<summary>CONFIGURE PLOTS</summary>' in html
    assert '<input type="checkbox" data-plot="pnl" checked>PNL HISTORY' in html
    assert '<input type="checkbox" data-plot="price" checked>PRICE HISTORY' in html
    assert '<input type="checkbox" data-plot="balance">BALANCES' in html
    assert '<input type="checkbox" data-plot="volume">CUMULATIVE USD VOLUME' in html
    assert 'id="hideSmallSwaps"' in html
    assert 'id="minimumSwapUsd"' in html
    assert html.count('class="chart-fullscreen"') == 4
    assert 'data-fullscreen-chart="priceChart"' in html
    assert 'CTRL+CLICK TRADE → EXPLORER' in html
    assert '<i>CLICK TO EXPAND</i>' in html
    assert '<details class="panel table-panel collapsible-panel" open>' not in html
    assert 'value="bybit" disabled hidden' in html
    assert "<th>FLOW PNL</th><th>SWAP EDGE</th>" in html
    assert html.count('class="fixed-price mono" type="number" min="0" step="any" placeholder="1.0" disabled') == 2
    assert "/static/app.css?v=" in html
    assert "/static/app.js?v=" in html
    assert 'id="mtmModeInput"' in html
    assert '<option value="balanced">50/50' in html
    assert 'id="balancesMetric" class="balance-values"' in html


def test_api_route_contracts_cover_full_configuration_lifecycle(monkeypatch, tmp_path):
    app = server.create_app(database_path=tmp_path / "api.duckdb", start_scheduler=False)
    wakes = []
    monkeypatch.setattr(app.state.scheduler, "wake", lambda: wakes.append(True))
    routes = {
        (path, method): endpoint(app, path, method)
        for path, method in (
            ("/api/bootstrap", "GET"),
            ("/api/pools/{pool_id}/dashboard", "GET"),
            ("/api/rpc/test", "POST"),
            ("/api/chains/{chain_key}/rpc", "PUT"),
            ("/api/chains/{chain_key}/rpc", "DELETE"),
            ("/api/envio/settings", "PUT"),
            ("/api/envio/key", "DELETE"),
            ("/api/pools/discover", "POST"),
            ("/api/pools", "POST"),
            ("/api/pools/{pool_id}", "PUT"),
            ("/api/pools/{pool_id}/pause", "POST"),
            ("/api/pools/{pool_id}/rebuild", "POST"),
            ("/api/pools/{pool_id}", "DELETE"),
            ("/", "GET"),
        )
    }
    empty_bootstrap = routes[("/api/bootstrap", "GET")]()
    assert empty_bootstrap["pools"] == []
    assert empty_bootstrap["envio"] == {"configured": False, "rpm_limit": 15}
    assert app.state.scheduler.status()["operation"]["message"] == "IDLE — SCHEDULER NOT STARTED"
    assert empty_bootstrap["summary_window_hours"] == 24
    assert str(routes[("/", "GET")]().path).endswith("web/index.html")

    assert_http_error(400, routes[("/api/rpc/test", "POST")], server.RpcTestInput(chain_key="nope"))
    connected = ConnectedClient()
    monkeypatch.setattr(
        server,
        "connect_rpc",
        lambda _urls, _chain_id: (
            connected,
            RpcStatus("https://rpc.test", "https://rpc.test", 8453, 123),
        ),
    )
    assert routes[("/api/rpc/test", "POST")](server.RpcTestInput(chain_key="base"))["latest_block"] == 123
    assert connected.closed
    assert routes[("/api/chains/{chain_key}/rpc", "PUT")]("base", server.RpcInput(url="https://rpc.test/key"))["ok"]
    assert_http_error(404, routes[("/api/chains/{chain_key}/rpc", "PUT")], "nope", server.RpcInput(url="https://rpc.test"))
    assert_http_error(404, routes[("/api/chains/{chain_key}/rpc", "DELETE")], "nope")

    monkeypatch.setattr(server, "validate_pool_input", lambda *_args, **kwargs: detected_pool(kwargs.get("start_block") or 100))
    monkeypatch.setattr(
        server,
        "discover_pool_assets",
        lambda *_args, **_kwargs: {**detected_pool(), "generated_name": "WETH/USDC 0x0000...00aa"},
    )
    validated_markets = []
    monkeypatch.setattr(server, "validate_market", lambda *args: validated_markets.append(args))
    discovered = routes[("/api/pools/discover", "POST")](
        server.PoolDiscoveryInput(chain_key="base", address=POOL_ADDRESS)
    )
    assert discovered["generated_name"] == "WETH/USDC 0x0000...00aa"
    pool_id = routes[("/api/pools", "POST")](pool_input())["pool_id"]
    assert validated_markets == [("binance", "ETHUSDT", "spot")]
    second_id = routes[("/api/pools", "POST")](pool_input())["pool_id"]
    assert second_id.endswith("-2")
    bootstrap = routes[("/api/bootstrap", "GET")](6)
    assert len(bootstrap["pools"]) == 2
    assert bootstrap["summary_window_hours"] == 6
    assert bootstrap["pools"][0]["summary"]["volume_usd"] is None
    assert all(pool['mtm_mode'] == 'balanced' for pool in bootstrap['pools'])
    assert bootstrap["total_summary"]["volume_usd"] is None
    assert json.loads(routes[("/api/pools/{pool_id}/dashboard", "GET")](pool_id, 6).body)["metrics"]["swap_count"] == 0
    assert_http_error(404, routes[("/api/pools/{pool_id}/dashboard", "GET")], "missing", 24)

    update = server.PoolUpdateInput(name="Renamed", start_block=100, assets=pool_input().assets, mtm_mode='as_is')
    monkeypatch.setattr(server, 'validate_pool_input', lambda *_a, **_k: pytest.fail('MTM-only edit called RPC'))
    monkeypatch.setattr(server, 'validate_market', lambda *_a, **_k: pytest.fail('MTM-only edit called exchange'))
    assert routes[("/api/pools/{pool_id}", "PUT")](pool_id, update)["ok"]
    assert app.state.db.get_pool(pool_id)['mtm_mode'] == 'as_is'
    assert json.loads(routes[("/api/pools/{pool_id}/dashboard", "GET")](pool_id, 6).body)['pool']['mtm_mode'] == 'as_is'
    assert_http_error(404, routes[("/api/pools/{pool_id}", "PUT")], "missing", update)
    assert routes[("/api/pools/{pool_id}/pause", "POST")](pool_id, server.PauseInput(paused=True))["ok"]
    assert routes[("/api/pools/{pool_id}/rebuild", "POST")](pool_id)["ok"]
    assert_http_error(404, routes[("/api/pools/{pool_id}/pause", "POST")], "missing", server.PauseInput(paused=True))
    assert_http_error(404, routes[("/api/pools/{pool_id}/rebuild", "POST")], "missing")

    assert_http_error(404, routes[("/api/pools/{pool_id}", "DELETE")], "missing")
    assert routes[("/api/pools/{pool_id}", "DELETE")](pool_id)["ok"]
    assert routes[("/api/chains/{chain_key}/rpc", "DELETE")]("base")["ok"]
    assert routes[("/api/envio/settings", "PUT")](
        server.EnvioSettingsInput(api_key="secret", rpm_limit=40)
    )["ok"]
    configured = routes[("/api/bootstrap", "GET")]()
    assert configured["envio"] == {"configured": True, "rpm_limit": 40}
    assert "secret" not in str(configured)
    assert routes[("/api/envio/key", "DELETE")]()["ok"]
    assert len(wakes) >= 7
    app.state.db.close()


def test_api_routes_translate_validation_and_rpc_errors(monkeypatch, tmp_path):
    app = server.create_app(database_path=tmp_path / "errors.duckdb", start_scheduler=False)
    test_rpc = endpoint(app, "/api/rpc/test", "POST")
    save_rpc = endpoint(app, "/api/chains/{chain_key}/rpc", "PUT")
    discover = endpoint(app, "/api/pools/discover", "POST")
    add = endpoint(app, "/api/pools", "POST")
    monkeypatch.setattr(server, "connect_rpc", lambda *_args: (_ for _ in ()).throw(RuntimeError("rpc failed")))
    assert_http_error(400, test_rpc, server.RpcTestInput(chain_key="base"))
    assert_http_error(400, save_rpc, "base", server.RpcInput(url="notaurl"))
    monkeypatch.setattr(server, "validate_pool_input", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad pool")))
    monkeypatch.setattr(server, "discover_pool_assets", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("no transfers")))
    assert_http_error(400, discover, server.PoolDiscoveryInput(chain_key="base", address=POOL_ADDRESS))
    assert_http_error(400, add, pool_input())
    app.state.db.close()


def test_asset_normalization_lifespan_and_server_entrypoint(monkeypatch, tmp_path):
    detected = detected_pool()["assets"]
    fixed = server._normalize_assets(
        [server.AssetInput(address=TOKEN0, price_kind="fixed", fixed_price=2), server.AssetInput(address=TOKEN1)],
        detected,
    )
    assert fixed[0]["fixed_price"] == 2 and fixed[1]["price_kind"] == "none"
    with pytest.raises(ValueError, match="Exactly two"):
        server._normalize_assets([], detected)
    with pytest.raises(ValueError, match="must be positive"):
        server._normalize_assets(
            [server.AssetInput(address=TOKEN0, price_kind="fixed", fixed_price=0), server.AssetInput(address=TOKEN1)],
            detected,
            )
    with pytest.raises(ValueError, match="requires a market"):
        server._normalize_assets(
            [server.AssetInput(address=TOKEN0, price_kind="binance"), server.AssetInput(address=TOKEN1)],
            detected,
            )
    with pytest.raises(ValueError, match="Price kind"):
        server._normalize_assets(
            [server.AssetInput(address=TOKEN0, price_kind="oracle"), server.AssetInput(address=TOKEN1)],
            detected,
            )
    with pytest.raises(ValueError, match="Price kind"):
        server._normalize_assets(
            [server.AssetInput(address=TOKEN0, price_kind="bybit", market="ETHUSDT"), server.AssetInput(address=TOKEN1)],
            detected,
            )

    lifespan_app = server.create_app(database_path=tmp_path / "lifespan.duckdb", start_scheduler=False)

    async def enter_lifespan():
        async with lifespan_app.router.lifespan_context(lifespan_app):
                assert lifespan_app.state.db.row("SELECT version FROM schema_meta")["version"] == 11

    asyncio.run(enter_lifespan())

    launched = []
    monkeypatch.setenv("OPEN_METRIC_DASHBOARD_DATA_DIR", str(tmp_path / "run-data"))
    monkeypatch.setenv("OPEN_METRIC_DASHBOARD_HOST", "127.0.0.2")
    monkeypatch.setenv("OPEN_METRIC_DASHBOARD_PORT", "9999")

    def fake_run(app, **kwargs):
        launched.append(kwargs)
        app.state.db.close()

    monkeypatch.setattr(server.uvicorn, "run", fake_run)
    server.run()
    assert launched == [{"host": "127.0.0.2", "port": 9999, "log_level": "info"}]
