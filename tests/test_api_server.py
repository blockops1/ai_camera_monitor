"""test_api_server -- Tests for the read-only API server (infra/api_server.py).

STATUS: alpha -- V2-072b
RUNS: pytest tests/test_api_server.py

COVERAGE:
    - test_healthz: /healthz returns 200
    - 17 route tests: all /api/v1/* endpoints return 200 with box_id
    - 4 auth tests: missing / wrong / valid / short token
    - 1 bind-refusal test
    = 23+ tests total
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from unittest import mock

import pytest

import infra.api_server as _api_server_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_TOKEN = "a" * 40  # 40-char token, well over the 32-char minimum


@pytest.fixture(autouse=True)
def _setup_api_env():
    """Ensure API env vars and module constants are set for each test."""
    with mock.patch.dict(os.environ, {
        "FARMSURV_API_ENABLED": "1",
        "FARMSURV_API_TOKEN": _FAKE_TOKEN,
        "FARMSURV_API_BIND": "127.0.0.1",
        "FARMSURV_API_PORT": "0",
        "FARMSURV_BOX_ID": "test-box-01",
    }, clear=False):
        # Re-set the module constants so the server sees them
        _api_server_mod._API_ENABLED = True
        _api_server_mod._API_BIND_MODE = "127.0.0.1"
        _api_server_mod._API_PORT = 0
        _api_server_mod._API_TOKEN = _FAKE_TOKEN
        _api_server_mod._BOX_ID = "test-box-01"
        yield


@pytest.fixture()
def api_server():
    """Start a real API server on a random free port for integration tests."""
    from infra.api_server import (
        _APIHandler,
        _ThreadingHTTPServer,
    )

    # Find a free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = _ThreadingHTTPServer(("127.0.0.1", port), _APIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield server, port

    server.shutdown()


# ---------------------------------------------------------------------------
# Helper: GET from an API server
# ---------------------------------------------------------------------------

def _get(api_server, path, token=None):
    """GET a path and return the parsed response."""
    _, port = api_server
    url = f"http://127.0.0.1:{port}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _get_error(api_server, path, token=None):
    """GET a path that returns an error, catching the HTTPError."""
    _, port = api_server
    url = f"http://127.0.0.1:{port}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    return exc.value


# ---------------------------------------------------------------------------
# Auth tests (standalone, no HTTP server needed)
# ---------------------------------------------------------------------------

class TestBearerAuth:
    """Test API auth module behavior."""

    def test_bearer_auth_missing_header(self):
        """Missing Authorization header -> authenticate returns False."""
        assert _api_server_mod._authenticate(None) is False
        assert _api_server_mod._authenticate("") is False
        assert _api_server_mod._authenticate("Basic abc") is False

    def test_bearer_auth_rejects_wrong_token(self):
        """Wrong token in Authorization header -> authenticate returns False."""
        assert _api_server_mod._authenticate("Bearer wrongtoken123") is False

    def test_bearer_auth_accepts_correct_token(self):
        """Correct token -> authenticate returns True."""
        assert _api_server_mod._authenticate(f"Bearer {_FAKE_TOKEN}") is True

    def test_validate_token_missing(self):
        """validate_token returns (False, error) when token is missing."""
        with mock.patch.dict(os.environ, {"FARMSURV_API_TOKEN": ""}, clear=False):
            valid, msg = _api_server_mod._validate_token()
        assert valid is False
        assert "required" in msg.lower()

    def test_validate_token_too_short(self):
        """validate_token returns (False, error) when token < 32 chars."""
        with mock.patch.dict(os.environ, {"FARMSURV_API_TOKEN": "short"}, clear=False):
            valid, msg = _api_server_mod._validate_token()
        assert valid is False
        assert "short" in msg.lower()

    def test_validate_token_valid(self):
        """validate_token returns (True, token) for a valid token."""
        with mock.patch.dict(os.environ, {"FARMSURV_API_TOKEN": _FAKE_TOKEN}, clear=False):
            valid, token = _api_server_mod._validate_token()
        assert valid is True
        assert len(token) == 40

    def test_constant_time_compare_same(self):
        assert _api_server_mod._constant_time_compare("abc", "abc") is True

    def test_constant_time_compare_diff(self):
        assert _api_server_mod._constant_time_compare("abc", "abd") is False

    def test_constant_time_compare_diff_length(self):
        assert _api_server_mod._constant_time_compare("abc", "abcd") is False

    def test_constant_time_compare_empty(self):
        assert _api_server_mod._constant_time_compare("", "") is True


# ---------------------------------------------------------------------------
# Health endpoint test
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """Test /healthz endpoint."""

    def test_healthz(self, api_server):
        """/healthz returns HTTP 200 with ok, box_id, api_enabled."""
        _, port = api_server
        url = f"http://127.0.0.1:{port}/healthz"

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())

        assert data["ok"] is True
        assert data["box_id"] == "test-box-01"
        assert data["api_enabled"] is True


# ---------------------------------------------------------------------------
# Route tests (via real HTTP server)
# ---------------------------------------------------------------------------

class TestRoutes:
    """Test all 17 API routes via real HTTP requests."""

    def _check_ok(self, api_server, path, name=None):
        """GET a route and verify 200 + box_id."""
        data = _get(api_server, path, token=_FAKE_TOKEN)
        assert "box_id" in data
        assert data["box_id"] == "test-box-01"

    # --- cameras ---
    def test_route_cameras(self, api_server):
        self._check_ok(api_server, "/api/v1/cameras")

    def test_route_camera_health(self, api_server):
        self._check_ok(api_server, "/api/v1/cameras/FRONT/health")

    def test_route_camera_snapshot(self, api_server):
        """Snapshot returns 200 when a snapshot file exists on disk."""
        from pathlib import Path
        # Mock get_camera_snapshot to return a real file path
        fake_snap = Path("/tmp/test_snapshot_front.png")
        fake_snap.write_bytes(b"fake-png")
        with mock.patch(
            "infra.farm_api.cameras.get_camera_snapshot",
            return_value=fake_snap,
        ):
            data = _get(api_server, "/api/v1/cameras/FRONT/snapshot", token=_FAKE_TOKEN)
        assert "box_id" in data
        assert data["box_id"] == "test-box-01"
        assert "snapshot_path" in data
        fake_snap.unlink(missing_ok=True)

    # --- events ---
    def test_route_events(self, api_server):
        self._check_ok(api_server, "/api/v1/events")

    def test_route_events_with_limit(self, api_server):
        self._check_ok(api_server, "/api/v1/events?limit=10")

    def test_route_events_with_since(self, api_server):
        self._check_ok(api_server, "/api/v1/events?since=2026-01-01T00:00:00")

    def test_route_event_by_id(self, api_server):
        """GET /api/v1/events/<id> returns 200 when event exists."""
        from dataclasses import dataclass

        @dataclass
        class FakeEvent:
            alert_id: str = "test-alert-id-000"
            camera_id: str = "FRONT"
            timestamp: str = "2026-09-23T12:00:00"
            confidence: float = 0.95

        fake = FakeEvent()
        with mock.patch(
            "infra.farm_api.events.get_event",
            return_value=fake,
        ):
            data = _get(api_server, "/api/v1/events/test-alert-id-000", token=_FAKE_TOKEN)
        assert "box_id" in data
        assert data["box_id"] == "test-box-01"
        assert "data" in data
        assert data["data"]["alert_id"] == "test-alert-id-000"

    # --- gate ---
    def test_route_gate(self, api_server):
        self._check_ok(api_server, "/api/v1/gate")

    def test_route_suppression_rules(self, api_server):
        self._check_ok(api_server, "/api/v1/gate/suppression-rules")

    # --- cooldowns ---
    def test_route_cooldowns(self, api_server):
        self._check_ok(api_server, "/api/v1/cooldowns")

    # --- identities ---
    def test_route_identities_people(self, api_server):
        self._check_ok(api_server, "/api/v1/identities/people")

    def test_route_identities_vehicles(self, api_server):
        self._check_ok(api_server, "/api/v1/identities/vehicles")

    def test_route_identities_animals(self, api_server):
        self._check_ok(api_server, "/api/v1/identities/animals")

    # --- listener ---
    def test_route_listener_status(self, api_server):
        self._check_ok(api_server, "/api/v1/listener/status")

    def test_route_listener_errors(self, api_server):
        self._check_ok(api_server, "/api/v1/listener/errors")

    def test_route_listener_errors_with_limit(self, api_server):
        self._check_ok(api_server, "/api/v1/listener/errors?limit=5")

    # --- telegram ---
    def test_route_telegram_status(self, api_server):
        self._check_ok(api_server, "/api/v1/telegram/status")

    # --- vision ---
    def test_route_vision_status(self, api_server):
        self._check_ok(api_server, "/api/v1/vision/status")

    # --- config ---
    def test_route_config(self, api_server):
        self._check_ok(api_server, "/api/v1/config")

    # --- non-api route returns 404 ---
    def test_route_non_api_404(self, api_server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(api_server, "/unknown")
        assert exc.value.code == 404


# ---------------------------------------------------------------------------
# Auth tests (via real HTTP server)
# ---------------------------------------------------------------------------

class TestRouteAuth:
    """Test auth enforcement on API routes."""

    def test_auth_missing_token(self, api_server):
        """GET /api/v1/cameras without Authorization -> 401."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(api_server, "/api/v1/cameras")
        assert exc.value.code == 401

    def test_auth_wrong_token(self, api_server):
        """GET /api/v1/cameras with wrong token -> 401."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(api_server, "/api/v1/cameras", token="wrongtoken")
        assert exc.value.code == 401

    def test_auth_valid_token(self, api_server):
        """GET /api/v1/cameras with valid token -> 200."""
        data = _get(api_server, "/api/v1/cameras", token=_FAKE_TOKEN)
        assert "box_id" in data

    def test_auth_short_token(self, api_server):
        """GET with short token -> 401."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(api_server, "/api/v1/cameras", token="abc123")
        assert exc.value.code == 401

    def test_healthz_no_auth(self, api_server):
        """/healthz does not require auth."""
        data = _get(api_server, "/healthz")
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# Token validation / startup tests
# ---------------------------------------------------------------------------

