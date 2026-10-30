"""farm_api.listener — Listener status resource functions.

STATUS: alpha — V2-068b6
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - launchctl list (PID lookup for the listener process)
    - logs/daemon.log (boot log line for started_at; ERROR lines for recent errors)

OUTPUTS:
    - ListenerStatus dataclass
    - ErrorRecord dataclass
    - get_listener_status() -> ListenerStatus
    - get_recent_errors(n=20) -> list[ErrorRecord]

DOES NOT DO:
    - Modify listener state (read-only introspection)
    - Restart or control the listener
    - Connect to external services (filesystem + launchctl reads)

CALLERS:
    - infra.mcp_server (MCP tool adapters: get_listener_status, get_recent_errors)
    - Any code that needs a read-only view of the listener's health

USAGE:
    from infra.farm_api.listener import get_listener_status, get_recent_errors
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from infra.paths import LOGS_DIR

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListenerStatus:
    """Read-only snapshot of the listener daemon's status.

    Attributes:
        pid: Process ID of the running listener, or None if not found.
        uptime_seconds: Approximate seconds since the listener started.
            Derived from the first log line after boot. 0 if PID not found.
        mode: The mode the listener is running in. Currently always "running"
            when the process is alive.
        started_at: ISO-8601 timestamp from the first boot log entry.
            None if PID not found or boot line missing.
        last_event_at: Timestamp of the most recent event processed,
            or None if no events have occurred yet.
    """

    pid: int | None
    uptime_seconds: float
    mode: str
    started_at: str | None
    last_event_at: str | None


@dataclass(frozen=True)
class ErrorRecord:
    """A single error extracted from the daemon log.

    Attributes:
        timestamp: ISO-8601 timestamp from the log line.
        severity: Always "ERROR" for these records.
        message: The log message (module prefix stripped).
        source: The Python module name that logged the error.
    """

    timestamp: str
    severity: str
    message: str
    source: str

# ---------------------------------------------------------------------------
# Log format
# ---------------------------------------------------------------------------

# Matches: 2026-09-22 20:51:31,035 ERROR module: message text here
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\s+"
    r"(INFO|ERROR|WARNING|DEBUG|CRITICAL)\s+"
    r"([\w.]+):\s+"
    r"(.*)$"
)

# Matches the boot log line: "env load complete: total=N ..."
# or "Booting RTSP readers for N camera(s)..."
_BOOT_LINE_RE = re.compile(
    r"(?:env load complete|Booting RTSP readers|RTSP registry ready|"
    r"cleanup thread started)"
)

# Matches pipeline log lines for event processing:
# "pipeline: dropped alert_id=..." or "pipeline: ..."
_PIPELINE_EVENT_RE = re.compile(
    r"pipeline:\s+"
)

# ---------------------------------------------------------------------------
# PID lookup via launchctl
# ---------------------------------------------------------------------------

_LABEL = "com.farm.surveillance.v2"


def _get_listener_pid() -> int | None:
    """Return the listener PID from launchctl, or None if not running.

    Uses `launchctl list` and parses the PID for the farm label.

    Returns:
        PID as int, or None if the process is not found.
    """
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in result.stdout.splitlines():
            # Format: <PID>\t<exit_status>\t<label>
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].strip() == _LABEL:
                pid_str = parts[0].strip()
                if pid_str and pid_str != "-15":  # -15 = non-zero exit
                    return int(pid_str)
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        log.debug("listener: failed to lookup PID: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


def _get_first_boot_timestamp() -> str | None:
    """Return the timestamp of the first boot-related log line.

    Searches logs/daemon.log for the earliest line matching known boot
    patterns (env load complete, Booting RTSP readers, etc.).

    Returns:
        ISO-8601 timestamp string, or None if no boot line found.
    """
    log_path = Path(LOGS_DIR) / "daemon.log"
    if not log_path.is_file():
        return None

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("listener: failed to read daemon.log: %s", exc)
        return None

    for line in lines:
        if _BOOT_LINE_RE.search(line):
            # Extract timestamp (first 23 chars: "YYYY-MM-DD HH:MM:SS,mmm")
            match = re.match(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})", line)
            if match:
                return match.group(1)
    return None


def _get_last_event_timestamp() -> str | None:
    """Return the timestamp of the most recent pipeline event log line.

    Searches the daemon log for the last line matching pipeline event
    patterns (dropped, ok, etc.).

    Returns:
        ISO-8601 timestamp string, or None if no event lines found.
    """
    log_path = Path(LOGS_DIR) / "daemon.log"
    if not log_path.is_file():
        return None

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("listener: failed to read daemon.log: %s", exc)
        return None

    last_ts = None
    for line in reversed(lines):
        if _PIPELINE_EVENT_RE.search(line):
            match = re.match(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})", line)
            if match:
                last_ts = match.group(1)
                break
    return last_ts


def _parse_boot_ts_to_iso(ts_str: str | None) -> str | None:
    """Convert daemon log timestamp (e.g. '2026-09-22 20:51:31,035') to ISO-8601.

    Args:
        ts_str: Raw timestamp from the log file.

    Returns:
        ISO-8601 string (e.g. '2026-09-22T20:51:31+00:00'), or None.
    """
    if ts_str is None:
        return None
    try:
        # daemon.log uses local time (EDT = UTC-4). We don't have timezone
        # info in the log, so we treat it as UTC for consistency.
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
        return dt.isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_listener_status() -> ListenerStatus:
    """Return a ListenerStatus snapshot of the running listener.

    Reads the PID from launchctl, parses the boot log line for started_at,
    and computes uptime_seconds from the boot timestamp.

    Returns:
        ListenerStatus with current state.
    """
    pid = _get_listener_pid()
    started_at_raw = _get_first_boot_timestamp()
    started_at = _parse_boot_ts_to_iso(started_at_raw)

    last_event_at = _get_last_event_timestamp()
    last_event_at = _parse_boot_ts_to_iso(last_event_at)

    if pid is not None and started_at is not None:
        try:
            started_dt = datetime.fromisoformat(started_at)
            now = datetime.now(UTC)
            uptime_seconds = (now - started_dt).total_seconds()
        except (ValueError, TypeError):
            uptime_seconds = 0
    elif pid is not None:
        # PID found but no boot log line — uptime unknown.
        uptime_seconds = 0
    else:
        pid = None
        started_at = None
        uptime_seconds = 0

    return ListenerStatus(
        pid=pid,
        uptime_seconds=uptime_seconds,
        mode="running" if pid is not None else "stopped",
        started_at=started_at,
        last_event_at=last_event_at,
    )


def get_recent_errors(n: int = 20) -> list[ErrorRecord]:
    """Return the N most recent ERROR lines from the daemon log.

    Parses logs/daemon.log for lines with severity ERROR, extracts the
    timestamp, module name, and message.

    Args:
        n: Maximum number of errors to return (default 20).

    Returns:
        A list of ErrorRecord objects, newest first.
    """
    log_path = Path(LOGS_DIR) / "daemon.log"
    if not log_path.is_file():
        return []

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("listener: failed to read daemon.log: %s", exc)
        return []

    errors: list[ErrorRecord] = []
    for line in reversed(lines):
        if len(errors) >= n:
            break
        match = _LOG_LINE_RE.match(line)
        if match and match.group(2) == "ERROR":
            timestamp_raw = match.group(1)
            source = match.group(3)
            message = match.group(4)

            # Convert to ISO-8601
            timestamp = _parse_boot_ts_to_iso(timestamp_raw) or timestamp_raw

            errors.append(ErrorRecord(
                timestamp=timestamp,
                severity="ERROR",
                message=message,
                source=source,
            ))

    # Return in newest-first order (we iterated reversed, so already correct)
    return errors
