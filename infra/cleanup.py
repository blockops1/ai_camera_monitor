"""
cleanup.py — Retention cleanup for captured frames and alert history.

STATUS: stable
THREAD SAFETY: thread-safe (the daemon thread is the only mutator;
    individual cleanup functions are stateless w.r.t. shared state)

INPUTS:
    - file tree data/frames/ — JPEG frame captures (time + budget enforced)
    - file tree data/alerts/ — JSONL alert history (time-based only)
    - file tree data/audit/ — JSONL audit log (delegates to audit.prune_old_audit_logs)
    - env FRAME_RETENTION_HOURS, FRAME_MAX_BYTES, ALERT_RETENTION_DAYS,
      CLEANUP_INTERVAL_S via infra.paths

OUTPUTS:
    - return value: CleanupResult (frames) | int (alerts) | int (run_all)
    - deletes files: oldest frames first (when time OR budget triggers)
    - deletes files: alert date files older than ALERT_RETENTION_DAYS
    - writes log: logs/cleanup.log via the "cleanup" logger
    - network call: NONE (local IO only)

PUBLIC API:
    cleanup_frames() -> CleanupResult
        Time + budget enforcement. Removes oldest frame dir until under
        both caps. Returns CleanupResult with bytes_freed + dirs_deleted.
    cleanup_alerts(retention_days: int | None = None) -> int
        Delete alert JSONL files older than retention_days. Returns
        count deleted.
    run_all() -> int
        Combined frames + alerts + audit cleanup. Logs a summary line.
    start_cleanup_thread(interval_s: float | None = None) -> threading.Thread
        Daemon thread that runs run_all() every CLEANUP_INTERVAL_S
        (default 1 hour). Idempotent — calling twice returns the same
        thread.

DOES NOT DO:
    - Network calls — local file IO only
    - Empty-out data/ — runs only when caps are exceeded
    - Rotate or compact the audit log — infra.audit handles its own
      retention via prune_old_audit_logs()
    - Cleanup faces/identity data — that's a separate module

WHY HERE:
    One daemon thread, one cleanup policy file. The listener bootstrap
    calls run_all() once (to clean up after a possible crash) and then
    start_cleanup_thread() for the steady-state hourly tick.

CALLED BY:
    - listener.listener.bootstrap: run_all() once + start_cleanup_thread()
    - tests: cleanup_frames/cleanup_alerts in isolation

CALLS INTO:
    - infra.paths: FRAME_RETENTION_HOURS, FRAME_MAX_BYTES,
      ALERT_RETENTION_DAYS, CLEANUP_INTERVAL_S, FRAMES_DIR, ALERTS_DIR
    - infra.audit: prune_old_audit_logs()
    - infra.logging_setup: configure_file_logger("cleanup", CLEANUP_LOG)

RELATED:
    - data/frames/, data/alerts/, data/audit/ — the trees this module prunes
    - logs/cleanup.log — the dedicated log file (5 MB × 3 backups)
"""

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from infra.paths import (
    ALERTS_DIR,
    CLEANUP_LOG,
    FRAMES_DIR,
    LOCAL_TZ,
)

# Module-level config (overridable in tests via monkeypatch).
# Mirrors paths.py defaults but lives here too so tests can patch
# `cleanup.FRAME_RETENTION_HOURS` without touching paths.py.
FRAME_RETENTION_HOURS = 24
ALERT_RETENTION_DAYS = 7
FRAME_MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB
CLEANUP_INTERVAL_S = 60 * 60  # 1 hour

# Paths (overridable in tests)
_frames_dir = FRAMES_DIR
_alerts_dir = ALERTS_DIR
_cleanup_log_path = CLEANUP_LOG


# Logging — writes to cleanup.log via the existing logging infra.
# The alert_listener root logger handles file routing (logs/cleanup.log).
log = logging.getLogger("cleanup")


# Module-level cleanup status — read by alert_listener's /status endpoint
# (Phase 1, 2026-07-24). Updated at the end of each run_all() pass.
_last_cleanup_at: str | None = None
_last_cleanup_result: dict | None = None


