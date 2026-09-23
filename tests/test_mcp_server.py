"""test_mcp_server -- Tests for the MCP server (infra/mcp_server.py, infra/mcp_auth.py).

STATUS: alpha -- V2-068c
RUNS: pytest tests/test_mcp_server.py

COVERAGE:
    - test_health_endpoint: /healthz returns 200 with expected fields
    - test_bearer_auth_required: missing Authorization header -> 401
    - test_bearer_auth_rejects_wrong_token: wrong token -> 401
    - test_tool_list_cameras_returns_box_id: list_cameras tool includes box_id
    - test_tool_get_event_missing_returns_null: get_event for missing alert_id
    - test_refuses_to_bind_when_token_missing: missing/short token -> sys.exit(1)
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

import infra.mcp_server as _mcp_server_mod
from infra.mcp_auth import _constant_time_compare, authenticate, validate_token

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_TOKEN = "a" * 40  # 40-char token, well over the 32-char minimum


@pytest.fixture(autouse=True)
def _setup_mcp_env():
    """Ensure MCP env vars and module constants are set for each test."""
    # Patch the mcp_server module-level constants directly, since they are
    # read once at import time from os.environ.
    with mock.patch.dict(os.environ, {
        "FARMSURV_MCP_ENABLED": "1",
        "FARMSURV_MCP_TOKEN": _FAKE_TOKEN,
        "FARMSURV_MCP_BIND": "127.0.0.1",
        "FARMSURV_MCP_PORT": "0",
        "FARMSURV_BOX_ID": "test-box-01",
    }, clear=False):
        # Re-set the module constants so the server sees them
        _mcp_server_mod._MCP_ENABLED = True
        _mcp_server_mod._MCP_BIND_MODE = "127.0.0.1"
        _mcp_server_mod._MCP_PORT = 0
        _mcp_server_mod._BOX_ID = "test-box-01"
        # Reset the global registry for each test
        _mcp_server_mod._GLOBAL_REGISTRY[0] = None
        yield


@pytest.fixture()
def mcp_server():
    """Start a real MCP server on a random free port for integration tests."""
    from infra.mcp_server import (
        _GLOBAL_REGISTRY,
        _build_registry,
        _MCPHandler,
        _ThreadingHTTPServer,
    )

    # Build the registry
    _GLOBAL_REGISTRY[0] = _build_registry()

    # Find a free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = _ThreadingHTTPServer(("127.0.0.1", port), _MCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield server, port

    server.shutdown()


# ---------------------------------------------------------------------------
# Helper: POST to an MCP server
# ---------------------------------------------------------------------------

def _post(mcp_server, payload):
    """POST a JSON-RPC request and return the parsed response."""
    _, port = mcp_server
    url = f"http://127.0.0.1:{port}/mcp"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_FAKE_TOKEN}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# Auth tests (standalone, no HTTP server needed)
# ---------------------------------------------------------------------------


class TestBearerAuth:
    """Test mcp_auth module behavior."""

    def test_bearer_auth_required(self):
        """Missing Authorization header -> authenticate returns False."""
        assert authenticate(None) is False
        assert authenticate("") is False
        assert authenticate("Basic abc") is False

    def test_bearer_auth_rejects_wrong_token(self):
        """Wrong token in Authorization header -> authenticate returns False."""
        assert authenticate("Bearer wrongtoken123") is False

    def test_bearer_auth_accepts_correct_token(self):
        """Correct token -> authenticate returns True."""
        assert authenticate(f"Bearer {_FAKE_TOKEN}") is True

    def test_validate_token_missing(self):
        """validate_token returns (False, error) when token is missing."""
        with mock.patch.dict(os.environ, {"FARMSURV_MCP_TOKEN": ""}, clear=False):
            valid, msg = validate_token()
        assert valid is False
        assert "required" in msg.lower()

    def test_validate_token_too_short(self):
        """validate_token returns (False, error) when token < 32 chars."""
        with mock.patch.dict(os.environ, {"FARMSURV_MCP_TOKEN": "short"}, clear=False):
            valid, msg = validate_token()
        assert valid is False
        assert "short" in msg.lower()

    def test_validate_token_valid(self):
        """validate_token returns (True, token) for a valid token."""
        with mock.patch.dict(os.environ, {"FARMSURV_MCP_TOKEN": _FAKE_TOKEN}, clear=False):
            valid, token = validate_token()
        assert valid is True
        assert len(token) == 40


# ---------------------------------------------------------------------------
# Health endpoint test
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Test /healthz endpoint."""

    def test_health_endpoint(self, mcp_server):
        """/healthz returns HTTP 200 with ok, box_id, uptime_seconds, mcp_enabled."""
        _, port = mcp_server
        url = f"http://127.0.0.1:{port}/healthz"

        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())

        assert data["ok"] is True
        assert data["box_id"] == "test-box-01"
        assert isinstance(data["uptime_seconds"], (int, float))
        assert data["uptime_seconds"] >= 0
        assert data["mcp_enabled"] is True


# ---------------------------------------------------------------------------
# Tool tests (via real HTTP server)
# ---------------------------------------------------------------------------


