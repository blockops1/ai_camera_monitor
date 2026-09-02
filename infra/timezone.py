"""Timezone helpers — single source of truth for converting Reolink webhooks' UTC
ISO timestamps into the local format the user wants to see everywhere.

Phase 6B.99 (PLAN.md §11.26, maintainer directive 2026-08-19):
    *"US Congress has decided that we're not going to move to eastern standard
    time this winter. Just EDT for now is good."*

Operating rule: fixed UTC-4 offset, year-round, NO DST switch, NO
ZoneInfo("America/New_York") (that auto-switches to EST in November and would
re-introduce the very timestamp bug the user just asked to fix).

Public API:
    parse_iso(s)            — robust ISO-8601 parser; handles Reolink's `+0000`
                              format (no colon) that Python's fromisoformat()
                              chokes on. Returns tz-aware datetime or None.
                              Promoted from infra.vision_cache._parse_iso so it
                              can be the single source of truth.
    to_edt_string(s)        — parse an ISO-8601 UTC string, convert to fixed
                              UTC-4, format as "YYYY-MM-DD HH:MM:SS EDT".
                              Returns the input string unchanged on parse
                              failure (best-effort; never raises).
    format_dt_edt(dt)       — format an already-tz-aware datetime as
                              "YYYY-MM-DD HH:MM:SS EDT". Asserts the datetime
                              is tz-aware; for raw tz-naive inputs we error
                              loudly because they should never reach us.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Fixed UTC-4 offset. The user explicitly chose this over ZoneInfo for now —
# we hold the line and DO NOT add DST switch logic in this phase.
EDT = timezone(timedelta(hours=-4))

# Reolink webhook format: "2026-08-19T16:12:33.000+0000" (no colon in offset).
# Standard ISO: "2026-08-19T16:12:33.000+00:00". Both should parse.
_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")
_ISO_DIGIT_RE = re.compile(r"\d+")  # sanity helper for date-validation


def parse_iso(s: str | None) -> datetime | None:
    """
    Parse an ISO-8601 timestamp string into a tz-aware `datetime`.

    Handles three forms:
      1. Reolink "5-digit offset no colon": "2026-07-18T17:36:08.000+0000"
      2. Standard ISO: "2026-08-19T16:12:33.000+00:00"
      3. Z-suffix: "2026-08-19T16:12:33.000Z" — Reolink REOLINK_MODEL occasionally
         sends Z on firmware updates.

    Returns None on parse failure (BadTimeFormat, BadDateFormat, empty input,
    non-str input). NEVER raises.
    """
    if not s or not isinstance(s, str):
        return None
    try:
        # Normalize 5-digit to 6-digit offset: "+0000" → "+00:00", "-0600" → "-06:00"
        normalized = _OFFSET_RE.sub(r"\1:\2", s.strip())
        # Also normalize "Z" to "+00:00" so fromisoformat() accepts it
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None


def format_dt_edt(dt: datetime) -> str:
    """
    Format a tz-aware `datetime` in fixed EDT (UTC-4) as
    ``"YYYY-MM-DD HH:MM:SS EDT"``.

    The literal ``" EDT"`` suffix is the user's preferred format
    (maintainer 2026-08-19). Do NOT use a tz-suffix like ``-0400``.

    Raises:
        AssertionError: if ``dt`` is naive (tzinfo is None). We never accept
        naive datetimes here — every caller should already know which tz
        their input represents.
    """
    assert dt.tzinfo is not None, (
        "format_dt_edt() requires a tz-aware datetime; got naive: "
        f"{dt!r}. Callers must convert to a known tz first."
    )
    edt_dt = dt.astimezone(EDT)
    return edt_dt.strftime("%Y-%m-%d %H:%M:%S") + " EDT"


def to_edt_string(s: str | None) -> str:
    """
    Parse an ISO-8601 UTC timestamp string and return it reformatted as
    ``"YYYY-MM-DD HH:MM:SS EDT"``.

    Best-effort: on parse failure (None, empty, garbled), returns the input
    string unchanged so downstream logs/Telegrams still carry *something*
    rather than going blank. This is the right failure mode — bad input must
    not abort an alert pipeline.

    >>> to_edt_string("2026-08-19T16:12:33.000+0000")
    '2026-08-19 12:12:33 EDT'
    >>> to_edt_string("garbage")
    'garbage'
    """
    if not s or not isinstance(s, str):
        return s or ""
    parsed = parse_iso(s)
    if parsed is None:
        return s
    return format_dt_edt(parsed)


__all__ = ["EDT", "format_dt_edt", "parse_iso", "to_edt_string"]
