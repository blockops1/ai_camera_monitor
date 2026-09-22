"""Tests for infra.farm_api.events.

STATUS: V2-068b2
RUN WITH: pytest tests/test_farm_api_events.py -v

Test strategy:
    - Create a temporary data/events/ tree with fake event.json files.
    - Override FARMSURV_DATA_DIR so EVENTS_DIR points at the temp dir.
    - Reload infra.event_record to pick up the new EVENTS_DIR.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test data — three events across two cameras.
# ---------------------------------------------------------------------------

_EVENT_A = {
    "alert_id": "00000000-0000-0000-0000-000000000001",
    "camera_id": "FRONT",
    "box_id": "farm-mac",
    "started_at": "2026-09-22T10:00:00+00:00",
    "finished_at": "2026-09-22T10:00:01+00:00",
    "gate": {"classification": "pass", "class_label": "person", "confidence": 0.95},
    "vm1": {"class": "person", "subclass": "human", "confidence": "likely"},
    "vm2": {"class_confirmed": "person"},
    "match": {"matched": True, "identity": "jill"},
    "dropped": None,
    "frame_paths": ["/tmp/frame_001.png"],
    "artifact_dir": "/tmp/artifact_001",
}

_EVENT_B = {
    "alert_id": "00000000-0000-0000-0000-000000000002",
    "camera_id": "BACK",
    "box_id": "farm-mac",
    "started_at": "2026-09-22T11:00:00+00:00",
    "finished_at": "2026-09-22T11:00:01+00:00",
    "gate": {"classification": "pass", "class_label": "car", "confidence": 0.80},
    "vm1": {"class": "vehicle", "subclass": "car", "confidence": "likely"},
    "vm2": {"class_confirmed": "vehicle"},
    "match": {"matched": False},
    "dropped": None,
    "frame_paths": ["/tmp/frame_001.png"],
    "artifact_dir": "/tmp/artifact_002",
}

_EVENT_C = {
    "alert_id": "00000000-0000-0000-0000-000000000003",
    "camera_id": "FRONT",
    "box_id": "farm-mac",
    "started_at": "2026-09-22T12:00:00+00:00",
    "finished_at": "2026-09-22T12:00:01+00:00",
    "gate": {"classification": "drop", "reason": "cooldown"},
    "vm1": None,
    "vm2": None,
    "match": None,
    "dropped": "cooldown_expired",
    "frame_paths": [],
    "artifact_dir": "",
}


@pytest.fixture()
def _test_events_dir(tmp_path: Path):
    """Create fake event.json files under data/events/ and reload modules."""
    events_dir = tmp_path / "data" / "events"

    # Write events (mkdir parents so nested dirs are created)
    for evt in (_EVENT_A, _EVENT_B, _EVENT_C):
        evt_dir = events_dir / evt["alert_id"]
        evt_dir.mkdir(parents=True, exist_ok=True)
        (evt_dir / "event.json").write_text(
            json.dumps(evt, indent=2)
        )

    # Save old env
    old_data = os.environ.get("FARMSURV_DATA_DIR")

    # Set FARMSURV_DATA_DIR so EVENTS_DIR points at temp
    os.environ["FARMSURV_DATA_DIR"] = str(tmp_path / "data")

    # Clear cached modules so they re-read the new EVENTS_DIR
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.event_record", "infra.farm_api.events")):
            del sys.modules[mod_name]

    yield tmp_path

    # Restore
    if old_data is not None:
        os.environ["FARMSURV_DATA_DIR"] = old_data
    else:
        os.environ.pop("FARMSURV_DATA_DIR", None)

    # Re-clear so subsequent tests get a fresh module
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.event_record", "infra.farm_api.events")):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# test_list_events_newest_first
# ---------------------------------------------------------------------------

class TestListEventsNewestFirst:
    """list_events() returns events sorted newest-first by default."""

    def test_list_events_newest_first(self, _test_events_dir):
        from infra.farm_api.events import EventRecord, list_events

        events = list_events()

        assert len(events) == 3
        assert all(isinstance(e, EventRecord) for e in events)

        # Newest first: C (12:00) > B (11:00) > A (10:00)
        assert events[0].alert_id == _EVENT_C["alert_id"]
        assert events[1].alert_id == _EVENT_B["alert_id"]
        assert events[2].alert_id == _EVENT_A["alert_id"]

        # Fields are correct dataclass instances
        assert events[0].camera_id == "FRONT"
        assert events[0].dropped == "cooldown_expired"


# ---------------------------------------------------------------------------
# test_list_events_filter_by_camera
# ---------------------------------------------------------------------------

class TestListEventsFilterByCamera:
    """list_events(camera_id=X) returns only events from that camera."""

    def test_list_events_filter_by_camera(self, _test_events_dir):
        from infra.farm_api.events import list_events

        # Filter FRONT: A and C
        front_events = list_events(camera_id="FRONT")
        assert len(front_events) == 2
        assert all(e.camera_id == "FRONT" for e in front_events)
        assert front_events[0].alert_id == _EVENT_C["alert_id"]  # newest first
        assert front_events[1].alert_id == _EVENT_A["alert_id"]

        # Filter BACK: only B
        back_events = list_events(camera_id="BACK")
        assert len(back_events) == 1
        assert back_events[0].alert_id == _EVENT_B["alert_id"]

        # Filter nonexistent: empty
        none_events = list_events(camera_id="UNKNOWN")
        assert len(none_events) == 0


# ---------------------------------------------------------------------------
# test_get_event_missing_returns_none
# ---------------------------------------------------------------------------

class TestGetEventMissing:
    """get_event(event_id) returns None for a nonexistent alert_id."""

    def test_get_event_missing_returns_none(self, _test_events_dir):
        from infra.farm_api.events import get_event

        result = get_event("00000000-0000-0000-0000-000000009999")
        assert result is None

    def test_get_event_existing_returns_record(self, _test_events_dir):
        from infra.farm_api.events import EventRecord, get_event

        result = get_event(_EVENT_A["alert_id"])
        assert result is not None
        assert isinstance(result, EventRecord)
        assert result.alert_id == _EVENT_A["alert_id"]
        assert result.camera_id == "FRONT"
        assert result.dropped is None
        assert result.gate is not None
        assert result.gate["classification"] == "pass"
