"""Run the real app with temporary secrets, independent of local credentials."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def app_url(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("streamlit-e2e")
    config = workdir / ".streamlit"
    config.mkdir()
    (config / "secrets.toml").write_text(
        'AUTH_USERNAME = "browser-test"\n'
        'AUTH_PASSWORD = "local-test-password"\n'
        'SHORTENER_BASE_URL = ""\n',
        encoding="utf-8",
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    with (workdir / "server.log").open("w+") as log:
        server = subprocess.Popen(
            [
                sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
                "--server.address=127.0.0.1", f"--server.port={port}",
                "--server.headless=true", "--browser.gatherUsageStats=false",
                f"--secrets.files={config / 'secrets.toml'}",
            ],
            cwd=workdir,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    break
                try:
                    with urlopen(f"{url}/_stcore/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Streamlit did not become ready within 30 seconds")
            if server.poll() is not None:
                log.seek(0)
                pytest.fail(f"Streamlit exited during startup:\n{log.read()}")
            yield url
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
