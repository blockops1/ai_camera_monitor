"""
event_record.py — Per-alert EventRecord dataclass + atomic JSON write.

STATUS: new (US-068a)
THREAD SAFETY: single-writer per alert_id; fsync+rename is atomic on POSIX.

INPUTS:
    - alert_id (str): unique identifier for the event
    - camera_id (str): camera that produced the event
    - box_id (str): multi-box namespace identifier
    - started_at / finished_at (str): ISO-8601 timestamps
    - gate (dict|None): gate verdict summary
    - vm1 (dict|None): VM1 result
    - vm2 (dict|None): VM2 result
    - match (dict|None): match result
    - dropped (str|None): drop reason if event was dropped, else None
    - frame_paths (list[str]): frame paths captured for this event
    - artifact_dir (str): alert-scoped artifact directory

OUTPUTS:
    - event.json written to data/events/<alert_id>/event.json

PUBLIC API:
    write_event_record(alert_id, camera_id, box_id, started_at, finished_at,
                       gate, vm1, vm2, match, dropped, frame_paths,
                       artifact_dir) -> None
        Write a complete EventRecord to disk atomically.

DOES NOT DO:
    - Read event records (that is US-068b2, farm_api.events)
    - Emit event records (caller in _run_pipeline decides when)

WHY HERE:
    Centralised event-record format so all downstream consumers (farm_api,
    MCP server, audit) share the same schema.

CALLS INTO:
    - json, os, pathlib, dataclasses
    - infra.paths for events output directory
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from infra.paths import DATA_DIR

log = logging.getLogger(__name__)

# Output directory for event.json files per alert.
EVENTS_DIR = str(Path(DATA_DIR) / "events")


@dataclass
class EventRecord:
    """Complete record of a single alert pipeline run.

    All fields are JSON-serialisable (dataclass + asdict).
    """
    alert_id: str
    camera_id: str
    box_id: str
    started_at: str
    finished_at: str
    gate: dict[str, Any] | None
    vm1: dict[str, Any] | None
    vm2: dict[str, Any] | None
    match: dict[str, Any] | None
    dropped: str | None
    frame_paths: list[str]
    artifact_dir: str


def write_event_record(
    alert_id: str,
    camera_id: str,
    box_id: str,
    started_at: str,
    finished_at: str,
    gate: dict[str, Any] | None,
    vm1: dict[str, Any] | None,
    vm2: dict[str, Any] | None,
    match: dict[str, Any] | None,
    dropped: str | None,
    frame_paths: list[str],
    artifact_dir: str,
    event_dir: str | None = None,
) -> str:
    """Write an EventRecord atomically to data/events/<alert_id>/event.json.

    Atomic strategy:
        1. Create the events/<alert_id> directory.
        2. Write to a .tmp file in the same directory (same mount point).
        3. fsync the .tmp file.
        4. os.rename(.tmp, event.json) -- atomic on POSIX.

    Args:
        alert_id: Unique event identifier.
        camera_id: Camera that triggered the event.
        box_id: Multi-box namespace (e.g. hostname).
        started_at: ISO-8601 timestamp when processing began.
        finished_at: ISO-8601 timestamp when processing finished.
        gate: Gate verdict summary (or None).
        vm1: VM1 verify_class result (or None).
        vm2: VM2 detail_class result (or None).
        match: Match result dict (or None).
        dropped: Drop reason string, or None for non-dropped events.
        frame_paths: List of frame paths captured for this event.
        artifact_dir: Alert-scoped artifact directory path.

    Returns:
        The absolute path to the written event.json file.
    """
    if event_dir is None:
        event_base = Path(EVENTS_DIR)
    else:
        event_base = Path(event_dir)
    event_path = event_base / alert_id
    event_path.mkdir(parents=True, exist_ok=True)

    record = EventRecord(
        alert_id=alert_id,
        camera_id=camera_id,
        box_id=box_id,
        started_at=started_at,
        finished_at=finished_at,
        gate=gate,
        vm1=vm1,
        vm2=vm2,
        match=match,
        dropped=dropped,
        frame_paths=frame_paths,
        artifact_dir=artifact_dir,
    )

    final_path = event_path / "event.json"

    tmp_path: str | None = None
    tmp_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w",
        dir=str(event_path),
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp_path = tmp_file.name
        json.dump(asdict(record), tmp_file, indent=2, default=str)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    except Exception:
        tmp_file.close()
        if tmp_path is not None:
            os.unlink(tmp_path)
        log.exception(
            "event_record: alert_id=%s camera=%s write failed",
            alert_id, camera_id,
        )
        raise

    tmp_file.close()
    os.rename(tmp_path, str(final_path))
    log.info(
        "event_record: alert_id=%s camera=%s written=%s",
        alert_id, camera_id, final_path,
    )
    return str(final_path)
