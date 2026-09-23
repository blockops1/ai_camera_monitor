"""test_daemon_api_spawn -- Verify API server spawns from daemon when enabled.

AC1: Spawn listener via subprocess with FARMSURV_API_ENABLED=1 and a
known 32+ char FARMSURV_API_TOKEN. Wait 5s. GET /healthz with bearer
token returns HTTP 200 + {ok: true, box_id, api_enabled}.

Tear down via SIGTERM.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

_FAKE_TOKEN = "b" * 40  # 40 chars, well over the 32-char minimum
_FAKE_BOX_ID = "test-box-api"
_API_TEST_PORT = 8097
_WORKSPACE = os.path.join(os.path.dirname(__file__), "..")


def _build_test_env():
    """Build an env dict for spawning the listener with API enabled."""
    env = os.environ.copy()
    env["FARMSURV_API_ENABLED"] = "1"
    env["FARMSURV_API_TOKEN"] = _FAKE_TOKEN
    env["FARMSURV_API_BIND"] = "127.0.0.1"
    env["FARMSURV_API_PORT"] = str(_API_TEST_PORT)
    env["FARMSURV_BOX_ID"] = _FAKE_BOX_ID
    env["LISTEN_HOST"] = "127.0.0.1"
    env["LISTEN_PORT"] = "0"  # random port; Flask won't conflict with API
    # Prevent camera/telegram creds from being needed
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_HOME_CHAT_ID", None)
    env.pop("VISION_LLM_URL", None)
    return env


def _healthz(port: int, token: str) -> dict:
    """GET /healthz with bearer token and return parsed JSON."""
    url = f"http://127.0.0.1:{port}/healthz"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
    })
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode())


class TestDaemonAPISpawn:
    """AC1: listener spawns API server on daemon thread when FARMSURV_API_ENABLED=1."""

    def test_healthz_returns_ok(self):
        """API server is reachable via /healthz with bearer token."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "listener.daemon"],
            cwd=_WORKSPACE,
            env=_build_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            # Wait for the API server to start (up to 10s)
            healthz_data = {}
            for _ in range(40):
                time.sleep(0.25)
                try:
                    data = _healthz(_API_TEST_PORT, _FAKE_TOKEN)
                    healthz_data = data
                    break
                except (urllib.error.URLError, ConnectionRefusedError, OSError):
                    continue

            if not healthz_data:
                # Read from the process output for debugging
                proc.terminate()
                proc.wait(timeout=5)
                output = proc.stdout.read().decode() if proc.stdout else ""
                raise AssertionError(
                    f"API server not reachable on port {_API_TEST_PORT}. "
                    f"Process output:\n{output[:1000]}"
                )

            assert healthz_data["ok"] is True
            assert healthz_data["box_id"] == _FAKE_BOX_ID
            assert healthz_data["api_enabled"] is True
            assert isinstance(healthz_data["uptime_seconds"], (int, float))
            assert healthz_data["uptime_seconds"] >= 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
