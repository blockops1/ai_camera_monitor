"""tg_upload_cleanup.py — 24h retention sweep for Telegram upload cache.

STATUS: new (US-030a)
THREAD SAFETY: single-threaded (called from daemon boot or watchdog tick)

INPUTS:
    - env FARMSURV_DATA_DIR (default $PROJECT_ROOT/data) — data directory root

OUTPUTS:
    - Deletion of data/tg_uploads/<date>/ directories older than 1 calendar day

PUBLIC API:
    sweep() -> dict
        Scan all date directories under TG_UPLOADS_DIR and remove those
        whose name (YYYY-MM-DD) sorts before yesterday's date.
        Returns {deleted_dirs: int, errors: int}.

DOES NOT DO:
    - Delete data/frames/ — that's infra.cleanup's job
    - Delete individual files (only whole date directories)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from infra import paths

log = logging.getLogger(__name__)
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
log.propagate = False


def _yesterday() -> str:
    """Return yesterday's date as YYYY-MM-DD string."""
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")


def sweep() -> dict:
    """Delete tg_uploads/<date>/ directories older than 24 hours.

    Date directories are compared by name (YYYY-MM-DD is lexicographically
    sortable), so no mtime inspection is needed.

    Returns:
        dict with keys deleted_dirs (int), errors (int).
    """
    tg_dir = Path(paths.TG_UPLOADS_DIR)
    cutoff = _yesterday()

    stats = {"deleted_dirs": 0, "errors": 0}

    if not tg_dir.is_dir():
        log.info(
            "tg_upload_cleanup: TG_UPLOADS_DIR does not exist (%s); skipping", tg_dir
        )
        return stats

    for entry in sorted(tg_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        # YYYY-MM-DD names sort lexicographically; anything before yesterday's
        # date is older than 24 hours.
        if entry.name < cutoff:
            try:
                for item in entry.rglob("*"):
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                    elif item.is_dir():
                        item.rmdir()
                entry.rmdir()
                stats["deleted_dirs"] += 1
                log.info("tg_upload_cleanup: removed date dir %s", entry.name)
            except OSError as exc:
                log.error("tg_upload_cleanup: failed to remove %s: %s", entry, exc)
                stats["errors"] += 1

    if stats["deleted_dirs"] > 0:
        log.info(
            "tg_upload_cleanup: %d dirs removed (cutoff=%s)",
            stats["deleted_dirs"],
            cutoff,
        )
    else:
        log.debug("tg_upload_cleanup: nothing to clean (cutoff=%s)", cutoff)

    return stats
