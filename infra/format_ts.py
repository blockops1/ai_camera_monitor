"""format_ts.py — UTC ISO timestamp → local display string.

Pure module with no circular dependencies.  Imported by listener.daemon
(for the env-var bootstrap) and by telegram_formatter/ display modules.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

PUBLIC API:
    format_local_timestamp(utc_iso: str) -> str
        Convert a UTC ISO-8601 timestamp to a local display string.

DOES NOT DO:
    - Read environment variables (that is daemon.py's job — this module
      accepts a tz_name parameter for testing).
    - Parse arbitrary date formats (only ISO-8601).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# Default display timezone (IANA name).
_DEFAULT_TZ_NAME = "America/New_York"


def format_local_timestamp(
    utc_iso: str | None,
    tz_name: str | None = None,
) -> str:
    """Convert a UTC ISO-8601 timestamp to local display string.

    Parses the ISO string, converts to the given timezone (default
    ``America/New_York``, read from the ``DISPLAY_TZ`` env var when called
    from the daemon), and formats as ``YYYY-MM-DD HH:MM:SS TZ``
    (e.g. ``2026-09-15 11:10:48 EDT``).

    If the input is empty, unparseable, or the timezone is invalid,
    returns the input verbatim (graceful fallback).

    Args:
        utc_iso: ISO-8601 timestamp string from the camera (UTC).
        tz_name: Override IANA timezone name.  When *None* the module-level
            default (``DISPLAY_TZ`` env var or ``America/New_York``) is used.

    Returns:
        Localized display string, or the original input on failure.
    """
    if not utc_iso or not isinstance(utc_iso, str):
        return utc_iso or ""

    try:
        # Resolve the display timezone FIRST (inside the try block so an
        # invalid IANA name in DISPLAY_TZ falls back gracefully instead of
        # crashing the daemon on every alert).
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo(_DEFAULT_TZ_NAME)
        # Parse ISO-8601 (handles +0000, Z suffix, fractional seconds).
        dt = datetime.fromisoformat(utc_iso)
        # If naive (no tzinfo), assume UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        # Convert to display timezone.
        local_dt = dt.astimezone(tz)
        # Format with IANA tz name (e.g. EDT/EST).
        return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError, KeyError):
        return utc_iso
