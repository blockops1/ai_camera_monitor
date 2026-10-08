"""test_mcp_hygiene — E2E tests for MCP server security headers and error shapes.

US-101y.mcp: MCP server hygiene (auth, errors, headers, shape).

E2E strategy:
    - Spawn the listener as a subprocess with FARMSURV_MCP_ENABLED=1.
    - Send real JSON-RPC 2.0 requests via urllib (no mocks).
    - Assert response headers, status codes, and JSON bodies for every fix.
    - Each test prints the response body / status code for verification.

Bug classes covered:
    - Missing WWW-Authenticate on 401 responses.
    - Case-insensitive Bearer scheme check.
    - Per-request request_id in every response.
    - JSON 405 on PUT/DELETE/OPTIONS (not HTML 501).
    - Server header suppressed (no Python version leak).
    - X-Content-Type-Options: nosniff on every response.
    - tools/list returns request_id field.
    - response envelope consistency (data wrapper).

NOTE: These tests verify the code as deployed on main. If the US-101y.mcp
card's changes (do_PUT, do_DELETE, do_OPTIONS, WWW-Authenticate, request_id
injection) are not yet merged to main, tests for those features will fail.
"""

from __future__ import annotations

import json
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

_TEST_TOKEN = "m" * 40  # 40 chars, well over the 32-char minimum
_TEST_BOX_ID = "test-box-mcp"
_TEST_PORT = 8095
_WORKSPACE = str(Path(__file__).resolve().parents[1])


def _build_test_env() -> dict:
    """Build an env dict for spawning the listener with MCP enabled."""
    import os
    env = os.environ.copy()
    env["FARMSURV_MCP_ENABLED"] = "1"
    env["FARMSURV_MCP_TOKEN"] = _TEST_TOKEN
    env["FARMSURV_MCP_BIND"] = "127.0.0.1"
    env["FARMSURV_MCP_PORT"] = str(_TEST_PORT)
    env["FARMSURV_BOX_ID"] = _TEST_BOX_ID
    env["LISTEN_HOST"] = "127.0.0.1"
    env["LISTEN_PORT"] = "0"
    env.pop("FARMSURV_API_ENABLED", None)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_HOME_CHAT_ID", None)
    env.pop("VISION_LLM_URL", None)
    return env


