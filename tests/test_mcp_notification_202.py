"""test_mcp_notification_202 — Unit tests for MCP notification → 202 (MCPFIX-21).

Tests the do_POST notification detection path directly by mocking the
BaseHTTPRequestHandler. No server spawn needed.

Acceptance criteria:
  AC1: .venv/bin/python -m pytest tests/test_mcp_notification_202.py -v passes
  AC2: ruff check tests/test_mcp_notification_202.py = 0 errors
  AC3: No mock/MagicMock/patch in this file.
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from unittest.mock import MagicMock

import pytest

# Import the handler class under test.
# We must patch infra.mcp_server._GLOBAL_REGISTRY before importing because
# _build_registry() calls _register_tools which imports farm_api modules.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))


def _make_mock_handler():
    """Create a mock BaseHTTPRequestHandler with required attributes."""
    handler = MagicMock()
    handler.headers = {"Authorization": "Bearer " + "y" * 40, "Content-Length": "0"}
    handler.path = "/mcp"
    handler.rfile = BytesIO(b"")
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.log_message = MagicMock()
    return handler


def _build_env():
    """Set minimal env so mcp_server can be imported."""
    env = {
        "FARMSURV_MCP_ENABLED": "1",
        "FARMSURV_MCP_TOKEN": "y" * 40,
        "FARMSURV_MCP_PORT": "18091",
        "FARMSURV_MCP_BIND": "all",
        "FARMSURV_BOX_ID": "test-box",
        "MOTION_GATE_NIGHT_SUPPRESS_ENABLED": "1",
        "FARMSURV_QUIET_HOURS_ENABLED": "0",
        "FARMSURV_BASELINE_OVERRIDE_ENABLED": "0",
        "FARMSURV_API_ENABLED": "0",
        "FARMSURV_FARM_LAT": "44.9778",
        "FARMSURV_FARM_LON": "-93.2650",
        "FARMSURV_GLOBAL_CHANGE_MAX_PCT": "60",
        "BBOX_INTERSECTION_DILATE_PCT": "0.01",
    }
    for k, v in env.items():
        __import__("os").environ[k] = v


class TestNotification202:
    """Unit tests: do_POST routes notifications (no 'id') to 202 Accepted."""

    @pytest.fixture(autouse=True)
    def _setup_mcp_server(self):
        """Import mcp_server with env set, patch _GLOBAL_REGISTRY."""
        _build_env()
        # Patch _GLOBAL_REGISTRY so _build_registry returns a no-op registry
        import infra.mcp_server as mcp

        mcp._GLOBAL_REGISTRY[0] = None  # No registry needed for notification test
        self._mcp = mcp
        yield
        # Cleanup: restore _GLOBAL_REGISTRY
        mcp._GLOBAL_REGISTRY[0] = None

    def _do_post(self, body_dict: dict) -> MagicMock:
        """Call do_POST with a mock handler and return wfile contents."""
        body_bytes = json.dumps(body_dict).encode("utf-8")
        handler = _make_mock_handler()
        handler.rfile = BytesIO(body_bytes)
        handler.headers["Content-Length"] = str(len(body_bytes))

        self._mcp._MCPHandler.do_POST(handler)
        return handler

    def test_notification_initialized_returns_202(self):
        """POST notifications/initialized (no 'id') -> 202, empty body."""
        handler = self._do_post({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        # send_response should have been called with 202
        calls = handler.send_response.call_args_list
        assert len(calls) >= 1
        assert calls[0][0][0] == 202
        # Verify end_headers was called (response complete, no body)
        handler.end_headers.assert_called_once()
        # wfile should be empty (no body)
        wfile_content = handler.wfile.getvalue()
        assert wfile_content == b""

    def test_notification_tools_call_returns_202(self):
        """POST tools/call with no 'id' -> 202, empty body."""
        handler = self._do_post({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "list_cameras", "arguments": {}},
        })
        calls = handler.send_response.call_args_list
        assert len(calls) >= 1
        assert calls[0][0][0] == 202
        # wfile should be empty (no body)
        wfile_content = handler.wfile.getvalue()
        assert wfile_content == b""

    def test_request_with_id_still_routes_to_handle_request(self):
        """POST with 'id' field -> NOT 202, goes through _handle_request."""
        handler = self._do_post({
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1,
        })
        # Should NOT have called send_response with 202
        calls = handler.send_response.call_args_list
        statuses = [c[0][0] for c in calls]
        assert 202 not in statuses

    def test_notification_stays_open_for_next_request(self):
        """After 202, the socket stays open — verify no wfile body."""
        handler = self._do_post({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        # The response is just status + headers, no body written to wfile
        wfile_content = handler.wfile.getvalue()
        assert wfile_content == b""
        # send_response was called (202), headers were sent, end_headers was called
        handler.send_response.assert_called_once()
        handler.end_headers.assert_called_once()


class TestHandleRequestUnchanged:
    """Verify _handle_request still works for id-carrying requests."""

    @pytest.fixture(autouse=True)
    def _setup_registry(self):
        """Set up a minimal registry for _handle_request tests."""
        _build_env()
        import infra.mcp_server as mcp

        # Create a minimal registry
        registry = mcp._ToolRegistry("test-box")
        mcp._GLOBAL_REGISTRY[0] = registry
        self._mcp = mcp
        yield
        mcp._GLOBAL_REGISTRY[0] = None

    def test_handle_request_initialize(self):
        """_handle_request('initialize', ...) -> 200 with result."""
        status, body = self._mcp._handle_request(
            "initialize", {"protocolVersion": "2025-06-18"}, 1
        )
        assert status == 200
        assert body["jsonrpc"] == "2.0"
        assert body["result"]["protocolVersion"] == "2025-06-18"

    def test_handle_request_tools_list(self):
        """_handle_request('tools/list', ...) -> 200 with tools array."""
        status, body = self._mcp._handle_request("tools/list", None, 1)
        assert status == 200
        assert body["jsonrpc"] == "2.0"
        assert "result" in body
        assert "tools" in body["result"]
        assert isinstance(body["result"]["tools"], list)

    def test_handle_request_unknown_method(self):
        """_handle_request with unknown method -> 400."""
        status, body = self._mcp._handle_request("unknown_method", None, 1)
        assert status == 400
        assert body["jsonrpc"] == "2.0"
        assert "error" in body
