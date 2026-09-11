"""test_cleanup.py — Tests for infra.cleanup.

Tests behaviors:
  1. run() returns zeroed stats when FRAMES_DIR is empty or missing.
  2. run() deletes empty alert directories.
  3. run() deletes age-exceeded alert directories (older than 24h).
  4. run() preserves fresh alert directories (newer than 24h).
  5. run() enforces FRAME_MAX_BYTES disk budget.
"""

import os
import time
from pathlib import Path

import pytest

import infra.paths as _paths_mod
from infra.cleanup import run


@pytest.fixture(autouse=True)
def _patch_project_root(tmp_path):
    """Set FARMSURV_DATA_DIR to tmp_path so cleanup targets tmp_path/data."""
    _paths_mod.DATA_DIR = str(tmp_path / "data")
    _paths_mod.FRAMES_DIR = str(tmp_path / "data" / "frames")
    yield
    # Restore after test
    _paths_mod.DATA_DIR = _paths_mod._DATA_DIR_OVERRIDE or _paths_mod.PROJECT_ROOT + "/data"


def _write_file(path: Path, size: int = 100) -> Path:
    """Write a file with given size bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    return path


def _set_mtime(path: Path, mtime: float) -> None:
    """Set the mtime of a file or directory."""
    os.utime(path, (mtime, mtime))


def _make_alert_dir(cam: str, alert: str, tmp_path: Path, age_hours: float = 0):
    """Create an alert directory with files at a given age.

    Args:
        cam: camera ID.
        alert: alert ID.
        tmp_path: pytest tmp_path fixture.
        age_hours: hours ago the files were created (0 = now).
    """
    base = Path(_paths_mod.FRAMES_DIR) / cam / alert
    base.mkdir(parents=True, exist_ok=True)
    _write_file(base / "crop_a.png", 100)
    _write_file(base / "crop_b.png", 100)
    _write_file(base / "pairwise_diff.png", 200)
    if age_hours > 0:
        mtime = time.time() - (age_hours * 3600)
        for f in base.rglob("*"):
            _set_mtime(f, mtime)
    return base


class TestCleanupRun:
    """Tests for infra.cleanup.run()."""

    def test_run_empty_frames_dir(self, tmp_path):
        """run returns zeroed stats when no alert dirs exist."""
        # Create frames_dir but no camera/alert subdirs
        Path(_paths_mod.FRAMES_DIR).mkdir(parents=True, exist_ok=True)

        result = run()

        assert result["deleted_dirs"] == 0
        assert result["freed_bytes"] == 0
        assert result["errors"] == 0

    def test_run_missing_frames_dir(self, tmp_path):
        """run returns zeroed stats when FRAMES_DIR does not exist."""
        # Don't create FRAMES_DIR at all
        result = run()

        assert result["deleted_dirs"] == 0
        assert result["freed_bytes"] == 0
        assert result["errors"] == 0

    def test_run_deletes_empty_alert_dirs(self, tmp_path):
        """Empty alert directories are removed."""
        frames_dir = Path(_paths_mod.FRAMES_DIR)
        cam_dir = frames_dir / "CAM1"
        alert_dir = cam_dir / "alert-old"
        alert_dir.mkdir(parents=True, exist_ok=True)

        result = run()

        assert result["deleted_dirs"] >= 1
        assert not alert_dir.exists()
        # Camera dir should also be removed (empty now)
        assert not cam_dir.exists()

    def test_run_deletes_age_exceeded_alerts(self, tmp_path):
        """Alert directories older than 24h are deleted."""
        _make_alert_dir("CAM1", "alert-old", tmp_path, age_hours=25)
        _make_alert_dir("CAM1", "alert-fresh", tmp_path, age_hours=1)

        result = run()

        assert result["deleted_dirs"] >= 1
        assert not (Path(_paths_mod.FRAMES_DIR) / "CAM1" / "alert-old").exists()
        # Fresh alert should still exist
        assert (Path(_paths_mod.FRAMES_DIR) / "CAM1" / "alert-fresh").exists()

    def test_run_preserves_fresh_alerts(self, tmp_path):
        """Fresh alert directories (newer than 24h) are preserved."""
        alert_dir = _make_alert_dir("CAM1", "alert-fresh", tmp_path, age_hours=1)

        result = run()

        assert result["deleted_dirs"] == 0
        assert alert_dir.exists()
        assert (alert_dir / "crop_a.png").exists()
        assert (alert_dir / "crop_b.png").exists()
        assert (alert_dir / "pairwise_diff.png").exists()

    def test_run_deletes_empty_camera_dirs(self, tmp_path):
        """When an alert dir is deleted leaving the camera dir empty, the camera dir is removed too."""
        frames_dir = Path(_paths_mod.FRAMES_DIR)
        cam_dir = frames_dir / "CAM1"
        cam_dir.mkdir(parents=True, exist_ok=True)
        alert_dir = cam_dir / "alert-old"
        alert_dir.mkdir(parents=True, exist_ok=True)
        _write_file(alert_dir / "crop_a.png", 100)
        # Set mtime to 48h ago so it exceeds retention
        mtime = time.time() - (48 * 3600)
        for f in alert_dir.rglob("*"):
            _set_mtime(f, mtime)

        result = run()

        assert result["deleted_dirs"] >= 1
        assert not cam_dir.exists()

    def test_run_enforces_disk_budget(self, tmp_path):
        """When total bytes exceed FRAME_MAX_BYTES, oldest dirs are evicted first."""
        from infra import paths

        # Set a tiny budget for testing
        old_max_bytes = paths.FRAME_MAX_BYTES
        paths.FRAME_MAX_BYTES = 300  # 300 bytes budget

        try:
            # Create 3 alert dirs: old (500 bytes), fresh (500 bytes), oldest (500 bytes)
            alert1 = _make_alert_dir("CAM1", "alert-old", tmp_path, age_hours=25)
            alert2 = _make_alert_dir("CAM1", "alert-fresh", tmp_path, age_hours=1)
            alert3 = _make_alert_dir("CAM1", "alert-oldest", tmp_path, age_hours=48)

            # Each has 400 bytes (100+100+200), total 1200 > 300 budget
            for d in [alert1, alert2, alert3]:
                for f in d.rglob("*"):
                    _set_mtime(f, time.time() - (24 * 3600))  # All within 24h window

            result = run()

            assert result["deleted_dirs"] >= 1
            assert result["freed_bytes"] > 0
        finally:
            paths.FRAME_MAX_BYTES = old_max_bytes

    def test_run_skips_hidden_dirs(self, tmp_path):
        """Directories starting with '.' are not touched."""
        frames_dir = Path(_paths_mod.FRAMES_DIR)
        hidden_dir = frames_dir / ".hidden" / "alert"
        hidden_dir.mkdir(parents=True, exist_ok=True)
        _write_file(hidden_dir / "crop_a.png", 100)

        result = run()

        assert result["deleted_dirs"] == 0
        assert hidden_dir.exists()