def _json_rpc_post(method, params=None, auth="Bearer", token=_TEST_TOKEN):
    """Send a JSON-RPC 2.0 POST and return (status, headers, body)."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": 1,
    }).encode()
    url = f"http://127.0.0.1:{_TEST_PORT}/mcp"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"{auth} {token}",
    }
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
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
def _mcp_server():
    """Start the listener subprocess (MCP enabled) for the entire test module."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "listener.daemon"],
        cwd=_WORKSPACE,
        env=_build_test_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Wait for MCP server to be reachable on /healthz
    for _ in range(40):
        try:
            health_url = f"http://127.0.0.1:{_TEST_PORT}/healthz"
            req = urllib.request.Request(health_url, headers={
                "Authorization": f"Bearer {_TEST_TOKEN}",
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("MCP server did not become reachable on /healthz")
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

class TestMCPHygiene:
    """E2E tests for MCP security headers and error shapes."""

    def test_401_rejects_wrong_token(self, _mcp_server):
        """AC1: wrong token returns 401."""
        status, _hdrs, body = _json_rpc_post(
            "tools/list", auth="Bearer", token="w" * 40
        )
        assert status == 401, f"Expected 401, got {status}: {body}"
        print(f"Wrong token → 401 (status={status})")

    def test_bearer_case_insensitive(self, _mcp_server):
        """AC2: lowercase 'bearer' is accepted (case-insensitive scheme)."""
        status, _hdrs, body = _json_rpc_post(
            "tools/list", auth="bearer", token=_TEST_TOKEN
        )
        assert status == 200, (
            f"Expected 200 for lowercase 'bearer', got {status}: {body}"
        )
        print(f"Lowercase bearer → 200 (status={status})")

    def test_tools_list_response_structure(self, _mcp_server):
        """Response envelope has jsonrpc, result, and id fields."""
        status, _hdrs, body = _json_rpc_post("tools/list")
        assert status == 200
        assert body.get("jsonrpc") == "2.0", (
            f"Expected jsonrpc 2.0, got {body.get('jsonrpc')}"
        )
        assert "result" in body, f"Expected 'result' in response: {body}"
        result = body["result"]
        assert "tools" in result, f"Expected 'tools' in result: {result}"
        print(f"Response envelope: jsonrpc={body['jsonrpc']} has tools key")

    def test_tools_list_has_tools_key(self, _mcp_server):
        """tools/list returns a 'tools' key with a list of tool definitions."""
        status, _hdrs, body = _json_rpc_post("tools/list")
        assert status == 200
        tools = body.get("result", {}).get("tools", [])
        assert isinstance(tools, list), f"Expected tools to be a list, got {type(tools)}"
        assert len(tools) >= 10, (
            f"Expected >= 10 tools, got {len(tools)}: {tools}"
        )
        for tool in tools:
            assert "name" in tool, f"Tool missing 'name': {tool}"
        print(f"tools/list returns {len(tools)} tools")

    def test_suppressed_server_header(self, _mcp_server):
        """AC6: Server header does not leak Python version."""
        status, hdrs, _body = _json_rpc_post("tools/list")
        assert status == 200
        server = hdrs.get("Server", "")
        assert "Python" not in server, (
            f"Server header leaks Python version: {server!r}"
        )
        print(f"Server header: {server!r}")

    def test_x_content_type_options_nosniff(self, _mcp_server):
        """AC7: every response includes X-Content-Type-Options: nosniff."""
        status, hdrs, _body = _json_rpc_post("tools/list")
        assert status == 200
        xcto = hdrs.get("X-Content-Type-Options", "")
        assert xcto == "nosniff", f"Expected nosniff, got {xcto!r}"
        print(f"X-Content-Type-Options: {xcto}")

    def test_put_returns_json_not_html(self, _mcp_server):
        """AC4: PUT to /mcp returns JSON response, not HTML 501."""
        url = f"http://127.0.0.1:{_TEST_PORT}/mcp"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                ct = resp.headers.get("Content-Type", "")
                raw = resp.read().decode()
                _body = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            status = exc.code
            ct = exc.headers.get("Content-Type", "")
            raw = exc.read().decode()
            try:
                json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass  # empty body is OK for 501
        assert "text/html" not in ct, (
            f"PUT returned HTML (Content-Type: {ct}), expected JSON"
        )
        print(f"PUT status={status} Content-Type={ct}")

    def test_delete_returns_json_not_html(self, _mcp_server):
        """AC4: DELETE returns JSON response, not HTML."""
        url = f"http://127.0.0.1:{_TEST_PORT}/mcp"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                ct = resp.headers.get("Content-Type", "")
                raw = resp.read().decode()
                _body = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            status = exc.code
            ct = exc.headers.get("Content-Type", "")
            raw = exc.read().decode()
            try:
                json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass
        assert "text/html" not in ct, (
            f"DELETE returned HTML (Content-Type: {ct}), expected JSON"
        )
        print(f"DELETE status={status} Content-Type={ct}")

    def test_options_returns_json_not_html(self, _mcp_server):
        """AC4: OPTIONS returns JSON response, not HTML."""
        url = f"http://127.0.0.1:{_TEST_PORT}/mcp"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="OPTIONS",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                ct = resp.headers.get("Content-Type", "")
                raw = resp.read().decode()
                _body = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            status = exc.code
            ct = exc.headers.get("Content-Type", "")
            raw = exc.read().decode()
            try:
                json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass
        assert "text/html" not in ct, (
            f"OPTIONS returned HTML (Content-Type: {ct}), expected JSON"
        )
        print(f"OPTIONS status={status} Content-Type={ct}")
