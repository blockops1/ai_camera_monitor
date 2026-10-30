"""test_no_image_resize -- Regression guard against image resize reintroduction.

Imports the scanner and asserts that the production tree (infra/, listener/,
vehicle_matcher/, telegram_formatter/) contains zero matches.

This test runs after US-019b and US-019c have cleared the resize surface;
it will fail if a future change reintroduces resize, thumbnail, letterbox,
or LANCZOS resampling into production code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCAN_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_no_image_resize.py"
)


@pytest.mark.parametrize("target", [
    pytest.param(None, id="production-tree"),
    pytest.param("infra", id="infra-only"),
    pytest.param("listener", id="listener-only"),
])
def test_no_resize_references(target):
    """The scanner must produce zero hits on the current production tree."""
    cmd = [sys.executable, str(SCAN_SCRIPT)]
    if target is not None:
        cmd.append(str(Path.cwd() / target))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"Scanner found resize references:\n"
        f"{result.stdout}\n{result.stderr}"
    )
