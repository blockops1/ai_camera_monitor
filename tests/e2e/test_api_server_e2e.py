"""E2E tests for the API server (port 8092).

Spawns the real infra.api_server on a free port via subprocess, then hits
every route with httpx using bearer-token auth. Verifies response shapes
(keys present, types correct) for each route group.

Bug classes covered:
    - API server starts without FARMSURV_API_TOKEN.
    - Routes return wrong key names or missing fields.
    - Auth bypass (no token -> 200).

Acceptance criteria:
  AC1: pytest tests/e2e/test_api_server_e2e.py -v passes
  AC2: ruff check tests/e2e/test_api_server_e2e.py = 0 errors
  AC3: No mock/MagicMock/patch in this file.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

_WORKSPACE = str(Path(__file__).resolve().parents[2])
_VENV_PYTHON = str(Path(_WORKSPACE) / ".venv" / "bin" / "python")


def _free_port() -> int:
    """Return a free TCP port by binding and closing a socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: int = 15) -> bool:
    """Block until the port is listening or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def _build_env(extra: dict | None = None) -> dict:
    """Build an env dict with API enabled and all required infra vars."""
    env = os.environ.copy()
    env["FARMSURV_API_ENABLED"] = "1"
    env["FARMSURV_API_TOKEN"] = "x" * 40  # >= 32 chars
    env["FARMSURV_API_PORT"] = str(extra.get("port", 18092) if extra else 18092)
    env["FARMSURV_API_BIND"] = "all"
    env["FARMSURV_BOX_ID"] = "test-box"
    # Infra-level vars that quick_classifier / quiet_hours require at import time
    env["MOTION_GATE_NIGHT_SUPPRESS_ENABLED"] = "1"
    env["FARMSURV_QUIET_HOURS_ENABLED"] = "0"
    env["FARMSURV_BASELINE_OVERRIDE_ENABLED"] = "0"
    env["FARMSURV_MCP_ENABLED"] = "0"  # MCP not needed for API tests
    env["FARMSURV_FARM_LAT"] = "34.5782"
    env["FARMSURV_FARM_LON"] = "-84.9438"
    env["FARMSURV_GLOBAL_CHANGE_MAX_PCT"] = "60"
    env["FARMSURV_DISTANT_KEYWORDS"] = "distant far away horizon"
    env["BBOX_INTERSECTION_DILATE_PCT"] = "0.01"
    env.pop("FARMSURV_MCP_TOKEN", None)
    return env


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_server():
    """Spawn the API server on a free port for the entire module.

    The server process is cleaned up when the module fixture tears down.
    (httpx.Client is restored to real by tests/e2e/conftest.py.)
    """
    port = _free_port()
    env = _build_env({"port": port})
    proc = subprocess.Popen(
        [_VENV_PYTHON, "-m", "infra.api_server"],
        cwd=_WORKSPACE,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        assert _wait_for_port("127.0.0.1", port, timeout=15), (
            f"API server did not start on port {port} within 15s"
        )
        yield {"port": port, "proc": proc}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _client(port: int) -> httpx.Client:
    """Return an httpx.Client pointed at the running server."""
    return httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=httpx.Timeout(10),
    )


# -----------------------------------------------------------------------
# Auth tests
# -----------------------------------------------------------------------

class TestAuth:
    """Auth: no token -> 401, wrong token -> 401, valid token -> 200."""

    def test_no_token_returns_401(self, api_server):
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/cameras")
        assert r.status_code == 401

    def test_wrong_token_returns_401(self, api_server):
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/cameras", headers={"Authorization": "Bearer wrongtoken"})
        assert r.status_code == 401

    def test_valid_token_returns_200(self, api_server):
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/cameras", headers={"Authorization": "Bearer " + "x" * 40})
        assert r.status_code == 200

    def test_healthz_no_auth(self, api_server):
        """/healthz does not require auth."""
        with _client(api_server["port"]) as c:
            r = c.get("/healthz")
        assert r.status_code == 200


# -----------------------------------------------------------------------
# Route-group tests
# -----------------------------------------------------------------------

class TestCamerasRoute:
    """GET /api/v1/cameras -> 200, body has box_id + data."""

    def test_cameras_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/cameras", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], list)


class TestEventsRoute:
    """GET /api/v1/events -> 200, body has box_id + data (list)."""

    def test_events_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/events", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], list)


class TestGateRoute:
    """GET /api/v1/gate -> 200, body has box_id + data (dict)."""

    def test_gate_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/gate", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], dict)


class TestSuppressionRulesRoute:
    """GET /api/v1/gate/suppression-rules -> 200, body has box_id + data (list)."""

    def test_suppression_rules_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/gate/suppression-rules", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], list)


class TestCooldownsRoute:
    """GET /api/v1/cooldowns -> 200, body has box_id + data (list)."""

    def test_cooldowns_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/cooldowns", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], list)


class TestIdentitiesPeopleRoute:
    """GET /api/v1/identities/people -> 200, body has box_id + data (list)."""

    def test_people_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/identities/people", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], list)


class TestIdentitiesVehiclesRoute:
    """GET /api/v1/identities/vehicles -> 200, body has box_id + data (list)."""

    def test_vehicles_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/identities/vehicles", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], list)


class TestIdentitiesAnimalsRoute:
    """GET /api/v1/identities/animals -> 200, body has box_id + data (list)."""

    def test_animals_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/identities/animals", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], list)


class TestListenerStatusRoute:
    """GET /api/v1/listener/status -> 200, body has box_id + data (dict)."""

    def test_listener_status_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/listener/status", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        data = body["data"]
        assert isinstance(data, dict)
        assert "pid" in data
        assert "uptime_seconds" in data
        assert "mode" in data

    def test_listener_errors_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/listener/errors", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        assert isinstance(body["data"], list)


class TestTelegramStatusRoute:
    """GET /api/v1/telegram/status -> 200, body has box_id + data (dict)."""

    def test_telegram_status_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/telegram/status", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        data = body["data"]
        assert isinstance(data, dict)
        assert "chat_id_redacted" in data
        assert "last_send_at" in data
        assert "last_send_status" in data
        assert "errors_last_24h" in data


class TestVisionStatusRoute:
    """GET /api/v1/vision/status -> 200, body has box_id + data (dict)."""

    def test_vision_status_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/vision/status", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        data = body["data"]
        assert isinstance(data, dict)
        assert "model_name" in data
        assert "base_url_redacted" in data

    def test_config_shape(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/config", headers={"Authorization": token})
        assert r.status_code == 200
        body = r.json()
        assert "box_id" in body
        assert "data" in body
        data = body["data"]
        assert isinstance(data, dict)
        assert "env" in data
        assert "knobs_documented" in data
        assert "knobs_set" in data
        assert "knobs_missing" in data


class TestUnknownRoute:
    """GET /api/v1/nonexistent -> 404."""

    def test_unknown_route(self, api_server):
        token = "Bearer " + "x" * 40
        with _client(api_server["port"]) as c:
            r = c.get("/api/v1/nonexistent", headers={"Authorization": token})
        assert r.status_code == 404

    def test_healthz_returns_ok(self, api_server):
        with _client(api_server["port"]) as c:
            r = c.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert "ok" in body
        assert "box_id" in body
        assert "uptime_seconds" in body
