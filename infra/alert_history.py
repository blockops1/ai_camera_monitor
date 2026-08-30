"""
alert_history.py — Append-only JSONL alert history with quarantine.

STATUS: stable
THREAD SAFETY: uses threading.Lock for in-process safety, fcntl.flock
    for cross-process safety (per Phase 2.1, 2026-08-05)

INPUTS:
    - file data/alerts/YYYY-MM-DD.jsonl (auto-created on first write)
    - function arg alert: dict (required) — alert payload from
      alert_generator.generate_alert()
    - function arg date_str: "YYYY-MM-DD" (required for read)

OUTPUTS:
    - return value: bool (append) | list[dict] (read) | list[str] (list_dates)
    - writes file: data/alerts/YYYY-MM-DD.jsonl (append-only, atomic per line)
    - writes file: data/alerts/.corrupt/<date>-<ts>-<hash>.jsonl
      (malformed lines are quarantined, never silently dropped)
    - mkdir: data/alerts/.corrupt/ (created on first quarantine)

PUBLIC API:
    append_alert(alert: dict) -> bool
        Append one alert to today's file. Thread-safe + process-safe.
    read_alerts(date_str: str) -> list[dict]
        Read all alerts from a date's file. Returns [] for missing file.
        Malformed lines are quarantined and skipped.
    list_dates() -> list[str]
        Return all dates that have alert files (sorted ascending).

DOES NOT DO:
    - Validate the alert schema — caller (alert_generator) is the source
      of structured output; this module is a sink
    - Delete or compact old alert files — infra.cleanup handles retention
    - Emit webhook/notification events — the listener pipeline handles
      side effects after append_alert returns

WHY HERE:
    Phase 2.1 (2026-08-05) added the fcntl.flock guard. Two listener
    instances writing to the same file (rolling restart, gateway
    misconfig) would corrupt it on the first concurrent write; the
    flock gives kernel-level mutual exclusion. The quarantine-on-
    parse-failure pattern is the same one audit.py uses — bad data
    goes to a sibling .corrupt/ dir, never silently disappears.

CALLED BY:
    - listener.listener: append_alert() at end of _process_alert()
    - tests: read_alerts() and list_dates() to assert pipeline output

CALLS INTO:
    - infra.paths: ALERTS_DIR for the date-bucket root
    - threading.Lock: in-process guard
    - fcntl.flock: cross-process guard
    - os, json, hashlib, datetime: standard IO + parse + dedupe

RELATED:
    - infra.cleanup: prunes old date files via cleanup_alerts()
    - data/alerts/YYYY-MM-DD.jsonl — the files this module writes
"""

import datetime
import hashlib
import json
import logging
import os
import threading

from infra.paths import ALERTS_DIR, LOCAL_TZ

log = logging.getLogger(__name__)


# Internal: tests monkeypatch this
_alerts_dir = ALERTS_DIR

# Single write lock — multiple threads may call append_alert concurrently.
# NOTE: this is process-local. Cross-process safety comes from fcntl.flock
# in _safe_write_line() below.
_write_lock = threading.Lock()

# Corrupt-line stash directory (lazy-created on first corruption).
# Phase 2.1b — partial lines are MOVED here instead of dropped, so a
# future forensic analysis can recover what was lost.
_CORRUPT_DIR = os.path.join(_alerts_dir, ".corrupt")


def _today() -> str:
    """Return YYYY-MM-DD for today (overridable in tests)."""
    return datetime.datetime.now(LOCAL_TZ).date().isoformat()


