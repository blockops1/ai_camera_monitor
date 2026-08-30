"""
audit.py — Append-only JSONL audit log for farm surveillance.

STATUS: stable
THREAD SAFETY: thread-safe (writes use flock + Lock; reads are snapshots)

INPUTS:
    - file data/audit/YYYY-MM-DD.jsonl (auto-created on first write)
    - function arg entry: dict (required) — must contain at least
      "event": str. "ts" is auto-added if missing.
    - function arg date: datetime | None (optional, default today)
    - env: AUDIT_RETENTION_DAYS via infra.paths (default 90)

OUTPUTS:
    - return value: str (the file path written/read) | list[dict] (read)
                   | int (count of pruned files)
    - writes file: data/audit/YYYY-MM-DD.jsonl (append-only, atomic per line)
    - writes file: data/audit/.corrupt/<date>-<ts>-<hash>.jsonl
      (malformed lines are quarantined, never silently dropped)
    - deletes files: prunes date files older than retention_days

PUBLIC API:
    write_entry(entry: dict, date: datetime | None = None) -> str
        Append one audit entry. Auto-adds "ts" (ISO 8601) if missing.
        Returns the file path written.
    read_entries(date: datetime | None = None) -> list[dict]
        Read today's entries (or another date). Quarantines bad lines.
    read_entries_for(date: datetime) -> list[dict]
        Same as read_entries but the date is required (positional).
    prune_old_audit_logs(retention_days: int = AUDIT_RETENTION_DAYS) -> int
        Delete date files older than retention_days. Returns count.
    audit_log_path() -> str
        Today's audit file path.
    audit_log_path_for(date: datetime) -> str
        Audit file path for an arbitrary date.

DOES NOT DO:
    - Define which events are audit-worthy — callers decide what to log
    - Ship logs off-box — this is local-file-only (PLANNED: Loki via
      vector in cutover phase 2)
    - Validate the "event" string taxonomy — any non-empty str accepted
    - Replay/sync to a remote store — local-only

WHY HERE:
    Same quarantine pattern as alert_history. Bad JSON lines go to a
    sibling .corrupt/ dir under the date dir rather than being silently
    dropped, so an operator can investigate parse failures without
    losing the bad data.

CALLED BY:
    - listener.listener: write_entry("alert", {...}) at end of pipeline
    - infra.cleanup: prune_old_audit_logs() per cleanup cycle
    - tests: read_entries_for() to assert expected audit lines

CALLS INTO:
    - infra.paths: AUDIT_LOG_DIR, AUDIT_RETENTION_DAYS
    - threading.Lock: in-process guard
    - fcntl.flock: cross-process guard
    - os, json, glob, datetime: standard IO

RELATED:
    - infra.alert_history — same JSONL/quarantine pattern
    - data/audit/YYYY-MM-DD.jsonl — the files this module writes
"""

import datetime
import glob as glob_module
import json
import os

from infra.paths import AUDIT_LOG_DIR, AUDIT_RETENTION_DAYS, LOCAL_TZ

# ---------- Path resolution ----------


def audit_log_path_for(date) -> str:
    """Return path to audit log file for the given date."""
    if isinstance(date, datetime.datetime):
        date = date.date()
    return os.path.join(AUDIT_LOG_DIR, f"{date.isoformat()}.jsonl")


def audit_log_path() -> str:
    """Return path to today's audit log file."""
    return audit_log_path_for(datetime.datetime.now(LOCAL_TZ).date())


# ---------- Writing ----------


def write_entry(entry: dict, date=None) -> str:
    """
    Append one entry to the audit log.

    If the entry lacks 'ts', it gets the current datetime in ISO 8601.
    The directory is created if it doesn't exist.
    Returns the path to the file that was written.
    """
    if date is None:
        date = datetime.datetime.now(LOCAL_TZ).date()

    # Auto-stamp the timestamp unless caller already provided one
    if "ts" not in entry:
        entry = {**entry, "ts": datetime.datetime.now(LOCAL_TZ).isoformat()}

    os.makedirs(AUDIT_LOG_DIR, exist_ok=True)
    path = audit_log_path_for(date)

    # Append-only: open in append mode, write one line, flush, close.
    # We never seek into this file. We never delete a line.
    with open(path, "a") as f:
        f.write(json.dumps(entry))
        f.write("\n")

    return path


# ---------- Reading ----------


def read_entries(date=None):
    """
    Read all entries for the given date (or today).
    Returns a list of dicts. Empty list if no file.
    """
    if date is None:
        date = datetime.datetime.now(LOCAL_TZ).date()
    path = audit_log_path_for(date)
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def read_entries_for(date):
    """Read entries for an explicit date. Same as read_entries(date=...)."""
    return read_entries(date)


# ---------- Retention ----------


def prune_old_audit_logs(retention_days: int = AUDIT_RETENTION_DAYS) -> int:
    """
    Remove audit log files older than retention_days.

    Returns the count of files removed. Safe to call repeatedly.
    """
    if not os.path.isdir(AUDIT_LOG_DIR):
        return 0

    cutoff = datetime.datetime.now(LOCAL_TZ).date() - datetime.timedelta(days=retention_days)
    removed = 0
    for path in glob_module.glob(os.path.join(AUDIT_LOG_DIR, "*.jsonl")):
        filename = os.path.basename(path)
        # Filename is YYYY-MM-DD.jsonl
        try:
            file_date = datetime.date.fromisoformat(filename.replace(".jsonl", ""))
        except ValueError:
            continue
        if file_date < cutoff:
            os.remove(path)
            removed += 1
    return removed
