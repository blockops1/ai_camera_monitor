"""Regression test for the dual-context import bug in vehicle_pipeline
and person_event_pipeline. Phase 6B.108a shipped listener/_gate_aware_capture.py
with `from listener._gate_aware_capture import ...` calls inside the pipelines,
which FAILED when listener.py runs as `python listener/listener.py` because
Python doesn't treat listener/ as a package in that mode.

This test simulates the production environment (cwd=repo root, listener.py
runs as __main__ script, sys.path[0]=listener/) and verifies that
process_alert and process_person_event can be called without ModuleNotFoundError.

If this test passes via pytest, that's expected — pytest adds repo root to
sys.path and initializes listener as a package, so the package-form import
works. The KEY part of this test is the manual subprocess invocation that
simulates the production entry path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_repo_root = Path(__file__).resolve().parent.parent.parent


def test_process_alert_imports_in_production_environment():
    """Verify process_alert() can call gate_aware_vehicle_capture() under
    the production environment (cwd=repo root, listener.py is __main__).

    Pre-fix: ModuleNotFoundError: 'listener' is not a package.
    Post-fix: bare 'from _gate_aware_capture' works because sys.path[0]
    is the listener/ directory when running `python listener/listener.py`.
    """
    # Run a tiny script that mimics what process_alert does:
    #   cwd = repo root
    #   python -c "import sys; sys.path.insert(0, '<repo_root>/listener');
    #              from vehicle_pipeline import process_alert"
    # That's effectively the production cwd=repo_root / __main__ script
    # combination.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, '{_repo_root}/listener'); "
                "from vehicle_pipeline import process_alert; "
                "print('OK', process_alert.__module__)"
            ),
        ],
        cwd=str(_repo_root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, (
        f"Import failed — production-path broken!\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_process_person_event_imports_in_production_environment():
    """Same regression check for the person pipeline."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, '{_repo_root}/listener'); "
                "from person_event_pipeline import process_person_event; "
                "print('OK', process_person_event.__module__)"
            ),
        ],
        cwd=str(_repo_root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, (
        f"Import failed — production-path broken!\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_dual_context_import_works_in_both_modes():
    """Both import paths must work:
    1. pytest mode (sys.path[0]=repo root, listener is a package)
    2. production mode (sys.path[0]=listener/, listener is __main__ script)

    The dual try/except pattern in the pipelines handles both.
    """
    # Mode 1: pytest (package form) — this is what existing test suite uses
    result_pkg = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, '{_repo_root}'); "
                "from listener._gate_aware_capture import gate_aware_vehicle_capture, "
                "gate_aware_person_capture; "
                "print('PKG_OK')"
            ),
        ],
        cwd=str(_repo_root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result_pkg.returncode == 0, (
        f"Package mode import failed: {result_pkg.stderr}"
    )
    assert "PKG_OK" in result_pkg.stdout

    # Mode 2: production (bare form)
    result_bare = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, '{_repo_root}/listener'); "
                "from _gate_aware_capture import gate_aware_vehicle_capture, "
                "gate_aware_person_capture; "
                "print('BARE_OK')"
            ),
        ],
        cwd=str(_repo_root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result_bare.returncode == 0, (
        f"Bare mode import failed: {result_bare.stderr}"
    )
    assert "BARE_OK" in result_bare.stdout


@pytest.mark.parametrize(
    "module,func_name",
    [
        ("vehicle_pipeline", "process_alert"),
        ("person_event_pipeline", "process_person_event"),
    ],
)
def test_pipeline_module_is_importable_under_both_modes(module, func_name):
    """Both pipeline modules must be importable in both modes."""
    # Package mode (pytest path)
    pkg = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, '{_repo_root}'); "
                f"from listener.{module} import {func_name}; "
                "print('OK_pkg')"
            ),
        ],
        cwd=str(_repo_root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert pkg.returncode == 0, (
        f"Package mode: {pkg.stderr}"
    )

    # Bare mode (production path)
    bare = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, '{_repo_root}/listener'); "
                f"from {module} import {func_name}; "
                "print('OK_bare')"
            ),
        ],
        cwd=str(_repo_root),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert bare.returncode == 0, (
        f"Bare mode: {bare.stderr}"
    )
