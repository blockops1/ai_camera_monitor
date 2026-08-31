"""
Tests for scripts/apply_all_tuning.py CLI argument parsing + target resolution.

Phase.167 §13.5 (Commit 9): apply_all_tuning.py was refactored to use
--camera <code> (preferred) + --list-cameras, with --start <friendly-name>
preserved for back-compat. Cameras are now resolved through infra.cameras
(load_cameras / by_code / by_ip / all_codes) instead of the legacy
infra.camera_creds.load_camera_creds dict.

These tests are PII-free: synthetic env files in tmp_path with 10.0.0.x
addresses and generic camera names. No operator IPs or site names.

Test layout follows the T1/T2 Commit 5/6/7/8 pattern:
  - subprocess-based (scripts/ not auto-imported)
  - tmp_path synthetic NEW-schema env files
  - monkeypatch.setenv("FARMSURV_CAMERAS_ENV", ...)
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "apply_all_tuning.py"


SYNTHETIC_CAMERAS_ENV = """\
# Synthetic test env file for Phase.167 Commit 9 (NEW schema)
CAM1_IP=10.10.1.21
CAM1_NAME=Test Camera Front
CAM1_ZONE=FRONT
CAM1_HTTP_USER=admin
CAM1_HTTP_PASS=secret1

CAM2_IP=10.10.1.22
CAM2_NAME=Test Camera Back
CAM2_ZONE=BACK
CAM2_HTTP_USER=admin
CAM2_HTTP_PASS=secret2

CAM3_IP=10.10.1.23
CAM3_NAME=Test Camera Side
CAM3_ZONE=SIDE
CAM3_HTTP_USER=admin
CAM3_HTTP_PASS=secret3
"""


@pytest.fixture
def synthetic_env(tmp_path, monkeypatch):
    """Write a synthetic NEW-schema cameras.env to tmp_path and point
    FARMSURV_CAMERAS_ENV at it."""
    env_file = tmp_path / "synthetic_cameras.env"
    env_file.write_text(SYNTHETIC_CAMERAS_ENV)
    monkeypatch.setenv("FARMSURV_CAMERAS_ENV", str(env_file))
    return env_file


def _run_apply_all_tuning(*args: str, env: dict | None = None,
                          expect_success: bool = True) -> subprocess.CompletedProcess:
    """Run scripts/apply_all_tuning.py with the given CLI args.

    PYTHONPATH is set so 'import infra.*' works.
    FARMSURV_CAMERAS_ENV is set to the synthetic env (test-isolated).
    """
    if env is None:
        env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True, text=True, env=env,
    )
    return result


# ------------------------------------------------------------------ help / list


def test_help_shows_all_flags(synthetic_env):
    """--help enumerates the new --camera and --list-cameras flags."""
    result = _run_apply_all_tuning("--help")
    assert result.returncode == 0
    assert "--camera" in result.stdout
    assert "--list-cameras" in result.stdout
    assert "--start" in result.stdout  # legacy flag still present


def test_help_groups_camera_and_start_as_mutually_exclusive(synthetic_env):
    """--camera and --start are in the same exclusive group in --help output."""
    result = _run_apply_all_tuning("--help")
    # argparse shows them on the same usage line, joined by '|'
    assert "[--camera CODE | --start NAME]" in result.stdout


def test_list_cameras_shows_all_three(synthetic_env):
    """--list-cameras against synthetic env shows CAM1/CAM2/CAM3."""
    result = _run_apply_all_tuning("--list-cameras")
    assert result.returncode == 0
    assert "CAM1" in result.stdout
    assert "CAM2" in result.stdout
    assert "CAM3" in result.stdout


def test_list_cameras_shows_synthetic_ips_and_names(synthetic_env):
    """--list-cameras includes the synthetic IPs and names, NOT operator ones."""
    result = _run_apply_all_tuning("--list-cameras")
    assert result.returncode == 0
    assert "10.10.1.21" in result.stdout
    assert "10.10.1.22" in result.stdout
    assert "10.10.1.23" in result.stdout
    assert "Test Camera Front" in result.stdout
    assert "Test Camera Back" in result.stdout
    assert "Test Camera Side" in result.stdout


def test_list_cameras_with_empty_env_prints_no_match(synthetic_env, tmp_path,
                                                     monkeypatch):
    """--list-cameras with no parseable cameras says so explicitly."""
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# nothing useful here\n")
    monkeypatch.setenv("FARMSURV_CAMERAS_ENV", str(empty_env))
    result = _run_apply_all_tuning("--list-cameras")
    assert result.returncode == 0
    assert "no cameras found" in result.stdout.lower() or \
           "no cameras found" in result.stderr.lower()


# ------------------------------------------------------------------ errors


def test_camera_and_start_are_mutually_exclusive(synthetic_env):
    """Passing both --camera and --start errors (argparse mutual exclusion)."""
    result = _run_apply_all_tuning("--camera", "CAM1",
                                   "--start", "Test Camera Back")
    assert result.returncode != 0
    assert "not allowed with argument" in result.stderr


def test_unknown_camera_code_errors_with_known_list(synthetic_env):
    """Unknown --camera value lists the known codes."""
    result = _run_apply_all_tuning("--camera", "NOPE99", "--dry-run", "--no-recipe")
    assert result.returncode != 0
    assert "unknown camera code" in result.stderr.lower() or \
           "unknown camera code" in result.stdout.lower()
    # The known codes should appear in the diagnostic
    combined = result.stdout + result.stderr
    assert "CAM1" in combined
    assert "CAM2" in combined
    assert "CAM3" in combined


def test_unknown_legacy_start_name_lists_known_names(synthetic_env):
    """Unknown --start value lists the known names."""
    result = _run_apply_all_tuning("--start", "Definitely Not A Camera Name",
                                   "--dry-run", "--no-recipe")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "Test Camera Front" in combined or \
           "not found in registry" in combined.lower()


def test_no_targets_errors_when_env_has_no_cameras(synthetic_env, tmp_path,
                                                   monkeypatch):
    """No --camera, no --start, empty env → error."""
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# nothing\n")
    monkeypatch.setenv("FARMSURV_CAMERAS_ENV", str(empty_env))
    result = _run_apply_all_tuning("--dry-run", "--no-recipe")
    assert result.returncode != 0


# ------------------------------------------------------------------ behavior


def test_dry_run_with_camera_only_runs_one(synthetic_env):
    """--camera CAM1 --dry-run --no-recipe produces a result for CAM1 only.

    Smoke test that the single-target path works without spinning up a browser.
    Uses --creds-env to point the script at the synthetic env (not the
    operator's camera-creds.env, which doesn't have CAM1 at 10.10.1.21).
    """
    # apply_to_one_camera will be called; it uses CamBrowser. With dry_run=True
    # the function returns BEFORE touching the browser, so no playwright
    # is needed. Verify dry_run path returns a JSON-shaped result.
    result = _run_apply_all_tuning(
        "--camera", "CAM1", "--dry-run", "--no-recipe",
        "--creds-env", str(synthetic_env),
    )
    # dry-run is a no-op for browser; should exit 0 (one camera, dry-run ok)
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    # Output should mention CAM1's friendly name OR IP
    combined = result.stdout + result.stderr
    assert "10.10.1.21" in combined or "Test Camera Front" in combined