from pathlib import Path
import re
import tempfile

import httpx
import pytest

from dashboard.database import Database


@pytest.fixture(autouse=True)
def isolate_provider_cooldowns(monkeypatch):
    from dashboard import prices
    monkeypatch.setattr(prices, "_PROVIDER_DEADLINES", {})


# ==== Browser scripts ====

BROWSER_DIR = Path(tempfile.gettempdir()) / "open-metric-dashboard-browser"


def browser_plotly() -> Path:
    """The Plotly build index.html loads, downloaded once so browser scripts serve it locally."""
    index = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text()
    url = re.search(r'src="(https://cdn\.plot\.ly/[^"]+)"', index).group(1)
    path = BROWSER_DIR / url.rsplit("/", 1)[1]
    if not path.exists():
        BROWSER_DIR.mkdir(exist_ok=True)
        print(f"Downloading {url}", flush=True)
        path.write_bytes(httpx.get(url, follow_redirects=True, timeout=60).raise_for_status().content)
    return path


POOL = {
    "id": "base-test",
    "name": "Test pool",
    "chain_key": "base",
    "address": "0x00000000000000000000000000000000000000aa",
    "start_block": 100,
    "contract_type": "two_asset_transfers",
}

ASSETS = [
    {
        "symbol": "WETH",
        "address": "0x0000000000000000000000000000000000000001",
        "decimals": 18,
        "price_kind": "binance",
        "exchange": "binance",
        "market": "ETHUSDT",
        "market_type": "spot",
        "quote_verified": True, "quote_asset": "USDT",
    },
    {
        "symbol": "USDC",
        "address": "0x0000000000000000000000000000000000000002",
        "decimals": 6,
        "price_kind": "fixed",
        "fixed_price": 1.0,
    },
]


def configured_database(tmp_path, *, assets=None):
    db = Database(tmp_path / "dashboard.duckdb")
    db.create_pool(POOL, assets or ASSETS)
    return db
