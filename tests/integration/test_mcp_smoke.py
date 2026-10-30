"""test_mcp_smoke — Smoke harness for the MCP server.

STATUS: alpha — V2-068h
RUNS:   pytest tests/integration/test_mcp_smoke.py
ENV:    RUN_INTEGRATION=1 required (auto-skips otherwise)

COVERAGE:
    - test_mcp_live_smoke: invokes bin/mcp_live_smoke.sh via subprocess,
      asserts exit 0, and checks stdout for the OK line.

DEPENDENCIES: US-068g1 (MCP server spawned inside listener.daemon).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# -----------------------------------------------------------------------
# Skip gate — integration tests require explicit opt-in
# -----------------------------------------------------------------------

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Integration tests require RUN_INTEGRATION=1. "
        "Set the env var and re-run to exercise the live smoke harness.",
        allow_module_level=True,
    )

# -----------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
_SMOKE_SCRIPT = _PROJECT_ROOT / "bin" / "mcp_live_smoke.sh"


def test_mcp_live_smoke():
    """Run bin/mcp_live_smoke.sh and assert exit 0 + OK line.

    Steps:
      1. Execute bin/mcp_live_smoke.sh as a subprocess.
      2. Assert exit code is 0 within 30 seconds.
      3. Assert stdout contains 'OK: N tools registered'.
    """
    assert _SMOKE_SCRIPT.exists(), (
        f"Smoke script not found at {_SMOKE_SCRIPT}. "
        "Ensure bin/mcp_live_smoke.sh exists and is executable."
    )

    result = subprocess.run(
        ["bash", str(_SMOKE_SCRIPT)],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    # Assert exit 0
    assert result.returncode == 0, (
        f"Smoke script exited non-zero (rc={result.returncode}). "
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Assert stdout contains the OK line
    ok_match = re.search(r"OK: \d+ tools registered", result.stdout)
    assert ok_match is not None, (
        f"Expected 'OK: N tools registered' in stdout, got:\n{result.stdout}"
    )

    print(f"Smoke test passed: {ok_match.group()}")
