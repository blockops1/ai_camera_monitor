"""test_api_hygiene — E2E tests for API server security headers and error shapes.

US-101y.api: Fix API security, healthz probe, and error handling (11 fixes).

E2E strategy:
    - Spawn the listener as a subprocess with FARMSURV_API_ENABLED=1.
    - Send real HTTP requests via urllib (no mocks).
    - Assert response headers, status codes, and JSON bodies for every fix.
    - Each test prints the response body / status code for verification.

Bug classes covered:
    - Missing WWW-Authenticate on 401 responses.
    - Missing Cache-Control on stable vs dynamic endpoints.
    - Non-JSON (HTML) responses on POST/PUT/DELETE/OPTIONS.
    - Missing X-Content-Type-Options nosniff.
    - Python version leaked in Server header.
    - Case-sensitive Bearer scheme (RFC 6750 requires case-insensitive).
    - request_id not echoed in error responses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_TEST_TOKEN = "h" * 40  # 40 chars, well over the 32-char minimum
_TEST_BOX_ID = "test-box-hygiene"
_TEST_PORT = 8096
_WORKSPACE = str(Path(__file__).resolve().parents[1])


def _build_test_env() -> dict:
    """Build an env dict for spawning the listener with API enabled."""
    env = os.environ.copy()
    env["FARMSURV_API_ENABLED"] = "1"
    env["FARMSURV_API_TOKEN"] = _TEST_TOKEN
    env["FARMSURV_API_BIND"] = "127.0.0.1"
    env["FARMSURV_API_PORT"] = str(_TEST_PORT)
    env["FARMSURV_BOX_ID"] = _TEST_BOX_ID
    env["LISTEN_HOST"] = "127.0.0.1"
    env["LISTEN_PORT"] = "0"
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_HOME_CHAT_ID", None)
    env.pop("VISION_LLM_URL", None)
    env["FARMSURV_DATA_DIR"] = os.path.join(_WORKSPACE, "data")
    return env


def _get(url: str, headers: dict | None = None, timeout: int = 5) -> tuple[int, dict, dict]:
    """Send GET and return (status, headers_dict, body_dict)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            hdrs = dict(resp.headers)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"raw": raw}
            return resp.status, hdrs, body
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        hdrs = dict(exc.headers)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return exc.code, hdrs, body


