"""Tests for telegram_formatter.match_alert — US-067c: stripped debug, TG marker.

Covers acceptance criteria for US-067c:
  (AC1) exactly one build_match_message def
  (AC2) no Thresholds:/Mode:/Reason: debug lines
  (AC3) TG X of 3 marker present
  (AC4) Label/Make/Owner field lines present
  (AC5) all tests pass
  (AC6) merged to main

Also tests the full build_match_message integration.
"""

from __future__ import annotations

from telegram_formatter.match_alert import (
    build_match_alert_body,
    build_match_message,
    pick_alert_image_path,
    status_wording_for,
)


def _make_alert(
    alert_id: str = "evt-001",
    timestamp: str = "2026-09-14T10:00:00Z",
) -> dict:
    """Build an alert dict with id and timestamp."""
    return {"id": alert_id, "timestamp": timestamp}


# ---------------------------------------------------------------------------
# AC2: no debug lines in output
# ---------------------------------------------------------------------------


class TestNoDebugLines:
    """US-067c: verify Mode:/Reason:/Thresholds:/Confidence: standalone are gone."""

    def test_match_body_no_mode_line(self):
        """Match body must not contain 'Mode:'."""
        match_result = {
            "matched": True,
            "known_vehicle": {
                "id": "v1", "label": "Test", "color": "red", "make": "Ford", "model": "F150",
            },
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=3.0)
        assert "Mode:" not in body

    def test_match_body_no_reason_line(self):
        """Match body must not contain 'Reason:'."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test", "color": "red", "make": "Ford", "model": "F150"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=3.0)
        assert "Reason:" not in body

    def test_match_body_no_thresholds_line(self):
        """Match body must not contain 'Thresholds:'."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test", "color": "red", "make": "Ford", "model": "F150"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=3.0)
        assert "Thresholds:" not in body

    def test_match_body_no_confidence_standalone(self):
        """Match body must not contain a standalone 'Confidence:' line."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test", "color": "red", "make": "Ford", "model": "F150"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=3.0)
        assert "Confidence:" not in body

    def test_no_match_body_has_no_thresholds(self):
        """No-match message must not contain 'Thresholds:'."""
        result = build_match_message(
            match_result={"matched": False},
            vm2_result={"class": "vehicle"},
            camera_label="Front Gate",
            top_candidates=[("v_a", 5.0)],
            position=3,
        )
        assert "Thresholds:" not in result["caption"]

    def test_no_match_body_has_no_reason_line(self):
        """No-match message must not contain 'Reason:'."""
        result = build_match_message(
            match_result={"matched": False},
            vm2_result={"class": "vehicle"},
            camera_label="Front Gate",
            top_candidates=[("v_a", 5.0)],
            position=3,
        )
        assert "Reason:" not in result["caption"]

    def test_no_match_body_has_no_status_line(self):
        """No-match message uses emoji header, not 'Status:'."""
        result = build_match_message(
            match_result={"matched": False},
            vm2_result={"class": "vehicle"},
            camera_label="Front Gate",
            top_candidates=[("v_a", 5.0)],
            position=3,
        )
        assert "Status:" not in result["caption"]


# ---------------------------------------------------------------------------
# AC3: TG marker present
# ---------------------------------------------------------------------------


class TestTGMarker:
    """US-067c: verify TG {position} of 3 marker in output."""

    def test_match_has_tg_marker(self):
        """Match message includes TG marker when position provided."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        result = build_match_message(
            match_result, {}, camera_label="Front Gate", position=3,
        )
        assert "TG 3 of 3" in result["caption"]

    def test_no_match_has_tg_marker(self):
        """No-match message includes TG marker when position provided."""
        result = build_match_message(
            match_result={"matched": False},
            vm2_result={"class": "vehicle"},
            camera_label="Front Gate",
            top_candidates=[("v_a", 5.0)],
            position=1,
        )
        assert "TG 1 of 3" in result["caption"]

    def test_no_tg_marker_when_position_none(self):
        """When position is None, no TG marker is emitted."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        result = build_match_message(
            match_result, {}, camera_label="Front Gate", position=None,
        )
        assert "TG" not in result["caption"]


# ---------------------------------------------------------------------------
# AC4: Label/Make/Owner field lines
# ---------------------------------------------------------------------------


class TestFieldLines:
    """US-067c: verify Label, Make, Owner field lines present."""

    def test_match_includes_label_line(self):
        """Match body includes 'Label:' line."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "KV-001"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=0.0)
        assert "Label: KV-001" in body

    def test_match_includes_make_line(self):
        """Match body includes 'Make:' line."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test", "make": "Ford"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=0.0)
        assert "Make: Ford" in body

    def test_match_includes_owner_line(self):
        """Match body includes 'Owner:' line when present."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test", "owner": "John"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=0.0)
        assert "Owner: John" in body

    def test_match_includes_model_line(self):
        """Match body includes 'Model:' line."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test", "model": "F-150"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=0.0)
        assert "Model: F-150" in body

    def test_match_header_bold_label(self):
        """Match header uses bold label: Match — *Label*."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Black Truck"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        body = build_match_alert_body(match_result, {}, score=8.0, gap=0.0)
        assert "Match — *Black Truck*" in body


