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
        and respect FARMSURV_FRAME_EVICT_HIGH_BYTES / LOW_BYTES disk budget.
        Returns {deleted_dirs: int, freed_bytes: int, errors: int}.

DOES NOT DO:
    - Delete alert JSONL files (ALERTS_DIR) — those have ALERT_RETENTION_DAYS
    - Delete audit logs (AUDIT_LOG_DIR) — those have AUDIT_RETENTION_DAYS
    - Delete vehicle/animal state directories
    - Delete the _sentinels/ sentinel file

CALLS INTO:
    - infra.paths: DATA_DIR, FRAMES_DIR, FRAME_RETENTION_HOURS,
      FARMSURV_FRAME_EVICT_HIGH_BYTES, FARMSURV_FRAME_EVICT_LOW_BYTES,
      FARMSURV_CLEANUP_ALERT_ENABLED, CLEANUP_LOG, ensure_dirs()
    - infra.telegram_alerts (threshold-crossing / recovery notifications)
    - logging (for cleanup log output)

RELATED:
    - launchd plist: a separate com.farm.surveillance.v2-cleanup.plist can
      call ``python -m infra.cleanup`` hourly; or the daemon can spawn a
      cleanup thread.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from infra import paths, telegram_alerts

log = logging.getLogger(__name__)
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
log.propagate = False

# ---------------------------------------------------------------------------
# Runtime state — exposed for health-heartbeat consumers (US-073i).
# ---------------------------------------------------------------------------
last_tick_monotonic: float | None = None
last_reclaimed_bytes: int | None = None

# ---------------------------------------------------------------------------
# Hysteresis state (US-104c).
# _was_over_high: True when we previously saw disk >= HIGH threshold,
#   False/None when we were below HIGH.
# _was_under_low: True when we previously saw disk <= LOW threshold,
#   False/None when we were above LOW.
# ---------------------------------------------------------------------------
_was_over_high: bool | None = None
_was_under_low: bool | None = None


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


def _human_gb(n_bytes: int) -> str:
    """Return a human-friendly GB string (1 decimal)."""
    return f"{n_bytes / 1e9:.1f}"


