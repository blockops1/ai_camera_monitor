"""E2E: Heartbeat TG-on-fail sends to Telegram when health is unhealthy.

Tests:
1. An unhealthy block (up=False) triggers a TG alert.
2. A healthy block does NOT trigger a TG alert.
3. LISTENER_HEALTH_TG_ON_FAIL=0 suppresses alerts even on failure.

Fixture: a local HTTP server that captures POSTs to verify the payload.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _CaptureHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that captures POST bodies."""

    captured: list[dict] = []  # noqa: RUF012

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        parsed = json.loads(body)
        _CaptureHandler.captured.append(
            {"path": self.path, "body": body.decode(), "json": parsed}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass  # silence logs


@pytest.fixture()
def tg_server():
    """Start a local HTTP server on a free port; return (port, clear_fn)."""
    # Find a free port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]

    server = HTTPServer(("127.0.0.1", port), _CaptureHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    _CaptureHandler.captured.clear()

    yield port, server

    server.shutdown()
    t.join(timeout=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unhealthy_block_triggers_tg_alert(tg_server, monkeypatch):
    """An unhealthy health block causes send_health_alert to fire."""
    _port, server = tg_server
    monkeypatch.setenv("LISTENER_HEALTH_TG_ON_FAIL", "1")
    # infra.__init__.py requires these env vars at import time.
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")
    monkeypatch.setenv("FARMSURV_QUIET_HOURS_ENABLED", "0")
    # telegram_alerts.send() checks for these before calling httpx.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_bot_token_12345678")
    monkeypatch.setenv("TELEGRAM_HOME_CHAT_ID", "123456789")
    monkeypatch.setenv("FARMSURV_API_TOKEN", "fake_api_token_1234567890abcdef")
    monkeypatch.setenv("FARMSURV_MCP_TOKEN", "fake_mcp_token_1234567890abcdef")

    # Patch httpx.Client BEFORE importing health.py so the TG send path
    # uses our fake client that records calls.
    class _FakeResponse:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, *a, **k):
            _CaptureHandler.captured.append(
                {"url": url, "json": k.get("json", {})}
            )
            return _FakeResponse()

    monkeypatch.setattr("httpx.Client", _FakeClient)

    from listener.health import HealthReporter

    hr = HealthReporter(port=8090, env_total=42)

    # Build an unhealthy block (simulates vision_down scenario).
    block = (
        "listener_health: uptime=2h+3m pid=12345 port=8090 env_total=42 "
        "plist_keepalive=True mcp_enabled=True alerts_last_10m=3\n"
        "vision_health: up=False model=unknown base_url=unknown "
        "response_ms=None last_call_age_s=612.3\n"
        "camera_health: id=cam-driveway healthy=True secs_since_last_frame=2.1 "
        "frames=1024 reconnects=0 teardowns=0 last_teardown_age=None\n"
    )

    hr._tg_on_fail(block)

    # Verify send_health_alert called telegram_alerts.send.
    assert len(_CaptureHandler.captured) >= 1, "Expected at least one TG POST"
    last = _CaptureHandler.captured[-1]
    text = last["json"].get("text", "")
    assert "Health ALERT" in text, f"Expected 'Health ALERT' in body, got: {text}"
    assert "up=False" in text, "Expected 'up=False' in body"

    server.shutdown()


def test_healthy_block_no_tg_alert(tg_server, monkeypatch):
    """A healthy health block does NOT trigger a TG alert."""
    _port, server = tg_server
    monkeypatch.setenv("LISTENER_HEALTH_TG_ON_FAIL", "1")
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")
    monkeypatch.setenv("FARMSURV_QUIET_HOURS_ENABLED", "0")

    class _FakeResponse:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, *a, **k):
            _CaptureHandler.captured.append(
                {"url": url, "json": k.get("json", {})}
            )
            return _FakeResponse()

    monkeypatch.setattr("httpx.Client", _FakeClient)

    from listener.health import HealthReporter

    hr = HealthReporter(port=8090, env_total=42)

    block = (
        "listener_health: uptime=2h+3m pid=12345 port=8090 env_total=42 "
        "plist_keepalive=True mcp_enabled=True alerts_last_10m=3\n"
        "vision_health: up=True model=Qwen model base_url=unknown "
        "response_ms=45.2 last_call_age_s=2.1\n"
        "camera_health: id=cam-driveway healthy=True secs_since_last_frame=2.1 "
        "frames=1024 reconnects=0 teardowns=0 last_teardown_age=None\n"
    )

    hr._tg_on_fail(block)

    # No POST should have been made.
    assert len(_CaptureHandler.captured) == 0, (
        f"Expected no TG POST for healthy block, got: {_CaptureHandler.captured}"
    )

    server.shutdown()


def test_tg_on_fail_disabled_no_alert(tg_server, monkeypatch):
    """LISTENER_HEALTH_TG_ON_FAIL=0 suppresses alerts."""
    _port, server = tg_server
    monkeypatch.setenv("LISTENER_HEALTH_TG_ON_FAIL", "0")
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")
    monkeypatch.setenv("FARMSURV_QUIET_HOURS_ENABLED", "0")

    class _FakeResponse:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, *a, **k):
            _CaptureHandler.captured.append(
                {"url": url, "json": k.get("json", {})}
            )
            return _FakeResponse()

    monkeypatch.setattr("httpx.Client", _FakeClient)

    from listener.health import HealthReporter

    hr = HealthReporter(port=8090, env_total=42)

    block = (
        "listener_health: uptime=2h+3m pid=12345 port=8090 env_total=42 "
        "plist_keepalive=True mcp_enabled=True alerts_last_10m=3\n"
        "vision_health: up=False model=unknown base_url=unknown "
        "response_ms=None last_call_age_s=612.3\n"
    )

    hr._tg_on_fail(block)

    # Even though the block is unhealthy, env var disables alerts.
    assert len(_CaptureHandler.captured) == 0

    server.shutdown()