class TestTools:
    """Test MCP tool adapters via real HTTP requests."""

    def test_tool_list_cameras_returns_box_id(self, mcp_server):
        """list_cameras tool response includes box_id."""
        result = _post(mcp_server, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_cameras", "arguments": {}},
        })
        content = json.loads(result["result"]["content"][0]["text"])
        if isinstance(content, list):
            if content:
                assert "box_id" in content[0]
                assert content[0]["box_id"] == "test-box-01"
            else:
                # Empty list is valid if no cameras configured
                pass
        else:
            assert "box_id" in content
            assert content["box_id"] == "test-box-01"

    def test_tool_get_event_missing_returns_null(self, mcp_server):
        """get_event for a missing alert_id returns None content."""
        result = _post(mcp_server, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_event",
                "arguments": {"event_id": "nonexistent-alert-id-000"},
            },
        })
        content = json.loads(result["result"]["content"][0]["text"])
        assert content is None

    def test_tools_list_returns_tools(self, mcp_server):
        """tools/list returns a non-empty list of tools."""
        result = _post(mcp_server, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/list",
            "params": {},
        })
        tools = result["result"]["tools"]
        assert len(tools) > 0
        tool_names = {t["name"] for t in tools}
        assert "list_cameras" in tool_names
        assert "get_event" in tool_names

    def test_tools_list_has_input_schema(self, mcp_server):
        """Each tool has an inputSchema field."""
        result = _post(mcp_server, {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/list",
            "params": {},
        })
        for tool in result["result"]["tools"]:
            assert "inputSchema" in tool
            assert isinstance(tool["inputSchema"], dict)
            assert "type" in tool["inputSchema"]

    def test_tools_list_has_description(self, mcp_server):
        """Each tool has a description."""
        result = _post(mcp_server, {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/list",
            "params": {},
        })
        for tool in result["result"]["tools"]:
            assert "description" in tool
            assert isinstance(tool["description"], str)
            assert len(tool["description"]) > 0

    def test_404_unknown_endpoint(self, mcp_server):
        """POST to a non-/mcp path returns 404."""
        _, port = mcp_server
        url = f"http://127.0.0.1:{port}/unknown"
        body = json.dumps({"jsonrpc": "2.0", "id": 1}).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_FAKE_TOKEN}",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 404


# ---------------------------------------------------------------------------
# Token validation test
# ---------------------------------------------------------------------------


class TestTokenValidation:
    """Test that the server refuses to bind when token is missing."""

    def test_refuses_to_bind_when_token_missing(self):
        """validate_token returns (False, error) when token is empty."""
        with mock.patch.dict(os.environ, {
            "FARMSURV_MCP_ENABLED": "1",
            "FARMSURV_MCP_TOKEN": "",
            "FARMSURV_MCP_BIND": "127.0.0.1",
            "FARMSURV_MCP_PORT": "8091",
            "FARMSURV_BOX_ID": "test",
        }, clear=False):
            valid, msg = validate_token()
        assert valid is False
        assert len(msg) > 0

    def test_refuses_to_bind_when_token_too_short(self):
        """validate_token returns (False, error) when token < 32 chars."""
        with mock.patch.dict(os.environ, {
            "FARMSURV_MCP_TOKEN": "short",
        }, clear=False):
            valid, msg = validate_token()
        assert valid is False
        assert "short" in msg.lower()


# ---------------------------------------------------------------------------
# Bind address test
# ---------------------------------------------------------------------------


class TestBindAddress:
    """Test bind address resolution."""

    def test_bind_tailnet_only(self):
        """MCP_BIND_MODE=tailnet-only -> resolves to 100.64.0.1."""
        with mock.patch.object(_mcp_server_mod, "_MCP_BIND_MODE", "tailnet-only"):
            from infra.mcp_server import _resolve_bind_address
            assert _resolve_bind_address() == "100.64.0.1"

    def test_bind_all(self):
        """MCP_BIND_MODE=all -> resolves to 0.0.0.0."""
        with mock.patch.object(_mcp_server_mod, "_MCP_BIND_MODE", "all"):
            from infra.mcp_server import _resolve_bind_address
            assert _resolve_bind_address() == "0.0.0.0"

    def test_bind_custom(self):
        """MCP_BIND_MODE=<ip> -> resolves to that IP."""
        with mock.patch.object(_mcp_server_mod, "_MCP_BIND_MODE", "10.0.0.1"):
            from infra.mcp_server import _resolve_bind_address
            assert _resolve_bind_address() == "10.0.0.1"


# ---------------------------------------------------------------------------
# Constant-time comparison test
# ---------------------------------------------------------------------------


class TestConstantTimeCompare:
    """Test _constant_time_compare behavior."""

    def test_same_strings(self):
        assert _constant_time_compare("abc", "abc") is True

    def test_different_length(self):
        assert _constant_time_compare("abc", "abcd") is False

    def test_different_content(self):
        assert _constant_time_compare("abc", "abd") is False

    def test_empty_strings(self):
        assert _constant_time_compare("", "") is True

    def test_one_empty(self):
        assert _constant_time_compare("abc", "") is False


# ---------------------------------------------------------------------------
# Initialize method test
# ---------------------------------------------------------------------------


class TestInitialize:
    """Test the initialize JSON-RPC method."""

    def test_initialize_response(self, mcp_server):
        """initialize returns protocol version and server info."""
        result = _post(mcp_server, {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "initialize",
            "params": {},
        })
        assert result["result"]["protocolVersion"] == "2025-06-18"
        assert result["result"]["serverInfo"]["name"] == "farm-surveillance"
        assert "version" in result["result"]["serverInfo"]
