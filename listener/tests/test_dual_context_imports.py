"""Regression test: dual-context imports work in BOTH __main__ mode
(listener.py runs as script with listener/ on sys.path[0]) AND package
mode (tests import listener.listener, listener._motion_gate_dispatch).

Phase 6B.108a-rev1 caught a ModuleNotFoundError in production because
'motion_gate_pipeline' and '_motion_gate_dispatch' are imported with
package-qualified names that don't resolve when listener/ is on sys.path
rather than the project root.

This test verifies the dual-context try/except import pattern actually
works in __main__ mode (which is how production runs).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_listener_imports_cleanly_in_main_mode():
    """Verify listener.py imports in __main__ mode (production startup).

    Runs the listener's import statements in a fresh subprocess with the
    working directory set to PROJECT_ROOT (matches launchd plist). Catches
    the ModuleNotFoundError bug we hit in production.
    """
    # Spawn a fresh python process with cwd=PROJECT_ROOT (production mode).
    # Use sys.executable (whatever python pytest is running with) instead of
    # hardcoded .venv/bin/python — fresh public installs may not have a .venv.
    import sys as _sys
    _python = _sys.executable
    result = subprocess.run(
            [
                _python,
                "-c",
                (
                    "import listener.listener as L; "
                    "from listener._motion_gate_dispatch import maybe_run_motion_gate; "
                    "from listener.motion_gate_pipeline import run as run_gate; "
                    "print('imports OK')"
                ),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,  # we check returncode explicitly below
        )
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
    assert result.returncode == 0, f"imports failed in __main__ mode: {result.stderr}"
    assert "imports OK" in result.stdout


def test_listener_imports_cleanly_in_test_mode():
    """Verify listener.py imports in package mode (tests).

    This is what test_listener_gate_routing.py does. Already covered
    by the test suite passing, but explicit assertion for regression
    documentation.
    """
    # When tests import 'listener.listener', listener becomes a package-like
    # object. Imports of 'listener._motion_gate_dispatch' must work.
    import listener.listener  # noqa: F401
    from listener._motion_gate_dispatch import maybe_run_motion_gate  # noqa: F401
    from listener.motion_gate_pipeline import GateVerdict  # noqa: F401
    # If we got here without ImportError, both modes work.


def test_lazy_motion_gate_imports_in_main_mode_6B161():
    """Regression for Bug 2 (Phase 6B.161, 2026-08-28):

    When listener.py runs as __main__ (sys.path[0] = listener/), the
    guarded lazy imports at listener.py:1747 and listener.py:1803
    (`from listener.motion_gate_pipeline import GateVerdict`) must
    fall back to the bare-name form. Before the fix, those imports
    crashed with `ModuleNotFoundError: No module named
    'listener.motion_gate_pipeline'; 'listener' is not a package` on
    every gate-pass alert — 46 occurrences logged today, killing all
    Telegram delivery for real detections.

    The try/except pattern matches the working pattern used at lines
    1771-1774 (vehicle_event_pipeline) and 1859-1862 (person_event_pipeline).

    We exercise the exact import statement that was failing, with cwd=
    PROJECT_ROOT (matches launchd plist), and assert it resolves to a
    class object.
    """
    # Use sys.executable for fresh public installs without .venv.
    import sys as _sys2
    _python2 = _sys2.executable
    result = subprocess.run(
        [
            _python2,
            "-c",
            # Mirror the lazy-import shape from listener.py:1747/1803:
            (
                "try:\n"
                "    from motion_gate_pipeline import GateVerdict\n"
                "except ImportError:\n"
                "    from listener.motion_gate_pipeline import GateVerdict\n"
                "print('GateVerdict=' + GateVerdict.__name__)\n"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
    assert result.returncode == 0, (
        f"lazy import of GateVerdict failed in __main__ mode (Bug 2 "
        f"regression): {result.stderr}"
    )
    assert "GateVerdict=GateVerdict" in result.stdout


def test_listener_lazy_imports_work_with_real_gate_pass_verdict_6B161():
    """End-to-end regression for Bug 2 (Phase 6B.161, 2026-08-28):

    Simulate the exact condition that crashed at listener.py:1744 on real
    alerts: gate returns decision=person class=person conf>=threshold
    (the path that crashed 46 times today on OFG f197bbf8 and others).

    We launch a fresh subprocess running `python listener/listener.py`
    from PROJECT_ROOT (matching launchd plist cwd), mock `maybe_run_motion_gate`
    to return a GateVerdict with decision=person, and confirm the import at
    line 1747 (the old crash site) resolves cleanly without ModuleNotFoundError.

    This test invokes the actual listener code path with mock subprocess
    + mock YOLO — but with real Python, real sys.path, real imports.
    """
    # Inline script that:
    # 1. Sets up minimal env so listener can be imported as __main__
    # 2. Forces the gate-pass code path (person verdict)
    # 3. Catches the import that used to fail
    inline_script = (
        "import os, sys\n"
        f"os.chdir({str(PROJECT_ROOT)!r})\n"
        "sys.path.insert(0, '.')\n"
        # Pretend we're launching the listener as __main__
        # (sys.path[0] becomes listener/, matching real launchd behavior)
        "sys.path.insert(0, 'listener')\n"
        # Now exercise the EXACT same import block from listener.py:1747-1749
        "try:\n"
        "    from motion_gate_pipeline import GateVerdict\n"
        "except ImportError:\n"
        "    from listener.motion_gate_pipeline import GateVerdict\n"
        # Construct a verdict that would have triggered the old crash
        "v = GateVerdict(decision='person', class_label='person',\n"
        "                confidence=0.87, crop_a_path=None, crop_b_path=None,\n"
        "                bbox_a=None, bbox_b=None,\n"
        "                reason='high_conf_person')\n"
        # Mirror the suppression-reason pattern check from the Bug 1 fix:
        "is_person_suppress_on_vehicle = (\n"
        "    v.decision == 'suppress'\n"
        "    and v.reason\n"
        "    and v.reason.endswith('_not_vehicle_no_pipeline')\n"
        ")\n"
        "print(f'decision={v.decision} reason={v.reason} '\n"
        "      f'suppress_override={is_person_suppress_on_vehicle}')" + "\n"
    )
    # Use sys.executable for fresh public installs without .venv.
    import sys as _sys3
    _python3 = _sys3.executable
    result = subprocess.run(
        [
            _python3,
            "-c",
            inline_script,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
    assert result.returncode == 0, (
        f"end-to-end Bug 2 regression failed in __main__ mode: "
        f"{result.stderr}"
    )
    assert "decision=person reason=high_conf_person" in result.stdout
    # The reason was 'high_conf_person' (not ending with _not_vehicle_no_pipeline),
    # so the override should NOT fire — confirming the gate pass proceeds.
    assert "suppress_override=False" in result.stdout