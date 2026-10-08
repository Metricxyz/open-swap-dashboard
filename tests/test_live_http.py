import json
import os
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_LIVE_INTEGRATION") != "1", reason="set RUN_LIVE_INTEGRATION=1")
def test_real_uvicorn_serves_ui_api_and_enforces_csrf(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    environment = {
        **os.environ,
        "OPEN_METRIC_DASHBOARD_DATA_DIR": str(tmp_path / "server-data"),
        "OPEN_METRIC_DASHBOARD_HOST": "127.0.0.1",
        "OPEN_METRIC_DASHBOARD_PORT": str(port),
    }
    process = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=os.fspath(os.path.dirname(os.path.dirname(__file__))),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        print(f"waiting for isolated test server on {base_url}", flush=True)
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(f"{base_url}/api/bootstrap", timeout=1) as response:
                    bootstrap = json.load(response)
                break
            except (URLError, ConnectionError):
                if process.poll() is not None:
                    raise AssertionError(process.stdout.read())
                if time.monotonic() >= deadline:
                    raise AssertionError("isolated Uvicorn server did not start within 15 seconds")
                time.sleep(0.1)

        assert len(bootstrap["chains"]) == 10 and bootstrap["pools"] == []
        with urlopen(f"{base_url}/", timeout=2) as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert b"Open Metric Dashboard" in response.read()
        with urlopen(f"{base_url}/static/app.js?v=5", timeout=2) as response:
            assert response.headers["Cache-Control"] == "no-store"
            script = response.read()
            assert b"loadDashboard" in script and b"setUiLocked" in script

        no_csrf = Request(
            f"{base_url}/api/rpc/test",
            data=b'{"chain_key":"invalid"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(no_csrf, timeout=2)
        assert rejected.value.code == 403

        with_csrf = Request(
            f"{base_url}/api/rpc/test",
            data=b'{"chain_key":"invalid"}',
            headers={"Content-Type": "application/json", "X-CSRF-Token": bootstrap["csrf_token"]},
            method="POST",
        )
        with pytest.raises(HTTPError) as validated:
            urlopen(with_csrf, timeout=2)
        assert validated.value.code == 400
    finally:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 0
    assert (tmp_path / "server-data" / "dashboard.duckdb").exists()