def _safe_write_line(filepath: str, line: str) -> None:
    """Write one line with cross-process exclusive lock (fcntl.flock).

    On Linux/macOS, flock is acquired per-process (each open(2) gets an
    independent file description) — two processes opening the same file
    serialize on the kernel's lock table. On Windows this would be a
    no-op (BLOCKING=False, fcntl is unavailable) but the in-process
    threading.Lock still serializes threads.

    Phase 2.1 (2026-08-05) — closes the silent-corruption failure mode
    if two listeners ever write to the same JSONL (rolling restart,
    gateway misconfig).
    """
    # In-process mutex (always)
    with _write_lock:
        # Open in append mode, line-buffered so the write flushes
        # promptly. flock is released when the file closes.
        fd = os.open(filepath, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            # Try fcntl — POSIX systems (macOS, Linux)
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
                have_flock = True
            except (ImportError, AttributeError):
                # Windows or exotic platform. In-process lock still
                # serializes; cross-process is unprotected (best-effort).
                have_flock = False
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                if have_flock:
                    try:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except Exception as _lock_err:
                        # Best-effort unlock; fd will be closed in the finally below
                        log.debug(f"fcntl unlock failed (will close fd anyway): {_lock_err}")
        finally:
            os.close(fd)


def _quarantine_line(date_str: str, raw_line: str, error: str) -> None:
    """Move a malformed line to data/alerts/.corrupt/ for forensic recovery.

    Filename includes a short hash of the raw bytes so concurrent
    corruptions don't clobber each other. Logs the path so operators
    know where to look.
    """
    try:
        os.makedirs(_CORRUPT_DIR, exist_ok=True)
        # SHA1 here is for filename uniqueness, NOT for security/auth.
        # Bandit B324 wants explicit intent — see docs.
        digest = hashlib.sha1(  # nosec B324 — non-cryptographic digest
            raw_line.encode("utf-8", errors="replace")
        ).hexdigest()[:10]
        ts = datetime.datetime.now(LOCAL_TZ).strftime("%H%M%S")
        out_path = os.path.join(_CORRUPT_DIR, f"{date_str}-{ts}-{digest}.jsonl")
        with open(out_path, "a") as f:
            f.write(raw_line if raw_line.endswith("\n") else raw_line + "\n")
        log.warning(
            f"Quarantined malformed line from {date_str}: {error!r}; "
            f"saved to {out_path}"
        )
    except Exception as err:
        log.warning(f"Failed to quarantine malformed line: {err}")


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def append_alert(alert: dict) -> bool:
    """
    Append an alert to today's JSONL file.

    Args:
        alert: Alert dict matching the Alert Output schema.

    Returns:
        True on success, False on failure.
    """
    today = _today()
    filepath = os.path.join(_alerts_dir, f"{today}.jsonl")

    try:
        os.makedirs(_alerts_dir, exist_ok=True)
        line = json.dumps(alert, separators=(",", ":")) + "\n"

        # Phase 2.1 (2026-08-05) — use flock-protected writer.
        _safe_write_line(filepath, line)

        return True
    except Exception as err:
        log.warning(f"Failed to append alert: {err}")
        return False


def read_alerts(date_str: str) -> list[dict]:
    """
    Read all alerts from a given date's JSONL file.

    Args:
        date_str: Date in YYYY-MM-DD format.

    Returns:
        List of alert dicts (empty list if file doesn't exist).

    Phase 2.1b (2026-08-05) — malformed lines are moved to
    data/alerts/.corrupt/<date>-<hash>.jsonl instead of silently
    dropped, so they can be recovered later if the corruption is
    investigated.
    """
    filepath = os.path.join(_alerts_dir, f"{date_str}.jsonl")

    if not os.path.exists(filepath):
        return []

    alerts = []
    try:
        with open(filepath, "r") as f:
            for line_num, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    alerts.append(json.loads(line))
                except json.JSONDecodeError as err:
                    log.info(f"Skipping malformed line in {date_str}:L{line_num}: {err}")
                    _quarantine_line(date_str, raw.rstrip("\n"), str(err))
                    continue
    except Exception as err:
        log.warning(f"Failed to read {date_str}: {err}")

    return alerts


def list_dates() -> list[str]:
    """
    List all dates that have alert JSONL files.

    Returns:
        Sorted list of date strings (YYYY-MM-DD).
    """
    if not os.path.exists(_alerts_dir):
        return []

    dates = []
    for f in os.listdir(_alerts_dir):
        if f.endswith(".jsonl"):
            dates.append(f[:-6])  # Strip ".jsonl"

    return sorted(dates)
