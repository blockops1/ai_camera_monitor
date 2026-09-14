"""test_tg_upload_cleanup.py — Tests for infra.tg_upload_cleanup.

Tests behaviors:
  1. sweep() returns zeroed stats when TG_UPLOADS_DIR is empty or missing.
  2. sweep() preserves fresh date directories (today's).
  3. sweep() removes date directories older than yesterday.
  4. sweep() handles non-empty alert subdirs (regression: rglob+rmdir race
     used to hit ENOTEMPTY when parents were yielded before children).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import infra.paths as _paths_mod
import infra.tg_upload_cleanup as mod


@pytest.fixture(autouse=True)
def _patch_tg_uploads_dir(tmp_path):
    """Set TG_UPLOADS_DIR to tmp_path/<tg_uploads> so sweep targets the sandbox."""
    _paths_mod.TG_UPLOADS_DIR = str(tmp_path / "tg_uploads")
    mod.paths.TG_UPLOADS_DIR = _paths_mod.TG_UPLOADS_DIR
    yield
    # No restore needed — next test sets it again.


def _populate_alert(date_dir: Path, alert_id: str, n_files: int = 4) -> None:
    """Create date_dir/<alert_id>/ with n_files fake JPEGs."""
    a = date_dir / alert_id
    a.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (a / f"file_{i:03d}.jpg").write_bytes(b"x" * 64)


def test_sweep_returns_zeroed_when_dir_missing(tmp_path):
    """Missing TG_UPLOADS_DIR is not an error."""
    result = mod.sweep()
    assert result["deleted_dirs"] == 0
    assert result["errors"] == 0


def test_sweep_returns_zeroed_when_dir_empty(tmp_path):
    """Empty TG_UPLOADS_DIR is not an error."""
    Path(_paths_mod.TG_UPLOADS_DIR).mkdir()
    result = mod.sweep()
    assert result["deleted_dirs"] == 0
    assert result["errors"] == 0


def test_sweep_preserves_today(tmp_path):
    """Today's date dir is preserved (cutoff = yesterday)."""
    tg = Path(_paths_mod.TG_UPLOADS_DIR)
    tg.mkdir()
    # cutoff is yesterday, so "today" sorts after it.
    from datetime import UTC, datetime, timedelta
    today = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
    _populate_alert(tg / today, "abc-123", n_files=2)
    result = mod.sweep()
    assert result["deleted_dirs"] == 0
    assert result["errors"] == 0
    assert (tg / today / "abc-123" / "file_000.jpg").exists()


def test_sweep_removes_yesterday_date_dir(tmp_path):
    """A date dir clearly older than 24h is removed entirely."""
    tg = Path(_paths_mod.TG_UPLOADS_DIR)
    tg.mkdir()
    # Anything strictly less than cutoff (_yesterday()) is eligible.
    # Use a date 3 days back to be unambiguous and timezone-robust.
    from datetime import UTC, datetime, timedelta
    old_date = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d")
    _populate_alert(tg / old_date, "abc-123", n_files=4)
    assert (tg / old_date).exists()
    result = mod.sweep()
    assert result["deleted_dirs"] == 1
    assert result["errors"] == 0
    assert not (tg / old_date).exists()


def test_sweep_handles_non_empty_alert_subdirs(tmp_path):
    """REGRESSION: previously failed with ENOTEMPTY when alert subdirs contained
    files (rglob yielded parents before children; manual unlink/rmdir raced).

    Old code: removed all subdirs of date_dir after only unlinking files
    from one alert, then tried to rmdir() non-empty siblings — ENOTEMPTY.

    Fix: shutil.rmtree walks the tree top-down correctly.
    """
    tg = Path(_paths_mod.TG_UPLOADS_DIR)
    tg.mkdir()
    from datetime import UTC, datetime, timedelta
    old_date = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d")
    yd = tg / old_date
    yd.mkdir()
    # 5 alert subdirs, each with 4 JPEGs (matches production cache shape)
    for alert_id in ["aaa", "bbb", "ccc", "ddd", "eee"]:
        _populate_alert(yd, alert_id, n_files=4)
    assert sum(1 for _ in yd.rglob("*.jpg")) == 20  # sanity

    result = mod.sweep()

    assert result["deleted_dirs"] == 1, f"expected 1 dir deleted, got {result['deleted_dirs']}"
    assert result["errors"] == 0, f"expected 0 errors, got {result['errors']}"
    assert not yd.exists(), f"old date dir should be gone: {yd}"
