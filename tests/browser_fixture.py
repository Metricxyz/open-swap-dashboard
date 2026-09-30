"""Isolated browser fixture server; never reads production data or credentials."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.server import create_app
from conftest import POOL, ASSETS
from test_trustworthiness import commit, swap_logs, price_swaps
import uvicorn

fixture_dir = Path(tempfile.mkdtemp(prefix="open-metric-dashboard-browser-"))
app = create_app(database_path=fixture_dir / "fixture.duckdb", start_scheduler=False)
app.state.db.create_pool(POOL, ASSETS)
commit(app.state.db, swap_logs("0xz", 0) + swap_logs("0xa", 1))
price_swaps(app.state.db)
app.state.scheduler._refresh_status()
print(f"Serving fixture data from {fixture_dir}", flush=True)
uvicorn.run(app, host="127.0.0.1", port=8053)
