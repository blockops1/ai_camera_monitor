"""farm_api.events — Events resource functions.

STATUS: alpha - V2-068b2
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - data/events/<alert_id>/event.json (filesystem)
    - infra.event_record.EventRecord (dataclass)

OUTPUTS:
    - EventRecord (re-exported from infra.event_record)
    - list[EventRecord], EventRecord | None

DOES NOT DO:
    - Modify event records (write via infra.event_record.write_event_record)
    - Connect to external services (pure filesystem reads)

CALLERS:
    - infra.mcp_server (MCP tool adapters: list_events, get_event)
    - infra.api_server (API route: _route_events)
    - Any code that needs a read-only event history

USAGE:
    from infra.farm_api.events import list_events, get_event
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from infra.event_record import EVENTS_DIR, EventRecord

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache: sorted alert_id list with mtime-based invalidation
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {
    "alert_ids": list[str](),  # sorted newest-first
    "mtime": 0.0,              # mtime of EVENTS_DIR at last scan
}


def _scan_event_dirs() -> list[str]:
    """Return alert_ids sorted newest-first using os.scandir.

    Uses mtime-based directory stat to skip re-scanning when nothing
    has changed on disk.  Bounded to one entry (the entire index).
    Cleared on listener restart (module reload) only.
    """
    event_base = Path(EVENTS_DIR)
    if not event_base.is_dir():
        return []

    dir_mtime = event_base.stat().st_mtime

    # Re-scan only when the directory mtime changed
    if dir_mtime == _cache["mtime"] and _cache["alert_ids"]:
        return _cache["alert_ids"]

    # Fast scan: os.scandir avoids per-entry is_dir() syscalls
    alert_ids: list[str] = []
    try:
        with os.scandir(str(event_base)) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    alert_ids.append(entry.name)
    except OSError as exc:
        log.warning("events: failed to scan %s: %s", event_base, exc)
        return []

    # Sort descending (newest first — UUIDs are time-based)
    alert_ids.sort(reverse=True)

    _cache["alert_ids"] = alert_ids
    _cache["mtime"] = dir_mtime
    return alert_ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_events(
    camera_id: str | None = None,
    since: str | None = None,
    limit: int = 50,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Return a paginated list of EventRecord, newest first.

    Walks data/events/*/event.json using os.scandir (no full iterdir),
    optionally filtered by camera_id and minimum started_at timestamp.

    Args:
        camera_id: If provided, only return events from this camera.
        since: If provided, only return events with started_at >= this
            ISO-8601 timestamp (parsed to tz-aware datetime, Z normalised
            to +00:00).
        limit: Maximum number of events to return (default 50, max 200).
        page_token: Opaque cursor (base64 of last alert_id from previous
            page).  Absent on first call; response includes next_page_token
            if more results exist.

    Returns:
        dict with keys:
            data (list[EventRecord]): matching event records.
            next_page_token (str | None): cursor for the next page, or None
                if this is the last page.

    Raises:
        ValueError: If limit > 200.
    """
    # Limit cap
    if limit > 200:
        raise ValueError(f"limit must be <= 200 (got {limit})")
    if limit < 1:
        limit = 50  # default floor

    event_base = Path(EVENTS_DIR)
    if not event_base.is_dir():
        return {"data": [], "next_page_token": None}

    # Parse since to tz-aware datetime
    since_dt: datetime | None = None
    if since is not None:
        # Normalise Z suffix to +00:00 for fromisoformat
        since_norm = since.replace("Z", "+00:00")
        since_dt = datetime.fromisoformat(since_norm)

    # Decode page_token to get the alert_id cursor
    last_alert_id: str | None = None
    if page_token is not None:
        try:
            last_alert_id = base64.b64decode(page_token).decode("utf-8")
        except (ValueError, TypeError):
            # Malformed token — start from beginning
            log.warning("events: invalid page_token, starting from beginning")

    # Get cached sorted alert_id list
    alert_ids = _scan_event_dirs()

    # Reverse-iterate: skip events we've already seen (after page_token)
    # and stop once we have enough results
    result: list[EventRecord] = []
    count = 0
    # (no artificial cap — alert_ids list is bounded by dir scan)

    # With page_token: skip all events up to and including the token.
    # In newest-first order: [003, 002, 001]. Token=002 means skip
    # 003 and 002, return only 001 (events older than the token).
    skip_until_token = last_alert_id is not None

    for aid in alert_ids:
        if skip_until_token:
            if aid == last_alert_id:
                skip_until_token = False  # found token, stop skipping
            continue  # skip everything until we find and pass the token

        # No page_token — skip if since filter doesn't match
        # (we need to read the file to check started_at)

        # Read event.json
        event_path = event_base / aid / "event.json"
        raw: dict[str, Any] | None = None

        if since_dt is not None and last_alert_id is None:
            # No page_token — check started_at to enforce since filter.
            # Since alert_ids are sorted newest-first (UUIDs are
            # time-based), once we find an event older than since_dt,
            # all remaining events are also older — break early.
            try:
                raw = json.loads(event_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            started_at = raw.get("started_at", "")
            if started_at:
                started_at_norm = started_at.replace("Z", "+00:00")
                try:
                    started_dt_check = datetime.fromisoformat(started_at_norm)
                    if started_dt_check < since_dt:
                        break  # all subsequent events are older
                except ValueError:
                    continue
            else:
                continue

        elif since_dt is not None and last_alert_id is not None:
            # With page_token, skip the since check (page_token already
            # filters by recency; since is redundant when paginating).
            pass
        else:
            # No since filter — read fresh.
            try:
                raw = json.loads(event_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

        # raw is guaranteed to be a dict here (only set when read succeeds)
        # Pyright can't narrow through the branch/continue pattern, so cast.
        _raw = cast(dict[str, Any], raw)

        # Apply camera_id filter
        if camera_id is not None and _raw.get("camera_id", "") != camera_id:
            continue

        # Build record
        try:
            record = _dict_to_record(_raw)
        except ValueError as exc:
            log.warning("events: skipping malformed event: %s", exc)
            continue

        result.append(record)
        count += 1

        if count >= limit:
            break

    # Determine next_page_token
    next_token: str | None = None
    if len(result) == limit and count >= limit:
        # There might be more — encode the last alert_id
        last_record = result[-1]
        next_token = base64.b64encode(last_record.alert_id.encode("utf-8")).decode("utf-8")

    return {
        "data": result,
        "next_page_token": next_token,
    }


def get_event(event_id: str) -> EventRecord | None:
    """Return a single EventRecord by alert_id.

    Args:
        event_id: The alert_id (UUID string) of the event to look up.

    Returns:
        EventRecord if found, None if the event.json does not exist
        or is malformed.
    """
    event_path = Path(EVENTS_DIR) / event_id / "event.json"
    raw = _read_event_file(event_path)
    if raw is None:
        return None

    try:
        return _dict_to_record(raw)
    except ValueError as exc:
        log.warning("events: malformed event %s: %s", event_id, exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_event_file(event_path: Path) -> dict[str, Any] | None:
    """Read and parse a single event.json file.

    Args:
        event_path: Path to the event.json file.

    Returns:
        Parsed dict, or None if the file does not exist or is malformed.
    """
    if not event_path.is_file():
        return None
    try:
        raw = event_path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("events: failed to read %s: %s", event_path, exc)
        return None


def _dict_to_record(d: dict[str, Any]) -> EventRecord:
    """Convert a raw event dict into an EventRecord dataclass instance.

    Args:
        d: Parsed JSON dict from event.json.

    Returns:
        EventRecord instance.

    Raises:
        ValueError: If required fields are missing.
    """
    required = ("alert_id", "camera_id", "box_id", "started_at",
                "finished_at", "frame_paths", "artifact_dir")
    for key in required:
        if key not in d:
            raise ValueError(f"event dict missing required field: {key}")

    return EventRecord(
        alert_id=d["alert_id"],
        camera_id=d["camera_id"],
        box_id=d["box_id"],
        started_at=d["started_at"],
        finished_at=d["finished_at"],
        gate=d.get("gate"),
        vm1=d.get("vm1"),
        vm2=d.get("vm2"),
        match=d.get("match"),
        dropped=d.get("dropped"),
        frame_paths=d.get("frame_paths", []),
        artifact_dir=d.get("artifact_dir", ""),
    )
