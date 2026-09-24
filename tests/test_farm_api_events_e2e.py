"""test_farm_api_events_e2e — E2E tests for farm_api events endpoint.

E2E strategy:
    - Create a real data/events/ tree with event.json files in a temp directory.
    - Set FARMSURV_DATA_DIR so infra.paths and infra.event_record read from the temp
      tree. Import the API module after env is set so EVENTS_DIR resolves correctly.
    - Exercise list_events() and get_event() against real file I/O.
    - Verify newest-first sort order, camera filtering, and missing alert handling.

Bug classes covered:
    - Events not sorted newest-first.
    - Camera filtering returns events from wrong camera.
    - get_event crashes on nonexistent alert_id.
    - EventRecord fields not populated from event.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test data — three events across two cameras
# ---------------------------------------------------------------------------

_EVENT_A = {
    "alert_id": "00000000-0000-0000-0000-000000000001",
    "camera_id": "FRONT",
    "box_id": "farm-mac",
    "started_at": "2026-09-22T10:00:00+00:00",
    "finished_at": "2026-09-22T10:00:01+00:00",
    "gate": {"classification": "pass", "class_label": "person", "confidence": 0.95},
    "vm1": {"class": "person", "subclass": None, "confidence": "likely"},
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


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def _events_e2e_env(tmp_path: Path, monkeypatch):
    """Create real event.json files and set FARMSURV_DATA_DIR.

    Uses an out-of-tree temp dir so the real data/events/ is never touched.
    """
    data_dir = tmp_path / "farmdata"

    # EVENTS_DIR = DATA_DIR / "events" (event_record.py computes this at import)
    events_dir = data_dir / "events"

    for evt in (_EVENT_A, _EVENT_B, _EVENT_C):
        evt_dir = events_dir / evt["alert_id"]
        evt_dir.mkdir(parents=True, exist_ok=True)
        (evt_dir / "event.json").write_text(json.dumps(evt, indent=2))

    # Monkeypatch ENV vars at os.environ level (paths.py reads these at import)
    monkeypatch.setenv("FARMSURV_DATA_DIR", str(data_dir))

    # Clear cached modules so they re-read DATA_DIR
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("infra.event_record", "infra.farm_api.events", "infra.paths"):
            del sys.modules[mod_name]

    yield tmp_path

    # Restore
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("infra.event_record", "infra.farm_api.events", "infra.paths"):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------

class TestListEventsE2E:
    """E2E: list_events() returns events sorted newest-first."""

    def test_returns_list_of_event_records(self, _events_e2e_env):
        from infra.farm_api.events import EventRecord, list_events

        events = list_events()
        assert isinstance(events, list)
        assert len(events) == 3
        assert all(isinstance(e, EventRecord) for e in events)

    def test_newest_first_order(self, _events_e2e_env):
        from infra.farm_api.events import list_events

        events = list_events()
        # Newest first: C (12:00) > B (11:00) > A (10:00)
        assert events[0].alert_id == _EVENT_C["alert_id"]
        assert events[1].alert_id == _EVENT_B["alert_id"]
        assert events[2].alert_id == _EVENT_A["alert_id"]

    def test_event_fields_populated(self, _events_e2e_env):
        from infra.farm_api.events import list_events

        events = list_events()
        assert events[0].camera_id == "FRONT"
        assert events[0].dropped == "cooldown_expired"
        assert events[0].gate is not None
        assert events[0].gate["classification"] == "drop"


class TestFilterEventsE2E:
    """E2E: list_events(camera_id=X) filters correctly."""

    def test_filter_by_camera_front(self, _events_e2e_env):
        from infra.farm_api.events import list_events

        front_events = list_events(camera_id="FRONT")
        assert len(front_events) == 2
        assert all(e.camera_id == "FRONT" for e in front_events)
        # Newest first
        assert front_events[0].alert_id == _EVENT_C["alert_id"]
        assert front_events[1].alert_id == _EVENT_A["alert_id"]

    def test_filter_by_camera_back(self, _events_e2e_env):
        from infra.farm_api.events import list_events

        back_events = list_events(camera_id="BACK")
        assert len(back_events) == 1
        assert back_events[0].alert_id == _EVENT_B["alert_id"]

    def test_filter_unknown_camera(self, _events_e2e_env):
        from infra.farm_api.events import list_events

        none_events = list_events(camera_id="UNKNOWN")
        assert none_events == []


class TestGetEventE2E:
    """E2E: get_event() returns record or None."""

    def test_existing_event_returns_record(self, _events_e2e_env):
        from infra.farm_api.events import EventRecord, get_event

        result = get_event(_EVENT_A["alert_id"])
        assert result is not None
        assert isinstance(result, EventRecord)
        assert result.alert_id == _EVENT_A["alert_id"]
        assert result.camera_id == "FRONT"
        assert result.dropped is None

    def test_missing_event_returns_none(self, _events_e2e_env):
        from infra.farm_api.events import get_event

        result = get_event("00000000-0000-0000-0000-000000009999")
        assert result is None

    def test_gate_field_present(self, _events_e2e_env):
        from infra.farm_api.events import get_event

        result = get_event(_EVENT_A["alert_id"])
        assert result is not None
        assert result.gate is not None
        assert result.gate["classification"] == "pass"
        assert result.gate["class_label"] == "person"
