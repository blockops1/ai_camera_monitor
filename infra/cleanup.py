"""cleanup.py — Hourly cleanup of alert artifacts older than 24 hours.

STATUS: stable
THREAD SAFETY: single-threaded (called from daemon thread or launchd cron)

INPUTS:
    - env FARMSURV_DATA_DIR (default $PROJECT_ROOT/data) — data directory root
    - env FARMSURV_PROJECT_ROOT (default ~/farm-surveillance-v2)

OUTPUTS:
    - Log file: CLEANUP_LOG (infra/paths.py)
    - Deletion of alert-scoped directories older than FRAME_RETENTION_HOURS

PUBLIC API:
    run() -> dict
        Scan all alert directories under data/frames/<camera_id>/<alert_id>/,
        delete those whose newest file is older than FRAME_RETENTION_HOURS,
        and respect FRAME_MAX_BYTES (10 GB) disk budget.
        Returns {deleted_dirs: int, freed_bytes: int, errors: int}.

DOES NOT DO:
    - Delete alert JSONL files (ALERTS_DIR) — those have ALERT_RETENTION_DAYS
    - Delete audit logs (AUDIT_LOG_DIR) — those have AUDIT_RETENTION_DAYS
    - Delete vehicle/animal state directories
    - Delete the _sentinels/ sentinel file

CALLS INTO:
    - infra.paths: DATA_DIR, FRAMES_DIR, FRAME_RETENTION_HOURS, FRAME_MAX_BYTES,
      CLEANUP_LOG, ensure_dirs()
    - logging (for cleanup log output)

RELATED:
    - launchd plist: a separate com.farm.surveillance.v2-cleanup.plist can
      call ``python -m infra.cleanup`` hourly; or the daemon can spawn a
      cleanup thread.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from infra import paths

log = logging.getLogger(__name__)
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
log.propagate = False


def _now_epoch() -> float:
    """Return current time as epoch seconds (UTC)."""
    return datetime.now(UTC).timestamp()


def _dir_oldest_mtime(directory: Path) -> float:
    """Return the oldest mtime of any file in this directory tree, or 0 if empty."""
    oldest = float("inf")
    found = False
    for p in directory.rglob("*"):
        if p.is_file():
            m = p.stat().st_mtime
            oldest = min(oldest, m)
            found = True
    return oldest if found else 0.0


def run() -> dict:
    """Scan and prune alert-scoped artifact directories older than retention.

    Iterates over every directory at
    ``DATA_DIR/frames/<camera_id>/<alert_id>/``. For each:

    1. If the directory has no files, it is removed (empty dir).
    2. If the newest file in the directory is older than
       ``FRAME_RETENTION_HOURS``, the entire directory (and empty parents)
       is removed.
    3. If the total disk usage of FRAMES_DIR exceeds ``FRAME_MAX_BYTES``,
       the oldest directories are evicted first regardless of age.

    Returns:
        dict with keys ``deleted_dirs`` (int), ``freed_bytes`` (int),
        ``errors`` (int).
    """
    frames_dir = Path(paths.FRAMES_DIR)
    retention_seconds = paths.FRAME_RETENTION_HOURS * 3600
    now = _now_epoch()
    cutoff = now - retention_seconds

    stats = {"deleted_dirs": 0, "freed_bytes": 0, "errors": 0}

    if not frames_dir.is_dir():
        log.info("cleanup: FRAMES_DIR does not exist (%s); skipping", frames_dir)
        return stats

    # ------------------------------------------------------------------
    # Phase 1: delete empty directories and age-based evictions
    # ------------------------------------------------------------------
    for camera_dir in frames_dir.iterdir():
        if not camera_dir.is_dir() or camera_dir.name.startswith("."):
            continue

        for alert_dir in camera_dir.iterdir():
            if not alert_dir.is_dir():
                continue

            dir_size = sum(
                f.stat().st_size for f in alert_dir.rglob("*") if f.is_file()
            )
            oldest_mtime = _dir_oldest_mtime(alert_dir)

            # Empty directory — remove it
            if oldest_mtime == 0.0:
                alert_dir.rmdir()
                stats["deleted_dirs"] += 1
                # Remove empty camera dir too if it has no other alerts
                if not any(camera_dir.iterdir()):
                    try:
                        camera_dir.rmdir()
                    except OSError:
                        pass
                continue

            # Age-based eviction
            if oldest_mtime < cutoff:
                try:
                    _remove_tree(alert_dir)
                    stats["deleted_dirs"] += 1
                    stats["freed_bytes"] += dir_size
                    # Remove empty camera dir too if it has no other alerts
                    if not any(camera_dir.iterdir()):
                        try:
                            camera_dir.rmdir()
                        except OSError:
                            pass
                except OSError as e:
                    log.error("cleanup: failed to remove %s: %s", alert_dir, e)
                    stats["errors"] += 1

    # ------------------------------------------------------------------
    # Phase 2: disk-budget enforcement — evict oldest if over limit
    # ------------------------------------------------------------------
    if frames_dir.is_dir():
        total_bytes = _total_bytes(frames_dir)
        if total_bytes > paths.FRAME_MAX_BYTES:
            evict_budget = total_bytes - paths.FRAME_MAX_BYTES
            evicted = _evict_by_oldest(frames_dir, evict_budget)
            stats["freed_bytes"] += evicted

    log.info(
        "cleanup: %d dirs, %d bytes freed, %d errors",
        stats["deleted_dirs"],
        stats["freed_bytes"],
        stats["errors"],
    )
    return stats


def _remove_tree(directory: Path) -> None:
    """Recursively remove directory and all contents."""
    for item in directory.rglob("*"):
        if item.is_file() or item.is_symlink():
            item.unlink()
        elif item.is_dir():
            item.rmdir()
    directory.rmdir()


def _total_bytes(directory: Path) -> int:
    """Sum of all file sizes under directory."""
    return sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())


def _evict_by_oldest(
    frames_dir: Path, budget: int
) -> int:
    """Evict oldest alert directories until within budget.

    Returns total bytes freed.
    """
    freed = 0
    # Collect all alert dirs with their oldest mtime and size
    candidates = []
    for cam in frames_dir.iterdir():
        if not cam.is_dir() or cam.name.startswith("."):
            continue
        for alert in cam.iterdir():
            if not alert.is_dir():
                continue
            oldest = _dir_oldest_mtime(alert)
            size = _total_bytes(alert)
            candidates.append((oldest, size, alert))

    # Sort by oldest mtime ascending (oldest first)
    candidates.sort(key=lambda x: x[0])

    for oldest, size, alert_dir in candidates:
        if freed >= budget:
            break
        try:
            _remove_tree(alert_dir)
            freed += size
        except OSError:
            pass

    return freed


if __name__ == "__main__":
    """CLI entry point for launchd-driven cleanup (python -m infra.cleanup)."""
    result = run()
    print(
        f"cleanup complete: dirs={result['deleted_dirs']} "
        f"bytes_freed={result['freed_bytes']} errors={result['errors']}"
    )
