"""E2E tests for the MCP server (port 8091).

Spawns the real infra.mcp_server on a free port via subprocess, then hits
the JSON-RPC 2.0 endpoint (/mcp) with httpx using bearer-token auth.
Tests tools/list, tools/call (representative tool), and initialize.

Bug classes covered:
    - MCP server starts without FARMSURV_MCP_TOKEN.
    - tools/list returns wrong structure.
    - tools/call returns wrong shape.
    - Auth bypass (no token -> 200).

Acceptance criteria:
  AC1: pytest tests/e2e/test_mcp_server_e2e.py -v passes
  AC2: ruff check tests/e2e/test_mcp_server_e2e.py = 0 errors
  AC3: No mock/MagicMock/patch in this file.
"""

from __future__ import annotations

import json
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
    """Build an env dict with MCP enabled and all required infra vars."""
    env = os.environ.copy()
    env["FARMSURV_MCP_ENABLED"] = "1"
    env["FARMSURV_MCP_TOKEN"] = "y" * 40  # >= 32 chars
    env["FARMSURV_MCP_PORT"] = str(extra.get("port", 18091) if extra else 18091)
    env["FARMSURV_MCP_BIND"] = "all"
    env["FARMSURV_BOX_ID"] = "test-box"
    # Infra-level vars that quick_classifier / quiet_hours require at import time
    env["MOTION_GATE_NIGHT_SUPPRESS_ENABLED"] = "1"
    env["FARMSURV_QUIET_HOURS_ENABLED"] = "0"
    env["FARMSURV_BASELINE_OVERRIDE_ENABLED"] = "0"
    env["FARMSURV_API_ENABLED"] = "0"  # API not needed for MCP tests
    env["FARMSURV_FARM_LAT"] = "34.5782"
    env["FARMSURV_FARM_LON"] = "-84.9438"
    env["FARMSURV_GLOBAL_CHANGE_MAX_PCT"] = "60"
    env["FARMSURV_DISTANT_KEYWORDS"] = "***REMOVED***"
    env["BBOX_INTERSECTION_DILATE_PCT"] = "0.01"
    env.pop("FARMSURV_API_TOKEN", None)
    return env


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def mcp_server():
    """Spawn the MCP server on a free port for the entire module.

    (httpx.Client is restored to real by tests/e2e/conftest.py.)
    """
    port = _free_port()
    env = _build_env({"port": port})
    proc = subprocess.Popen(
        [_VENV_PYTHON, "-m", "infra.mcp_server"],
        cwd=_WORKSPACE,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        assert _wait_for_port("127.0.0.1", port, timeout=15), (
            f"MCP server did not start on port {port} within 15s"
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


def _rpc_post(c: httpx.Client, method: str, params: dict | None = None,
              token: str = "y" * 40, id_val: str | int = 1) -> httpx.Response:
    """Send a JSON-RPC 2.0 POST to /mcp."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": id_val,
    }
    if params is not None:
        payload["params"] = params
    return c.post(
        "/mcp",
        content=json.dumps(payload),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )


# -----------------------------------------------------------------------
# Auth tests
# -----------------------------------------------------------------------

class TestAuth:
    """Auth: no token -> 401, wrong token -> 401, valid token -> 200."""

    def test_no_token_returns_401(self, mcp_server):
        with _client(mcp_server["port"]) as c:
            r = c.post("/mcp",
                        content=json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}))
        assert r.status_code == 401

    def test_wrong_token_returns_401(self, mcp_server):
        with _client(mcp_server["port"]) as c:
            r = c.post("/mcp",
                        content=json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}),
                        headers={"Authorization": "Bearer wrongtoken"})
        assert r.status_code == 401

    def test_valid_token_returns_200(self, mcp_server):
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "tools/list", token="y" * 40)
        assert r.status_code == 200

    def test_healthz_no_auth(self, mcp_server):
        """/healthz does not require auth."""
        with _client(mcp_server["port"]) as c:
            r = c.get("/healthz")
        assert r.status_code == 200


# -----------------------------------------------------------------------
# JSON-RPC tests
# -----------------------------------------------------------------------

class TestInitialize:
    """POST /mcp method=initialize -> 200 with server info."""

    def test_initialize_response(self, mcp_server):
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "initialize", {"protocolVersion": "2025-06-18"})
        assert r.status_code == 200
        body = r.json()
        assert body["jsonrpc"] == "2.0"
        assert body["result"]["protocolVersion"] == "2025-06-18"
        assert body["result"]["serverInfo"]["name"] == "farm-surveillance"


class TestToolsList:
    """POST /mcp method=tools/list -> 200, returns array of tool defs."""

    def test_tools_list_returns_array(self, mcp_server):
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "tools/list")
        assert r.status_code == 200
        body = r.json()
        assert "result" in body
        tools = body["result"]["tools"]
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_each_tool_has_required_keys(self, mcp_server):
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "tools/list")
        body = r.json()
        tools = body["result"]["tools"]
        for tool in tools:
            assert "name" in tool, f"Tool missing 'name': {tool}"
            assert "description" in tool, f"Tool missing 'description': {tool}"
            assert "inputSchema" in tool, f"Tool missing 'inputSchema': {tool}"
            assert isinstance(tool["inputSchema"], dict), (
                f"inputSchema must be a dict, got {type(tool['inputSchema'])}"
            )

    def test_tools_list_includes_known_tools(self, mcp_server):
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "tools/list")
        body = r.json()
        tool_names = {t["name"] for t in body["result"]["tools"]}
        expected = {"list_cameras", "get_gate_config", "list_known_people",
                     "get_listener_status", "get_telegram_status", "get_config"}
        missing = expected - tool_names
        assert not missing, f"Expected tools not found: {missing}"


class TestToolsCall:
    """POST /mcp method=tools/call -> 200, representative tool test."""

    def test_call_list_cameras_shape(self, mcp_server):
        """Call list_cameras and assert the response shape.

        MCP _wrap appends box_id to each list item (not a wrapper like the API).
        """
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "tools/call", {"name": "list_cameras", "arguments": {}})
        assert r.status_code == 200
        body = r.json()
        assert body["jsonrpc"] == "2.0"
        assert "result" in body
        # Result is a JSON-stringified list; each item gets box_id appended
        result = json.loads(body["result"]["content"][0]["text"])
        assert isinstance(result, list)
        assert len(result) > 0
        # Each list item has box_id
        assert "box_id" in result[0]

    def test_call_get_gate_config_shape(self, mcp_server):
        """Call get_gate_config and assert the response shape.

        MCP _wrap appends box_id as a field on the dataclass (not a wrapper).
        """
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "tools/call", {"name": "get_gate_config", "arguments": {}})
        assert r.status_code == 200
        body = r.json()
        result = json.loads(body["result"]["content"][0]["text"])
        assert isinstance(result, dict)
        # MCP wraps dataclass fields + appends box_id
        assert "box_id" in result
        assert "motion_threshold" in result

    def test_call_unknown_tool_returns_404(self, mcp_server):
        """Calling an unknown tool name returns 404."""
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "tools/call", {"name": "nonexistent_tool", "arguments": {}})
        assert r.status_code == 404

    def test_call_missing_args_returns_400(self, mcp_server):
        """tools/call without name/arguments returns 400."""
        with _client(mcp_server["port"]) as c:
            r = c.post("/mcp",
                        content=json.dumps({"jsonrpc": "2.0", "method": "tools/call",
                                            "id": 1}),
                        headers={"Authorization": "Bearer " + "y" * 40,
                                 "Content-Type": "application/json"})
        assert r.status_code == 400


class TestUnknownMethod:
    """POST /mcp with unknown method -> 400."""

    def test_unknown_method(self, mcp_server):
        with _client(mcp_server["port"]) as c:
            r = _rpc_post(c, "unknown_method")
        assert r.status_code == 400
        body = r.json()
        assert body["jsonrpc"] == "2.0"
        assert "error" in body