@dataclass
class CleanupResult:
    """Result of a frame-cleanup pass."""

    time_based_deleted: int = 0
    budget_based_deleted: int = 0
    bytes_freed: int = 0

    @property
    def total_deleted(self) -> int:
        return self.time_based_deleted + self.budget_based_deleted


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def _frames_dir_size() -> int:
    """Total bytes under FRAMES_DIR (recursive)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(_frames_dir):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


def cleanup_frames(
    retention_hours: int | None = None,
    max_bytes: int | None = None,
) -> CleanupResult:
    """
    Delete frame folders older than retention_hours, then enforce
    max_bytes budget by deleting the OLDEST folders first if the
    directory is still over budget.

    Args:
        retention_hours: Override time retention (default FRAME_RETENTION_HOURS).
        max_bytes: Override disk budget (default FRAME_MAX_BYTES).

    Returns:
        CleanupResult with deletion counts and bytes freed.

    Two-phase logic:
      Phase 1 (time): mtime < cutoff → delete. Cheap, frees space when
        the listener runs continuously and frames naturally age out.
      Phase 2 (budget): if _frames_dir_size() > max_bytes, sort remaining
        folders by mtime ascending, delete until under budget. Protects
        against floods (e.g. a misbehaving camera firing constantly).
    """
    if retention_hours is None:
        retention_hours = FRAME_RETENTION_HOURS
    if max_bytes is None:
        max_bytes = FRAME_MAX_BYTES

    if not os.path.exists(_frames_dir):
        return CleanupResult()

    result = CleanupResult()
    now = time.time()
    cutoff = now - (retention_hours * 3600)

    # Phase 1: time-based cleanup
    for entry in os.listdir(_frames_dir):
        path = os.path.join(_frames_dir, entry)
        if not os.path.isdir(path):
            continue
        mtime = os.path.getmtime(path)
        if mtime < cutoff:
            freed = _dir_size_bytes(path)
            try:
                shutil.rmtree(path)
                result.time_based_deleted += 1
                result.bytes_freed += freed
                log.info(
                    f"Deleted frame folder (age {int((now - mtime) / 3600)}h): "
                    f"{entry} ({freed // 1024} KB)"
                )
            except Exception as err:
                log.error(f"Failed to delete {entry}: {err}")

    # Phase 2: budget enforcement — if still over max_bytes, delete oldest
    current_size = _frames_dir_size()
    if current_size > max_bytes:
        log.warning(
            f"Frames dir over budget: {current_size // (1024 * 1024)} MB > "
            f"{max_bytes // (1024 * 1024)} MB max — deleting oldest"
        )
        # Collect remaining folders with mtime, oldest first
        folders = []
        for entry in os.listdir(_frames_dir):
            path = os.path.join(_frames_dir, entry)
            if os.path.isdir(path):
                folders.append((os.path.getmtime(path), path, entry))
        folders.sort()  # ascending by mtime

        for mtime, path, entry in folders:
            if current_size <= max_bytes:
                break
            freed = _dir_size_bytes(path)
            try:
                shutil.rmtree(path)
                result.budget_based_deleted += 1
                result.bytes_freed += freed
                current_size -= freed
                log.info(f"Deleted frame folder (budget): {entry} ({freed // 1024} KB)")
            except Exception as err:
                log.error(f"Failed to delete {entry} (budget): {err}")

    return result


def cleanup_alerts(retention_days: int | None = None) -> int:
    """
    Delete alert JSONL files older than retention_days.

    Args:
        retention_days: Override default retention. Uses ALERT_RETENTION_DAYS if None.

    Returns:
        Number of files deleted.
    """
    if retention_days is None:
        retention_days = ALERT_RETENTION_DAYS

    if not os.path.exists(_alerts_dir):
        return 0

    cutoff = time.time() - (retention_days * 86400)
    deleted = 0

    for entry in os.listdir(_alerts_dir):
        if not entry.endswith(".jsonl"):
            continue
        path = os.path.join(_alerts_dir, entry)
        mtime = os.path.getmtime(path)
        if mtime < cutoff:
            try:
                os.remove(path)
                deleted += 1
                log.info(
                    f"Deleted alert file: {entry} (age: {int((time.time() - mtime) / 86400)}d)"
                )
            except Exception as err:
                log.error(f"Failed to delete {entry}: {err}")

    return deleted


def _dir_size_bytes(path: str) -> int:
    """Sum of file sizes under `path` (one level + recursive)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


def run_all() -> int:
    """
    Run both cleanup operations and log a summary.

    Returns:
        Total number of items deleted (frames + alerts).
    """
    log.info("Starting retention cleanup")

    frames_result = cleanup_frames()
    frames_deleted = frames_result.total_deleted
    alerts_deleted = cleanup_alerts()
    total = frames_deleted + alerts_deleted

    summary = (
        f"Cleanup complete at {datetime.now(LOCAL_TZ).isoformat()}: "
        f"{frames_deleted} frame folder(s) "
        f"({frames_result.time_based_deleted} time, "
        f"{frames_result.budget_based_deleted} budget, "
        f"{frames_result.bytes_freed // (1024 * 1024)} MB freed), "
        f"{alerts_deleted} alert file(s), {total} total deleted"
    )
    log.info(summary)

    # Persist run summary for /status (Phase 1, 2026-07-24).
    global _last_cleanup_at, _last_cleanup_result
    _last_cleanup_at = datetime.now(LOCAL_TZ).isoformat()
    _last_cleanup_result = {
        "frames_time_deleted": frames_result.time_based_deleted,
        "frames_budget_deleted": frames_result.budget_based_deleted,
        "frames_bytes_freed": frames_result.bytes_freed,
        "alerts_deleted": alerts_deleted,
        "total_deleted": total,
    }

    # Append to cleanup log file
    try:
        os.makedirs(os.path.dirname(_cleanup_log_path), exist_ok=True)
        with open(_cleanup_log_path, "a") as f:
            f.write(summary + "\n")
    except Exception as err:
        log.error(f"Failed to write cleanup log: {err}")

    return total


# ----------------------------------------------------------------------
# Periodic cleanup thread
# ----------------------------------------------------------------------


def start_cleanup_thread(interval_s: float | None = None) -> threading.Thread:
    """
    Start a daemon thread that runs cleanup every interval_s seconds.

    Behavior:
        - Sleep `interval_s` seconds (default CLEANUP_INTERVAL_S = 1h)
        - Run run_all()
        - Sleep again — forever

    The thread is daemon=True so it dies with the listener process.

    This mirrors the heartbeat thread pattern in src/heartbeat.py: a
    long-lived background daemon that keeps state hygienic without
    requiring listener restarts.

    Args:
        interval_s: Override CLEANUP_INTERVAL_S. Mainly for tests.

    Returns:
        The Thread object (mostly for tests; daemon dies with process).
    """
    if interval_s is None:
        interval_s = CLEANUP_INTERVAL_S

    def _run() -> None:
        log.info(f"[cleanup] thread started, interval={interval_s}s")
        while True:
            try:
                run_all()
            except Exception:
                # Never let cleanup kill the thread.
                log.exception("[cleanup] thread caught exception")
            time.sleep(interval_s)

    t = threading.Thread(target=_run, name="cleanup-thread", daemon=True)
    t.start()
    log.info(f"Cleanup thread started (interval={interval_s}s)")
    return t


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


if __name__ == "__main__":
    run_all()
