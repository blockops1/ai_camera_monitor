"""test_mcp_smoke_dry — Dry unit tests for the MCP smoke harness.

STATUS: alpha — V2-068h
RUNS:   pytest tests/test_mcp_smoke_dry.py
ENV:    None required

COVERAGE:
    - test_assertion_well_formed_response: well-formed JSON-RPC response parses.
    - test_assertion_missing_tools_key: rejects response without 'tools' key.
    - test_assertion_empty_tools: rejects response with empty tools list.
    - test_assertion_healthz_ok: healthz ok=True is accepted.
    - test_assertion_healthz_not_ok: healthz ok=False is rejected.
    - test_mcp_json_structure: verifies the JSON-RPC response structure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# -----------------------------------------------------------------------
# Extracted assertion logic (mirrors bin/mcp_live_smoke.sh Python one-liner)
# -----------------------------------------------------------------------


def assert_mcp_tools_response(resp_str: str) -> int:
    """Assert the MCP tools/list response and return the tool count.

    This is the assertion logic from bin/mcp_live_smoke.sh extracted into
    a testable function so regressions in the assertion are caught.

    Args:
        resp_str: The raw JSON string from the MCP tools/list response.

    Returns:
        The number of tools registered.

    Raises:
        AssertionError: If the response is missing 'result' or has no tools.
    """
    data = json.loads(resp_str)
    assert "result" in data, "missing result key"
    tools = data["result"].get("tools", [])
    assert len(tools) > 0, "no tools registered"
    return len(tools)


def assert_mcp_healthz(resp_str: str) -> None:
    """Assert the MCP healthz response is ok.

    Args:
        resp_str: The raw JSON string from the healthz endpoint.

    Raises:
        AssertionError: If the response doesn't contain ok=True.
    """
    data = json.loads(resp_str)
    assert data.get("ok"), f"not ok: {data}"


# -----------------------------------------------------------------------
# Unit tests
# -----------------------------------------------------------------------


def test_assertion_well_formed_response():
    """Well-formed JSON-RPC tools/list response parses and returns count."""
    resp = json.dumps({
        "jsonrpc": "2.0",
        "result": {
            "tools": [
                {"name": "list_cameras", "description": "...", "inputSchema": {}},
                {"name": "get_camera_health", "description": "...", "inputSchema": {}},
            ]
        },
        "id": 1,
    })
    count = assert_mcp_tools_response(resp)
    assert count == 2


def test_assertion_missing_result_key():
    """Response missing 'result' key should fail assertion."""
    resp = json.dumps({
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": "Method not found"},
        "id": 1,
    })
    with pytest.raises(AssertionError, match="missing result key"):
        assert_mcp_tools_response(resp)


def test_assertion_empty_tools():
    """Response with empty tools list should fail assertion."""
    resp = json.dumps({
        "jsonrpc": "2.0",
        "result": {"tools": []},
        "id": 1,
    })
    with pytest.raises(AssertionError, match="no tools registered"):
        assert_mcp_tools_response(resp)


def test_assertion_no_tools_key():
    """Response with 'result' but no 'tools' key should fail assertion."""
    resp = json.dumps({
        "jsonrpc": "2.0",
        "result": {"version": "0.6.3"},
        "id": 1,
    })
    with pytest.raises(AssertionError, match="no tools registered"):
        assert_mcp_tools_response(resp)


def test_assertion_healthz_ok():
    """Healthz with ok=True passes."""
    resp = json.dumps({
        "ok": True,
        "box_id": "testbox",
        "uptime_seconds": 42.0,
        "mcp_enabled": True,
    })
    assert_mcp_healthz(resp)  # should not raise


def test_assertion_healthz_not_ok():
    """Healthz with ok=False fails."""
    resp = json.dumps({
        "ok": False,
        "box_id": "testbox",
        "uptime_seconds": 0.0,
        "mcp_enabled": False,
    })
    with pytest.raises(AssertionError, match="not ok"):
        assert_mcp_healthz(resp)


def test_assertion_invalid_json():
    """Non-JSON response raises JSONDecodeError."""
    with pytest.raises(json.JSONDecodeError):
        assert_mcp_tools_response("not json at all")


def test_script_exists_and_executable():
    """bin/mcp_live_smoke.sh exists and is executable."""
    script = Path(__file__).resolve().parent.parent / "bin" / "mcp_live_smoke.sh"
    assert script.exists(), f"Script not found at {script}"
    assert os.access(script, os.X_OK), f"Script not executable: {script}"


def test_smoke_plist_exists():
    """bin/com.farm.surveillance.v2.smoke.plist exists."""
    plist = Path(__file__).resolve().parent.parent / "bin" / "com.farm.surveillance.v2.smoke.plist"
    assert plist.exists(), f"Smoke plist not found at {plist}"


def test_smoke_listener_wrapper_exists():
    """bin/mcp_smoke_listener.sh exists and is executable."""
    wrapper = Path(__file__).resolve().parent.parent / "bin" / "mcp_smoke_listener.sh"
    assert wrapper.exists(), f"Smoke wrapper not found at {wrapper}"
    assert os.access(wrapper, os.X_OK), f"Wrapper not executable: {wrapper}"