class TestTokenValidation:
    """Test that the server refuses to bind when token is invalid."""

    def test_refuses_bind_token_missing(self):
        """Server does not start when token is missing."""
        with mock.patch.dict(os.environ, {
            "FARMSURV_API_ENABLED": "1",
            "FARMSURV_API_TOKEN": "",
            "FARMSURV_API_BIND": "127.0.0.1",
            "FARMSURV_API_PORT": "8092",
            "FARMSURV_BOX_ID": "test",
        }, clear=False):
            valid, msg = _api_server_mod._validate_token()
        assert valid is False
        assert len(msg) > 0

    def test_refuses_bind_token_short(self):
        """Server does not start when token < 32 chars."""
        with mock.patch.dict(os.environ, {
            "FARMSURV_API_TOKEN": "short",
        }, clear=False):
            valid, msg = _api_server_mod._validate_token()
        assert valid is False
        assert "short" in msg.lower()


# ---------------------------------------------------------------------------
# Bind address test
# ---------------------------------------------------------------------------

class TestBindAddress:
    """Test bind address resolution."""

    def test_bind_tailnet_only(self):
        """API_BIND_MODE=tailnet-only -> resolves to 100.64.0.1."""
        with mock.patch.object(_api_server_mod, "_API_BIND_MODE", "tailnet-only"):
            import infra.net_bind
            assert infra.net_bind.resolve_bind_address("tailnet-only") == "100.64.0.1"

    def test_bind_all(self):
        """API_BIND_MODE=all -> resolves to 0.0.0.0."""
        import infra.net_bind
        assert infra.net_bind.resolve_bind_address("all") == "0.0.0.0"

    def test_bind_custom(self):
        """API_BIND_MODE=<ip> -> resolves to that IP."""
        import infra.net_bind
        assert infra.net_bind.resolve_bind_address("10.0.0.1") == "10.0.0.1"


# ---------------------------------------------------------------------------
# Disabled server test
# ---------------------------------------------------------------------------

class TestDisabledServer:
    """Test that disabled server returns None from _start_server."""

    def test_disabled_server_returns_none(self):
        """_start_server returns None when FARMSURV_API_ENABLED=0."""
        with mock.patch.object(_api_server_mod, "_API_ENABLED", False):
            result = _api_server_mod._start_server()
        assert result is None
