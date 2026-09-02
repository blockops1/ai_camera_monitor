"""Unit tests for ai_camera_monitor/nas_preview.py — Synology NAS preview locator.

Phase 6B.113b (maintainer 2026-08-25): regression test for the
macOS SMB-mount EINTR retry helper. The /preview endpoint 500s when
os.scandir raises InterruptedError spuriously against the SMB mount.
The fix wraps iterdir() in a 3-attempt retry with exponential backoff.

These tests cover:
  - _iterdir_with_retry() normal path (no error)
  - InterruptedError on first call → retry succeeds
  - InterruptedError on first 2 calls → 3rd succeeds
  - InterruptedError on all 3 calls → raises after exhausting retries
  - Other exceptions (FileNotFoundError, PermissionError) propagate
    immediately without retry (those are real errors, not transient)
  - find_preview() end-to-end via tmpdir fake filesystem
"""

from __future__ import annotations

import errno
import sys
from pathlib import Path
from unittest import mock

import pytest

_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from infra.synology_preview import (  # noqa: E402
    _INTERDIR_RETRY_ATTEMPTS,
    _iterdir_with_retry,
)


def _make_eintr():
    """Construct an InterruptedError with errno=4 (EINTR) for realism."""
    return InterruptedError(errno.EINTR, "Interrupted system call")


# ---------------------------------------------------------------------------
# _iterdir_with_retry — the helper itself
# ---------------------------------------------------------------------------


def test_iterdir_with_retry_returns_list_on_success(tmp_path):
    """No error → returns the directory contents as a list."""
    (tmp_path / "a").touch()
    (tmp_path / "b").touch()
    (tmp_path / "c").touch()

    result = _iterdir_with_retry(tmp_path)
    assert len(result) == 3
    assert {p.name for p in result} == {"a", "b", "c"}


def test_iterdir_with_retry_returns_empty_for_empty_dir(tmp_path):
    """Empty directory → returns []."""
    assert _iterdir_with_retry(tmp_path) == []


def test_iterdir_with_retry_recovers_from_one_eintr(tmp_path):
    """First call raises InterruptedError → second succeeds → returns list."""
    (tmp_path / "x").touch()
    real_iterdir = Path.iterdir

    call_count = [0]

    def flaky_iterdir(self):
        call_count[0] += 1
        if call_count[0] == 1:
            raise _make_eintr()
        return real_iterdir(self)

    with mock.patch.object(Path, "iterdir", flaky_iterdir):
        result = _iterdir_with_retry(tmp_path)

    assert call_count[0] == 2  # failed once, succeeded once
    assert len(result) == 1
    assert result[0].name == "x"


def test_iterdir_with_retry_recovers_from_two_eintrs(tmp_path):
    """First 2 calls raise InterruptedError → 3rd succeeds → returns list."""
    (tmp_path / "y").touch()
    real_iterdir = Path.iterdir

    call_count = [0]

    def flaky_iterdir(self):
        call_count[0] += 1
        if call_count[0] < 3:
            raise _make_eintr()
        return real_iterdir(self)

    with mock.patch.object(Path, "iterdir", flaky_iterdir):
        result = _iterdir_with_retry(tmp_path)

    assert call_count[0] == 3
    assert len(result) == 1
    assert result[0].name == "y"


def test_iterdir_with_retry_raises_after_max_attempts(tmp_path):
    """All 3 attempts raise InterruptedError → raises after exhaustion."""
    def always_eintr(self):
        raise _make_eintr()

    with mock.patch.object(Path, "iterdir", always_eintr), pytest.raises(InterruptedError):
        _iterdir_with_retry(tmp_path)


def test_iterdir_with_retry_attempts_constant_is_three():
    """Sanity check on the retry-count constant."""
    assert _INTERDIR_RETRY_ATTEMPTS == 3


def test_iterdir_with_retry_propagates_filenotfounderror_immediately(tmp_path):
    """FileNotFoundError is NOT a transient error — should propagate on
    first attempt without retry."""
    call_count = [0]

    def raise_fnf(self):
        call_count[0] += 1
        raise FileNotFoundError(f"directory vanished: {self}")

    with mock.patch.object(Path, "iterdir", raise_fnf), pytest.raises(FileNotFoundError):
        _iterdir_with_retry(tmp_path)

    # Should only call once — no retry on real errors
    assert call_count[0] == 1


def test_iterdir_with_retry_propagates_notadirectoryerror_immediately(tmp_path):
    """NotADirectoryError is NOT transient — propagate immediately."""
    call_count = [0]

    def raise_nade(self):
        call_count[0] += 1
        raise NotADirectoryError(f"not a directory: {self}")

    with mock.patch.object(Path, "iterdir", raise_nade), pytest.raises(NotADirectoryError):
        _iterdir_with_retry(tmp_path)

    assert call_count[0] == 1