# ---------------------------------------------------------------------------
# Fixture: spawn listener
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _api_server():
    """Start the listener subprocess (API enabled) for the entire test module."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "listener.daemon"],
        cwd=_WORKSPACE,
        env=_build_test_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Wait for API server to be reachable on /healthz
    for _ in range(40):
        try:
            _get(f"http://127.0.0.1:{_TEST_PORT}/healthz")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("API server did not become reachable on /healthz")
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAPIHygiene:
    """E2E tests for API security headers and error shapes."""

    def test_401_includes_www_authenticate(self, _api_server):
        """AC1: 401 response includes WWW-Authenticate: Bearer realm=\"farm-api\"."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/cameras"
        status, hdrs, body = _get(url)
        assert status == 401, f"Expected 401, got {status}"
        www = hdrs.get("WWW-Authenticate", "")
        assert 'Bearer realm="farm-api"' in www, (
            f"WWW-Authenticate missing or wrong: {www!r}"
        )
        print(f"401 WWW-Authenticate: {www}")
        assert body.get("code") == "unauthorized"
        assert "request_id" in body

    def test_401_request_id_in_body(self, _api_server):
        """AC7: request_id is echoed in the 401 response body."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/gate"
        status, _hdrs, body = _get(url)
        assert status == 401
        rid = body.get("request_id", "")
        assert rid, "request_id missing from 401 body"
        assert len(rid) > 0  # should be a UUID
        print(f"401 request_id: {rid}")

    def test_cache_control_on_list_endpoint(self, _api_server):
        """AC2: stable list endpoint (cameras) has Cache-Control: max-age=5, private."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/cameras"
        _status, hdrs, _body = _get(url, {"Authorization": f"Bearer {_TEST_TOKEN}"})
        cc = hdrs.get("Cache-Control", "")
        assert "max-age=5" in cc, f"Expected Cache-Control max-age=5, got {cc!r}"
        print(f"Cache-Control on /api/v1/cameras: {cc}")

    def test_no_cache_on_single_event_endpoint(self, _api_server):
        """AC2: single-event lookup (/api/v1/events/{id}) has Cache-Control: no-store."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/events/00000000-0000-0000-0000-000000000001"
        _status, hdrs, _body = _get(url, {"Authorization": f"Bearer {_TEST_TOKEN}"})
        # Single event lookup returns 404 if event doesn't exist — that's fine,
        # we're testing the Cache-Control header on non-list endpoints
        cc = hdrs.get("Cache-Control", "")
        assert "no-store" in cc, f"Expected Cache-Control no-store, got {cc!r}"
        print(f"Cache-Control on single event lookup: {cc}")

    def test_x_content_type_options_nosniff(self, _api_server):
        """AC4: every response includes X-Content-Type-Options: nosniff."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/cameras"
        _status, hdrs, _body = _get(url, {"Authorization": f"Bearer {_TEST_TOKEN}"})
        xcto = hdrs.get("X-Content-Type-Options", "")
        assert xcto == "nosniff", f"Expected nosniff, got {xcto!r}"
        print(f"X-Content-Type-Options: {xcto}")

    def test_suppressed_server_header(self, _api_server):
        """AC5: Server header does not leak Python version."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/cameras"
        _status, hdrs, _body = _get(url, {"Authorization": f"Bearer {_TEST_TOKEN}"})
        server = hdrs.get("Server", "")
        assert "Python" not in server, (
            f"Server header leaks Python version: {server!r}"
        )
        print(f"Server header: {server!r}")

    def test_bearer_case_insensitive(self, _api_server):
        """AC6: 'bearer' (lowercase) is accepted as well as 'Bearer'."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/cameras"
        status, _hdrs, body = _get(url, {"Authorization": f"bearer {_TEST_TOKEN}"})
        assert status == 200, (
            f"Expected 200 for lowercase 'bearer', got {status}: {body}"
        )
        print(f"Lowercase bearer → 200 (status={status})")

    def test_post_returns_json_405(self, _api_server):
        """AC3: POST to an API endpoint returns JSON 405, not HTML."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/cameras"
        payload = json.dumps({"test": "data"}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_TEST_TOKEN}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read().decode())
        assert status == 405, f"Expected 405, got {status}"
        assert body.get("code") == "method_not_allowed", (
            f"Expected method_not_allowed, got {body}"
        )
        assert "error" in body
        assert "request_id" in body
        print(f"POST 405 body: {json.dumps(body)}")

    def test_put_returns_json_405(self, _api_server):
        """AC3: PUT returns JSON 405."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/gate"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read().decode())
        assert status == 405, f"Expected 405 for PUT, got {status}"
        assert body.get("code") == "method_not_allowed"
        assert "request_id" in body
        print(f"PUT 405 body: {json.dumps(body)}")

    def test_delete_returns_json_405(self, _api_server):
        """AC3: DELETE returns JSON 405."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/cameras"
        req = urllib.request.Request(url, method="DELETE", headers={
            "Authorization": f"Bearer {_TEST_TOKEN}",
        })
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read().decode())
        assert status == 405, f"Expected 405 for DELETE, got {status}"
        assert body.get("code") == "method_not_allowed"
        assert "request_id" in body
        print(f"DELETE 405 body: {json.dumps(body)}")

    def test_options_returns_json_405(self, _api_server):
        """AC3: OPTIONS returns JSON 405."""
        url = f"http://127.0.0.1:{_TEST_PORT}/api/v1/gate"
        req = urllib.request.Request(url, method="OPTIONS", headers={
            "Authorization": f"Bearer {_TEST_TOKEN}",
        })
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = json.loads(exc.read().decode())
        assert status == 405, f"Expected 405 for OPTIONS, got {status}"
        assert body.get("code") == "method_not_allowed"
        assert "request_id" in body
        print(f"OPTIONS 405 body: {json.dumps(body)}")
