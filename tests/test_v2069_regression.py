"""Regression test for US-069 import-order bug.

V2-069 night-suppress was broken because `_NIGHT_SUPPRESS_CAMERAS` and
`_NIGHT_SUPPRESS_CLASSES` were computed at module-import time (when
os.environ was empty in the listener) instead of at call time.

This test verifies the fix: import the module in a fresh process where
FARMSURV_NIGHT_SUPPRESS_CAMERAS is set in os.environ but ~/.env has NOT
been loaded — the module must still read the values correctly.

See: https://hermes-agent.nousresearch.com/docs
"""

import os
import subprocess
import sys


def test_night_suppress_reads_env_at_call_time():
    """Fresh Python process: set env var, import quick_classifier, call
    night_suppress_match — must return True without ~/.env being loaded.

    This is the exact production scenario: the listener daemon imports
    infra.quick_classifier at startup (before _load_home_env runs), so
    the module cannot depend on import-time os.environ. The fix replaces
    module-level constants with lazy-read functions.
    """
    # Build a command that sets the env vars and imports the module.
    # We use a subprocess so the process starts clean (no ~/.env loaded).
    code = """
import os
os.environ["FARMSURV_NIGHT_SUPPRESS_CAMERAS"] = "OUTSIDE_BACK_SOLAR"
os.environ["FARMSURV_NIGHT_SUPPRESS_CLASSES"] = "vehicle"

from infra.quick_classifier import night_suppress_match

# Must return True: camera is on the list, class "car" is in "vehicle" bucket.
assert night_suppress_match("OUTSIDE_BACK_SOLAR", "car") is True, \
    "night_suppress_match should return True for suppressed camera+class"
# Must return False: wrong camera.
assert night_suppress_match("OTHER_CAMERA", "car") is False, \
    "night_suppress_match should return False for non-listed camera"
# Must return False: right camera but class not in bucket.
assert night_suppress_match("OUTSIDE_BACK_SOLAR", "person") is False, \
    "night_suppress_match should return False when class not in bucket"
print("PASS")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items()
             if k not in ("FARMSURV_NIGHT_SUPPRESS_CAMERAS",
                          "FARMSURV_NIGHT_SUPPRESS_CLASSES")},
    )
    assert result.returncode == 0, (
        f"Regression test failed:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "PASS" in result.stdout, f"Expected PASS in output, got: {result.stdout}"