def test_iterdir_with_retry_propagates_permissionerror_immediately(tmp_path):
    """PermissionError is NOT transient — propagate immediately."""
    call_count = [0]

    def raise_perm(self):
        call_count[0] += 1
        raise PermissionError(f"permission denied: {self}")

    with mock.patch.object(Path, "iterdir", raise_perm), pytest.raises(PermissionError):
        _iterdir_with_retry(tmp_path)

    assert call_count[0] == 1


def test_iterdir_with_retry_backoff_timing(tmp_path):
    """Verify the retry sleeps for the documented backoff durations.

    Total time across 3 attempts should be at least 50ms (first retry)
    + 100ms (second retry) = 150ms minimum.
    """
    real_iterdir = Path.iterdir
    call_count = [0]

    def flaky_iterdir(self):
        call_count[0] += 1
        if call_count[0] < 3:
            raise _make_eintr()
        return real_iterdir(self)

    # Mock time.sleep so we don't actually wait, but count the calls
    with (
        mock.patch.object(Path, "iterdir", flaky_iterdir),
        mock.patch("infra.synology_preview.time.sleep") as mock_sleep,
    ):
        _iterdir_with_retry(tmp_path)

    # Should have slept twice (between attempts 1→2 and 2→3)
    assert mock_sleep.call_count == 2
    # First sleep = 50ms, second sleep = 100ms
    assert mock_sleep.call_args_list[0] == mock.call(50 / 1000.0)
    assert mock_sleep.call_args_list[1] == mock.call(100 / 1000.0)


def test_iterdir_with_retry_no_sleep_after_final_attempt(tmp_path):
    """If the LAST attempt succeeds, no sleep after it (we're done)."""
    real_iterdir = Path.iterdir
    call_count = [0]

    def flaky_iterdir(self):
        call_count[0] += 1
        if call_count[0] == 1:
            raise _make_eintr()
        return real_iterdir(self)

    with (
        mock.patch.object(Path, "iterdir", flaky_iterdir),
        mock.patch("infra.synology_preview.time.sleep") as mock_sleep,
    ):
        _iterdir_with_retry(tmp_path)

    # 1 retry happened (attempt 1 failed → sleep → attempt 2 succeeded)
    # So exactly 1 sleep call
    assert mock_sleep.call_count == 1


# ---------------------------------------------------------------------------
# find_preview() end-to-end against a fake Synology directory tree
# ---------------------------------------------------------------------------


def test_find_preview_with_fake_synology_tree(tmp_path, monkeypatch):
    """End-to-end: build a fake @SSRECMETA tree, verify find_preview
    returns the closest file. Uses monkeypatch on SYNOLOGY_ROOT to
    redirect to tmp_path.

    This test also exercises the iterdir-retry integration — we patch
    Path.iterdir so the first call to a day-folder dir raises EINTR,
    then succeeds on retry. find_preview should still return the right
    file.
    """
    import infra.synology_preview as sp

    # Build a minimal Synology tree under tmp_path
    # Structure: SYNOLOGY_ROOT/<Camera>/@SSRECMETA/Preview/<YYYYMMDD{AM,PM}>/<epoch_dir>/<ts>
    # target_ts = 2026-08-25 13:00 EDT → 1787677200 UTC, EDT date 20260825, PM folder
    target_ts = 1787677200  # 2026-08-25 13:00 EDT
    camera_dir = (
        tmp_path / "Test Camera" / "@SSRECMETA" / "Preview" / "20260825PM" / "1787676000"
    )
    camera_dir.mkdir(parents=True)

    # Place 3 preview files (timestamps: t-30, t, t+30, all relative to target_ts)
    file_at_target = camera_dir / str(target_ts)
    file_at_target.touch()
    (camera_dir / str(target_ts - 30)).touch()
    (camera_dir / str(target_ts + 30)).touch()

    monkeypatch.setattr(sp, "SYNOLOGY_ROOT", str(tmp_path))

    # Mock the iterdir calls so the FIRST call to ANY dir raises EINTR,
    # then succeeds. This simulates the macOS SMB mount quirk.
    real_iterdir = Path.iterdir
    call_count = [0]

    def flaky_iterdir(self):
        call_count[0] += 1
        if call_count[0] == 1:
            raise _make_eintr()
        return real_iterdir(self)

    with mock.patch.object(Path, "iterdir", flaky_iterdir):
        result = sp.find_preview("Test Camera", target_ts)

    assert result is not None
    # The file at target_ts has delta=0, which is the smallest possible
    assert result == str(file_at_target)
    # We should have called iterdir at least twice (one retry)
    assert call_count[0] >= 2


def test_find_preview_returns_none_for_unknown_camera(tmp_path, monkeypatch):
    """Unknown camera name → returns None without raising."""
    import infra.synology_preview as sp

    monkeypatch.setattr(sp, "SYNOLOGY_ROOT", str(tmp_path))

    result = sp.find_preview("Nonexistent Camera", 1787677200)
    assert result is None
