"""Tests for scripts/gc_stale_tmp.py — stale-tmp garbage collector.

Verifies:
  1. Removes dirs older than the TTL.
  2. Keeps the newest pytest-of-jill dir.
  3. Keeps dirs touched by a live process.
  4. Dry-run mode does not delete.
  5. Skirs dirs that are not stale.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gc_stale_tmp


@pytest.fixture()
def fake_temp_base(tmp_path: Path):
    """Create a fake /private/var/folders structure under tmp_path.

    Layout:
        tmp_path/
            user1/
                T/
                    pytest-of-runner123/  (old)
                    pytest-of-jill/        (newest — should be protected)
                    pm_test_abc/           (leak dir — old)
    """
    base = tmp_path / "var" / "folders" / "user1" / "T"
    base.mkdir(parents=True)
    return base


def _make_dir(base: Path, name: str, age_hours: float = 0) -> Path:
    """Create a directory named *name* and set its mtime to *age_hours* hours ago."""
    p = base / name
    p.mkdir(parents=True, exist_ok=True)
    if age_hours > 0:
        older = time.time() - age_hours * 3600
        os.utime(p, (older, older))
    return p


class TestGcStaleTmpRemovesOldDirs:
    """gc_stale_tmp removes stale dirs older than the TTL."""

    def test_removes_old_pytest_of_dir(self, fake_temp_base, monkeypatch):
        """An old pytest-of-* dir is removed."""
        old_dir = _make_dir(fake_temp_base, "pytest-of-runner456", age_hours=100)
        assert old_dir.exists()

        monkeypatch.setattr(gc_stale_tmp, "TTL_HOURS", 72)
        monkeypatch.setattr(gc_stale_tmp, "TEMP_BASE", str(fake_temp_base.parent.parent))

        results = gc_stale_tmp.gc_pytest_of_dirs(dry_run=False)
        assert len(results) == 1
        assert results[0].path == str(old_dir)
        assert not old_dir.exists()

    def test_removes_old_leak_dir(self, fake_temp_base, monkeypatch):
        """An old pm_* leak dir is removed."""
        leak_dir = _make_dir(fake_temp_base, "pm_test_xyz", age_hours=100)
        assert leak_dir.exists()

        monkeypatch.setattr(gc_stale_tmp, "TTL_HOURS", 72)
        monkeypatch.setattr(gc_stale_tmp, "TEMP_BASE", str(fake_temp_base.parent.parent))

        results = gc_stale_tmp.gc_leak_dirs(dry_run=False)
        assert len(results) == 1
        assert results[0].path == str(leak_dir)
        assert not leak_dir.exists()

    def test_keeps_newest_pytest_of_jill(self, fake_temp_base, monkeypatch):
        """The pytest-of-jill dir is never deleted."""
        jill_dir = _make_dir(fake_temp_base, "pytest-of-jill", age_hours=200)
        assert jill_dir.exists()

        monkeypatch.setattr(gc_stale_tmp, "TTL_HOURS", 72)
        monkeypatch.setattr(gc_stale_tmp, "TEMP_BASE", str(fake_temp_base.parent.parent))

        results = gc_stale_tmp.gc_pytest_of_dirs(dry_run=False)
        assert all("pytest-of-jill" not in r.path for r in results)
        assert jill_dir.exists()

    def test_keeps_fresh_dir(self, fake_temp_base, monkeypatch):
        """A dir younger than the TTL is not removed."""
        fresh_dir = _make_dir(fake_temp_base, "pytest-of-runner789", age_hours=12)

        monkeypatch.setattr(gc_stale_tmp, "TTL_HOURS", 72)
        monkeypatch.setattr(gc_stale_tmp, "TEMP_BASE", str(fake_temp_base.parent.parent))

        results = gc_stale_tmp.gc_pytest_of_dirs(dry_run=False)
        assert len(results) == 0
        assert fresh_dir.exists()

    def test_dry_run_does_not_delete(self, fake_temp_base, monkeypatch):
        """Dry-run mode prints but does not delete."""
        old_dir = _make_dir(fake_temp_base, "pytest-of-runner999", age_hours=100)
        assert old_dir.exists()

        monkeypatch.setattr(gc_stale_tmp, "TTL_HOURS", 72)
        monkeypatch.setattr(gc_stale_tmp, "TEMP_BASE", str(fake_temp_base.parent.parent))

        results = gc_stale_tmp.gc_pytest_of_dirs(dry_run=True)
        assert len(results) == 1
        assert old_dir.exists()  # still exists after dry-run

    def test_keeps_dir_under_live_tmpdir(self, fake_temp_base, monkeypatch):
        """A dir under a live pytest TMPDIR is skipped."""
        live_dir = _make_dir(fake_temp_base, "pytest-of-live123", age_hours=100)

        monkeypatch.setattr(gc_stale_tmp, "TTL_HOURS", 72)
        monkeypatch.setattr(gc_stale_tmp, "TEMP_BASE", str(fake_temp_base.parent.parent))
        monkeypatch.setattr(gc_stale_tmp, "_live_pytest_tmpdirs", lambda: {str(fake_temp_base)})

        results = gc_stale_tmp.gc_pytest_of_dirs(dry_run=False)
        assert len(results) == 0
        assert live_dir.exists()
