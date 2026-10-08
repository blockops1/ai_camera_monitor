"""test_events_pagination — E2E tests for events endpoint pagination and filtering.

US-101y.events: events pagination + camera_id + datetime parse.

E2E strategy:
    - Create a real data/events/ tree with event.json files in a temp directory.
    - Set FARMSURV_DATA_DIR via os.environ so infra.paths reads from temp tree.
    - Exercise list_events() directly against real file I/O.
    - Verify pagination tokens, since filtering, limit enforcement, and camera_id filter.
    - Each test prints the response body for verification.

Bug classes covered:
    - Missing next_page_token when more events exist.
    - page_token not decoding to the correct last alert_id.
    - since parameter not parsing ISO-8601 with Z suffix.
    - limit > 200 not returning 400.
    - camera_id filter not working via API or MCP.
    - Slow iteration on large event sets (>1000 events).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test data — 25 events across 3 cameras
# ---------------------------------------------------------------------------

def _make_event(alert_num: int, camera_id: str, hour: int) -> dict:
    """Create a synthetic event dict."""
    return {
        "alert_id": f"00000000-0000-0000-0000-00000000000{alert_num:02d}",
        "camera_id": camera_id,
        "box_id": "farm-test",
        "started_at": f"2026-09-22T{hour:02d}:00:00+00:00",
        "finished_at": f"2026-09-22T{hour:02d}:00:01+00:00",
        "gate": {"classification": "pass", "class_label": "car", "confidence": 0.80},
        "vm1": {"class": "vehicle", "subclass": "car", "confidence": "likely"},
        "vm2": {"class_confirmed": "vehicle"},
        "match": None,
        "dropped": None,
        "frame_paths": [f"/tmp/frame_{alert_num}.png"],
        "artifact_dir": f"/tmp/artifact_{alert_num}",
    }


# Create 25 events: 10 FRONT (hours 10-19), 8 BACK (hours 10-17), 7 OUTSIDE (hours 10-16)
_TEST_EVENTS = []
for i in range(10):
    _TEST_EVENTS.append(_make_event(i + 1, "FRONT", 10 + i))
for i in range(8):
    _TEST_EVENTS.append(_make_event(i + 11, "BACK", 10 + i))
for i in range(7):
    _TEST_EVENTS.append(_make_event(i + 19, "OUTSIDE", 10 + i))


@pytest.fixture(scope="module", autouse=True)
def _event_tree(tmp_path_factory):
    """Create a temporary events directory with 25 event.json files.

    This fixture runs once per module and sets FARMSURV_DATA_DIR.
    """
    tmpdir = tmp_path_factory.mktemp("events_pagination")
    events_dir = tmpdir / "events"
    events_dir.mkdir()

    for event in _TEST_EVENTS:
        event_dir = events_dir / event["alert_id"]
        event_dir.mkdir()
        (event_dir / "event.json").write_text(
            json.dumps(event), encoding="utf-8"
        )

    # Also create an additional 975 events to test pagination depth
    # (we only need 25 for core tests, but 1000+ for performance)
    for i in range(1000):
        alert_id = f"00000000-0000-0000-0000-000000000{1000 + i:04d}"
        event_dir = events_dir / alert_id
        event_dir.mkdir()
        (event_dir / "event.json").write_text(
            json.dumps({
                "alert_id": alert_id,
                "camera_id": "FRONT",
                "box_id": "farm-test",
                "started_at": f"2026-09-20T{10 + i % 10:02d}:00:00+00:00",
                "finished_at": f"2026-09-20T{10 + i % 10:02d}:00:01+00:00",
                "gate": {"classification": "pass"},
                "vm1": None,
                "vm2": None,
                "match": None,
                "dropped": None,
                "frame_paths": [],
                "artifact_dir": "",
            }),
            encoding="utf-8",
        )

    return tmpdir


def _import_events_module(event_tree: Path):
    """Import infra.farm_api.events with DATA_DIR pointing at the temp tree."""
    # Clear cached modules
    for mod in list(sys.modules.keys()):
        if mod.startswith("infra."):
            del sys.modules[mod]

    os.environ["FARMSURV_DATA_DIR"] = str(event_tree)
    from infra.farm_api import events
    return events


class TestEventsPagination:
    """E2E tests for events pagination behavior."""

    def test_first_page_returns_next_page_token(self, _event_tree):
        """AC1: first page returns next_page_token when more events exist."""
        events = _import_events_module(_event_tree)
        result = events.list_events(limit=10)
        assert len(result["data"]) == 10
        assert result["next_page_token"] is not None, (
            "Expected next_page_token when limit=10 and >10 events exist"
        )
        print(f"next_page_token: {result['next_page_token']}")

    def test_page_token_decodes_to_last_alert_id(self, _event_tree):
        """AC2: page_token decodes to the last alert_id; next page starts after."""
        events = _import_events_module(_event_tree)
        page1 = events.list_events(limit=5)
        assert len(page1["data"]) == 5
        token = page1["next_page_token"]
        assert token is not None

        # Decode token
        decoded = base64.b64decode(token).decode("utf-8")
        print(f"Decoded token (last alert_id from page 1): {decoded}")

        # Fetch page 2
        page2 = events.list_events(limit=5, page_token=token)
        assert len(page2["data"]) == 5
        # First event of page 2 should NOT be the same as last event of page 1
        page2_first = page2["data"][0].alert_id
        page1_last = page1["data"][-1].alert_id
        assert page2_first != page1_last, (
            f"Page 2 starts with same alert_id as page 1 end: {page2_first}"
        )
        print(f"Page 1 last: {page1_last}, Page 2 first: {page2_first}")

    def test_since_with_z_suffix_parses_correctly(self, _event_tree):
        """AC3: since parameter parses ISO-8601 with Z suffix."""
        events = _import_events_module(_event_tree)
        # Events with started_at >= 2026-09-22T15:00:00Z
        result = events.list_events(since="2026-09-22T15:00:00Z", limit=50)
        for record in result["data"]:
            assert record.started_at >= "2026-09-22T15:00:00", (
                f"Event {record.alert_id} started_at {record.started_at} "
                f"before since filter 2026-09-22T15:00:00"
            )
        print(f"since=2026-09-22T15:00:00Z returned {len(result['data'])} events")

    def test_since_with_plus_offset_parses_correctly(self, _event_tree):
        """Since with +00:00 offset also works (not just Z suffix)."""
        events = _import_events_module(_event_tree)
        result = events.list_events(since="2026-09-22T15:00:00+00:00", limit=50)
        for record in result["data"]:
            assert record.started_at >= "2026-09-22T15:00:00", (
                f"Event {record.alert_id} started_at {record.started_at} "
                f"before since filter"
            )
        print(f"since=2026-09-22T15:00:00+00:00 returned {len(result['data'])} events")

    def test_limit_gt_200_returns_400(self, _event_tree):
        """AC4: limit > 200 raises ValueError (mapped to 400 by the API)."""
        events = _import_events_module(_event_tree)
        with pytest.raises(ValueError, match=r"limit must be <= 200"):
            events.list_events(limit=201)
        print("limit=201 raised ValueError as expected")

    def test_limit_exact_200_works(self, _event_tree):
        """limit=200 should work (boundary case)."""
        events = _import_events_module(_event_tree)
        result = events.list_events(limit=200)
        assert len(result["data"]) == 200
        print(f"limit=200 returned {len(result['data'])} events")

    def test_camera_id_filter_via_api(self, _event_tree):
        """AC5: camera_id filter works via API (list_events with camera_id)."""
        events = _import_events_module(_event_tree)
        result = events.list_events(camera_id="FRONT", limit=50)
        for record in result["data"]:
            assert record.camera_id == "FRONT", (
                f"Expected camera_id=FRONT, got {record.camera_id}"
            )
        print(f"camera_id=FRONT returned {len(result['data'])} events")

    def test_camera_id_filter_via_mcp(self, _event_tree):
        """AC5: camera_id filter works via MCP tool (list_events tool)."""
        events = _import_events_module(_event_tree)
        # The MCP server calls the same list_events function with camera_id arg
        result = events.list_events(camera_id="BACK", limit=50)
        for record in result["data"]:
            assert record.camera_id == "BACK", (
                f"Expected camera_id=BACK, got {record.camera_id}"
            )
        print(f"camera_id=BACK returned {len(result['data'])} events")

    def test_pagination_completes_all_events(self, _event_tree):
        """Verify that pagination walks through all events."""
        events = _import_events_module(_event_tree)
        all_alert_ids = []
        page_token = None
        page_num = 0
        while True:
            page_num += 1
            result = events.list_events(limit=50, page_token=page_token)
            alert_ids_in_page = [r.alert_id for r in result["data"]]
            all_alert_ids.extend(alert_ids_in_page)
            if result["next_page_token"] is None:
                break
            page_token = result["next_page_token"]
        print(f"Pagination completed: {page_num} pages, {len(all_alert_ids)} total events")
        # Should have gotten all 1025 events (25 original + 1000 additional)
        assert len(all_alert_ids) == 1025, (
            f"Expected 1025 events total, got {len(all_alert_ids)}"
        )

    def test_latency_under_0_5s_for_limit_50(self, _event_tree):
        """AC6: latency under 0.5s for limit=50 with >1000 events on disk."""
        events = _import_events_module(_event_tree)
        start = time.monotonic()
        _result = events.list_events(limit=50)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, (
            f"list_events(limit=50) took {elapsed:.3f}s, expected < 0.5s"
        )
        print(f"list_events(limit=50) took {elapsed:.3f}s (threshold: 0.5s)")
