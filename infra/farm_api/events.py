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
    - Any code that needs a read-only event history

USAGE:
    from infra.farm_api.events import list_events, get_event
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from infra.event_record import EVENTS_DIR, EventRecord

log = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_events(
    camera_id: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[EventRecord]:
    """Return a list of EventRecord, newest first.

    Walks data/events/*/event.json, optionally filtered by camera_id
    and minimum started_at timestamp.

    Args:
        camera_id: If provided, only return events from this camera.
        since: If provided, only return events with started_at >= this
            ISO-8601 timestamp (string comparison works for ISO-8601).
        limit: Maximum number of events to return (default 50).

    Returns:
        A list of EventRecord instances, sorted by alert_id descending
        (newest first, since alert_ids are UUIDs).
    """
    event_base = Path(EVENTS_DIR)

    if not event_base.is_dir():
        return []

    # Collect all event.json paths
    all_events: list[tuple[str, Path]] = []
    for alert_dir in event_base.iterdir():
        if not alert_dir.is_dir():
            continue
        event_file = alert_dir / "event.json"
        raw = _read_event_file(event_file)
        if raw is None:
            continue
        all_events.append((raw.get("alert_id", alert_dir.name), event_file))

    # Sort by alert_id descending (newest first — alert_ids are UUIDs
    # with time-based components, so descending string sort = newest first).
    all_events.sort(key=lambda x: x[0], reverse=True)

    # Parse into records and filter
    result: list[EventRecord] = []
    for _aid, epath in all_events:
        raw = _read_event_file(epath)
        if raw is None:
            continue

        # Apply camera_id filter
        if camera_id is not None and raw.get("camera_id", "") != camera_id:
            continue

        # Apply since filter (ISO-8601 string comparison)
        if since is not None:
            started_at = raw.get("started_at", "")
            if started_at < since:
                continue

        try:
            record = _dict_to_record(raw)
        except ValueError as exc:
            log.warning("events: skipping malformed event: %s", exc)
            continue

        result.append(record)

        if len(result) >= limit:
            break

    return result


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