# ---------------------------------------------------------------------------
# AC2 full: grep for (Thresholds:|Mode:|Reason:)
# ---------------------------------------------------------------------------


class TestAC2Grep:
    """Verify the file contains zero lines matching (Thresholds:|Mode:|Reason:)."""

    def test_build_match_alert_body_no_debug(self):
        """build_match_alert_body output has no debug keywords."""
        match_result = {
            "matched": True,
            "known_vehicle": {
                "id": "v1", "label": "Test", "color": "red",
                "make": "Ford", "model": "F150", "owner": "Jane",
            },
            "score": 8.5,
            "all_scores": [("v1", 8.5), ("v2", 3.0)],
        }
        body = build_match_alert_body(
            match_result, {}, score=8.5, gap=5.5,
            runner_ups=[("v2", 3.0)],
        )
        assert "Thresholds:" not in body
        assert "Mode:" not in body
        assert "Reason:" not in body
        assert "Confidence:" not in body  # standalone Confidence line removed

    def test_build_match_message_no_debug(self):
        """build_match_message output has no debug keywords."""
        # Match case
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        result = build_match_message(
            match_result, {}, camera_label="Cam", position=3,
        )
        assert "Thresholds:" not in result["caption"]
        assert "Mode:" not in result["caption"]
        assert "Reason:" not in result["caption"]

        # No-match case
        result2 = build_match_message(
            match_result={"matched": False},
            vm2_result={"class": "vehicle"},
            camera_label="Cam",
            top_candidates=[("v_a", 5.0)],
            position=3,
        )
        assert "Thresholds:" not in result2["caption"]
        assert "Mode:" not in result2["caption"]
        assert "Reason:" not in result2["caption"]


# ---------------------------------------------------------------------------
# Full message integration tests
# ---------------------------------------------------------------------------