def run() -> dict:
    """Scan and prune alert-scoped artifact directories older than retention.

    Iterates over every directory at
    ``DATA_DIR/frames/<camera_id>/<alert_id>/``. For each:

    1. If the directory has no files, it is removed (empty dir).
    2. If the newest file in the directory is older than
       ``FRAME_RETENTION_HOURS``, the entire directory (and empty parents)
       is removed.
    3. If the total disk usage of FRAMES_DIR exceeds
       ``FARMSURV_FRAME_EVICT_HIGH_BYTES``, the oldest directories are evicted
       until total <= ``FARMSURV_FRAME_EVICT_LOW_BYTES`` (one batch per run).

    Hysteresis (US-104c): Telegram alerts fire only on state *transitions*,
    not on every tick:
      - under_low → at_or_above_high: ⚠️ "Cleanup threshold crossed" + evict
      - at_or_above_high → under_low: ✅ "Cleanup recovered"
      - any other: silent on Telegram, daemon.log only

    Returns:
        dict with keys ``deleted_dirs`` (int), ``freed_bytes`` (int),
        ``errors`` (int).
    """
    global _was_over_high, _was_under_low, last_tick_monotonic, last_reclaimed_bytes

    # ------------------------------------------------------------------
    # Kill switch — FARMSURV_CLEANUP_DISABLED
    # ------------------------------------------------------------------
    if paths.FARMSURV_CLEANUP_DISABLED:
        log.info("cleanup: disabled by FARMSURV_CLEANUP_DISABLED, skipping")
        return {"deleted_dirs": 0, "freed_bytes": 0, "errors": 0}

    frames_dir = Path(paths.FRAMES_DIR)
    retention_seconds = paths.FRAME_RETENTION_HOURS * 3600
    now = _now_epoch()
    cutoff = now - retention_seconds

    high_bytes = paths.FARMSURV_FRAME_EVICT_HIGH_BYTES
    low_bytes = paths.FARMSURV_FRAME_EVICT_LOW_BYTES
    alert_enabled = paths.FARMSURV_CLEANUP_ALERT_ENABLED

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
                        log.exception("cleanup: failed to remove empty camera dir %s", camera_dir)
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
                            log.exception("cleanup: failed to remove empty camera dir %s", camera_dir)
                except OSError:
                    log.exception("cleanup: failed to remove alert dir %s", alert_dir)
                    stats["errors"] += 1

    # ------------------------------------------------------------------
    # Phase 2: disk-budget enforcement — evict oldest if over HIGH threshold
    # ------------------------------------------------------------------
    total_bytes = _total_bytes(frames_dir)
    over_high = total_bytes >= high_bytes

    # Capture pre-eviction size for the threshold-crossed log line.
    # total_bytes is reassigned post-cleanup further down, so we snapshot
    # it here while it still holds the value that crossed HIGH.
    pre_eviction_bytes = total_bytes

    if over_high:
        # Evict until total <= LOW (not just down to HIGH — AC7)
        target = low_bytes
        evict_budget = total_bytes - target
        if evict_budget > 0:
            evicted = _evict_by_oldest(frames_dir, evict_budget)
            stats["freed_bytes"] += evicted

        # Log still-over message (daemon.log only). First value is the
        # pre-eviction total that crossed HIGH; second is the HIGH cap;
        # third is the LOW target we evicted down to.
        log.warning(
            "cleanup: disk over HIGH threshold: %s GB (cap %s GB), "
            "evicting down to %s GB",
            _human_gb(pre_eviction_bytes),
            _human_gb(high_bytes),
            _human_gb(low_bytes),
        )

    # ------------------------------------------------------------------
    # Post-cleanup: recompute total bytes for hysteresis state machine
    # ------------------------------------------------------------------
    total_bytes = _total_bytes(frames_dir)
    under_low = total_bytes <= low_bytes

    # ------------------------------------------------------------------
    # Hysteresis state transitions (US-104c).
    #
    # We observe `over_high` BEFORE eviction (the pre-eviction disk state).
    # This is the "threshold crossed" observation.  Eviction happens first,
    # so `under_low` reflects post-eviction state.
    #
    # Transition logic:
    #   under_low → observed_over_high : fire ⚠️ threshold-crossed alert
    #   observed_over_high → under_low : fire ✅ recovery alert
    #   any other : silent (daemon.log only)
    # ------------------------------------------------------------------
    if alert_enabled:
        # Transition: under_low (prev) → observed_over_high (now, before eviction)
        # "The disk has climbed out of the safe band and crossed the high threshold."
        if _was_under_low is True and over_high:
            alert_msg = (
                f"⚠️ Cleanup threshold crossed: {_human_gb(pre_eviction_bytes)} GB "
                f"(cap {_human_gb(high_bytes)}). Evicting down to {_human_gb(low_bytes)} GB."
            )
            telegram_alerts.send(alert_msg)
            log.info("cleanup: Telegram alert sent: %s", alert_msg)

        # Transition: observed_over_high (prev) → under_low (now, after eviction)
        # "The eviction successfully brought disk back into the safe band."
        if _was_over_high is True and under_low:
            alert_msg = (
                f"✅ Cleanup recovered: {_human_gb(total_bytes)} GB "
                f"(below {_human_gb(low_bytes)} GB target)."
            )
            telegram_alerts.send(alert_msg)
            log.info("cleanup: Telegram alert sent: %s", alert_msg)

        # Update state for next tick
        _was_over_high = over_high
        _was_under_low = under_low
    else:
        # Even with alerts disabled, update state so re-enabling picks up correctly
        _was_over_high = over_high
        _was_under_low = under_low

    last_tick_monotonic = time.monotonic()
    last_reclaimed_bytes = stats["freed_bytes"]

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
            log.exception("cleanup: failed to evict alert dir %s", alert_dir)

    return freed


if __name__ == "__main__":
    """CLI entry point for launchd-driven cleanup (python -m infra.cleanup)."""
    result = run()
    print(
        f"cleanup complete: dirs={result['deleted_dirs']} "
        f"bytes_freed={result['freed_bytes']} errors={result['errors']}"
    )