class TestBuildMatchMessageIntegration:
    """Full build_match_message integration tests (US-067c)."""

    def test_match_full_output_format(self):
        """Match message has the new stripped format."""
        match_result = {
            "matched": True,
            "known_vehicle": {
                "id": "v_kv001",
                "label": "Black Truck",
                "color": "black",
                "make": "Ford",
                "model": "F-150",
                "owner": "John",
            },
            "score": 8.5,
            "all_scores": [("v_kv001", 8.5), ("v_other", 3.0)],
        }
        result = build_match_message(
            match_result, {}, camera_label="Front Gate",
            alert=_make_alert(), position=3,
        )
        caption = result["caption"]

        # Verify key lines exist
        assert "✅ Match — *Black Truck*" in caption
        assert "Label: Black Truck" in caption
        assert "Make: Ford" in caption
        assert "Model: F-150" in caption
        assert "Color: black" in caption
        assert "Owner: John" in caption
        assert "Gap: 5.5" in caption  # gap = 8.5 - 3.0
        assert "TG 3 of 3" in caption
        assert "Alert 3 of 3" in caption
        # Alert ID removed per US-073g
        assert "Alert ID:" not in caption

        # Verify debug lines are gone
        assert "Confidence:" not in caption
        assert "Runner-ups:" not in caption
        assert "Thresholds:" not in caption

    def test_match_no_gap_zero(self):
        """When gap is zero (single candidate), no Gap line is emitted."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        result = build_match_message(
            match_result, {}, camera_label="Cam", position=2,
        )
        assert "Gap:" not in result["caption"]

    def test_no_match_output_format(self):
        """No-match message has minimal format with runner-up."""
        result = build_match_message(
            match_result={"matched": False},
            vm2_result={"class": "vehicle"},
            camera_label="Front Gate",
            top_candidates=[("Gray Sedan", 4.2)],
            position=3,
        )
        caption = result["caption"]
        assert "⚠️ No match (confidence below threshold)" in caption
        assert "Runner-up: *Gray Sedan* (4.2)" in caption
        assert "TG 3 of 3" in caption
        # No Thresholds, no Reason, no Status, no Runner-ups (plural)
        assert "Thresholds:" not in caption
        assert "Runner-ups:" not in caption

    def test_no_match_no_candidates(self):
        """No-match with no top_candidates still renders."""
        result = build_match_message(
            match_result={"matched": False},
            vm2_result={"class": "vehicle"},
            camera_label="Front Gate",
            position=3,
        )
        caption = result["caption"]
        assert "⚠️ No match (confidence below threshold)" in caption
        assert "TG 3 of 3" in caption

    def test_alert_metadata_order(self):
        """Alert lines appear after match body, before TG marker."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        result = build_match_message(
            match_result, {}, camera_label="Cam",
            alert=_make_alert(), position=3,
        )
        caption = result["caption"]
        lines = caption.split("\n")
        # Verify order: Camera -> Match block -> Alert lines -> TG marker
        idx_match = next(i for i, l in enumerate(lines) if "Match" in l)
        idx_alert3 = next(i for i, l in enumerate(lines) if "Alert 3 of 3" in l)
        idx_tg = next(i for i, l in enumerate(lines) if "TG 3 of 3" in l)
        assert idx_match < idx_alert3 < idx_tg

    def test_no_alert_no_tg(self):
        """When alert is None and position is None, no alert/TG lines."""
        match_result = {
            "matched": True,
            "known_vehicle": {"id": "v1", "label": "Test"},
            "score": 8.0,
            "all_scores": [("v1", 8.0)],
        }
        result = build_match_message(
            match_result, {}, camera_label="Cam",
            alert=None, position=None,
        )
        assert "Alert" not in result["caption"]
        assert "TG" not in result["caption"]

    def test_animal_match_unchanged(self):
        """Animal matched alert still uses build_animal_alert_body."""
        match_result = {
            "matched": True,
            "candidate": {
                "id": "a_finnegan",
                "label": "Finnegan",
                "species": "cow",
                "breed": "Holstein",
            },
            "cosine_score": 0.9234,
            "tier1_score": 3.125,
        }
        vm2_result = {"class": "animal"}
        result = build_match_message(
            match_result, vm2_result, camera_label="Pasture",
            position=3,
        )
        caption = result["caption"]
        assert "Recognized: Finnegan" in caption
        assert "species: cow" in caption
        assert "cosine" in caption.lower()
        assert "Match" not in caption

    def test_animal_unmatched_unchanged(self):
        """Animal unmatched alert shows no-match emoji header (US-067c)."""
        result = build_match_message(
            match_result={"matched": False},
            vm2_result={"class": "animal"},
            camera_label="Gate",
            position=3,
        )
        caption = result["caption"]
        # US-067c: no-match uses emoji header regardless of class
        assert "⚠️ No match" in caption
        assert "TG 3 of 3" in caption
        assert "Status:" not in caption  # Wait — this should still have the no-match header

    def test_flat_match_result_shape(self):
        """Flat dict (actual runtime shape) works correctly."""
        flat_match = {
            "id": "v_flat",
            "label": "Flat Truck",
            "color": "blue",
            "make": "Chevy",
            "model": "Silverado",
            "owner": "Bob",
            "matched": True,
            "score": 7.5,
            "all_scores": [("v_flat", 7.5), ("v_other", 3.0)],
        }
        result = build_match_message(
            flat_match, {}, camera_label="Cam", position=3,
        )
        caption = result["caption"]
        assert "✅ Match — *Flat Truck*" in caption
        assert "ID: v_flat" in caption
        assert "Label: Flat Truck" in caption
        assert "Color: blue" in caption
        assert "Owner: Bob" in caption
        assert "Make: Chevy" in caption
        assert "Model: Silverado" in caption


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for status_wording_for and pick_alert_image_path."""

    def test_status_wording_recognized(self):
        assert status_wording_for("vehicle", True) == "recognized vehicle"

    def test_status_wording_unrecognized(self):
        assert status_wording_for("vehicle", False) == "unrecognized vehicle"

    def test_status_wording_person(self):
        assert status_wording_for("person", True) == "recognized person"
        assert status_wording_for("person", False) == "unrecognized person"

    def test_pick_alert_image_crop_a(self):
        result = pick_alert_image_path(
            {"better_crop": "crop_a"}, "/a.png", "/b.png"
        )
        assert result == "/a.png"

    def test_pick_alert_image_crop_b(self):
        result = pick_alert_image_path(
            {"better_crop": "crop_b"}, "/a.png", "/b.png"
        )
        assert result == "/b.png"

    def test_pick_alert_image_neither(self):
        result = pick_alert_image_path(
            {"better_crop": "neither"}, "/a.png", "/b.png"
        )
        assert result == "/a.png"

    def test_pick_alert_image_missing(self):
        result = pick_alert_image_path({}, "/a.png", "/b.png")
        assert result == "/a.png"

    def test_pick_alert_image_none_vm2(self):
        result = pick_alert_image_path(None, "/a.png", "/b.png")
        assert result == "/a.png"
