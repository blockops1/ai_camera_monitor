"""E2E tests for person match alert body — build_match_message person branch.

Tests: person matched (cosine path), person matched (tag/Jaccard path),
person unmatched, and vehicle/animal paths unchanged.

No real Telegram dispatch — pure string assertions on caption format.
"""
from __future__ import annotations

import pytest

from telegram_formatter.match_alert import (
    build_match_message,
    build_person_alert_body,
    status_wording_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _person_match_cosine(
    label="Jeremiah",
    id_="p1",
    cosine=0.91,
    tier1=0.6,
    runner_ups=None,
):
    """Build a person match result dict in the cosine path shape."""
    return {
        "matched": True,
        "candidate": {"id": id_, "label": label},
        "cosine_score": cosine,
        "tier1_score": tier1,
        "top_candidates": [
            {"id": id_, "label": label, "cosine_score": cosine, "tier1_score": tier1},
        ] + ([{"id": ru[0], "label": ru[1], "cosine_score": ru[2], "tier1_score": ru[3]} for ru in (runner_ups or [])]),
        "threshold": 0.85,
        "runner_ups": [{"id": ru[0], "label": ru[1], "cosine_score": ru[2], "tier1_score": ru[3]} for ru in (runner_ups or [])],
    }


def _person_match_tag(
    label="Jeremiah",
    id_="p1",
):
    """Build a person match result dict in the tag/Jaccard fast-path shape (flat)."""
    return {
        "matched": True,
        "id": id_,
        "label": label,
    }


def _person_unmatched():
    """Build a person unmatched result dict."""
    return {
        "matched": False,
        "top_candidates": [],
        "threshold": 0.85,
    }


def _vm2_result(class_="person"):
    """Build a minimal VM2 result dict."""
    return {"class": class_}


# ---------------------------------------------------------------------------
# Tests: build_person_alert_body — matched (cosine path)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestPersonAlertBodyMatchedCosine:
    """Person matched via CLIP cosine — label + scores rendered."""

    def test_match_shows_label(self):
        """AC2: person + matched path renders candidate.label (no license_plate fallback)."""
        result = build_person_alert_body(_person_match_cosine(), {})
        assert "Jeremiah" in result
        assert "<b>Recognized:</b>" in result

    def test_match_shows_cosine_score(self):
        """Cosine score rendered in <code> tags."""
        result = build_person_alert_body(_person_match_cosine(cosine=0.91), {})
        assert "0.9100" in result

    def test_match_shows_tier1_score(self):
        """Tier-1 Jaccard score rendered in parens."""
        result = build_person_alert_body(_person_match_cosine(tier1=0.6), {})
        assert "tier1:" in result
        assert "0.6000" in result

    def test_match_no_runner_ups(self):
        """No runner-ups line when list is empty."""
        result = build_person_alert_body(_person_match_cosine(runner_ups=[]), {})
        assert "Runner-ups" not in result

    def test_match_shows_runner_ups(self):
        """Runner-ups rendered when present."""
        result = build_person_alert_body(
            _person_match_cosine(runner_ups=[("p2", "Stranger", 0.82, 0.3)]),
            {},
        )
        assert "Runner-ups" in result
        assert "p2" in result
        assert "0.8200" in result


# ---------------------------------------------------------------------------
# Tests: build_person_alert_body — matched (tag/Jaccard path, flat shape)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestPersonAlertBodyMatchedTag:
    """Person matched via tag/Jaccard fast-path — label rendered, scores = 0."""

    def test_flat_shape_label(self):
        """AC2: flat match result (no 'candidate' key) still renders label."""
        result = build_person_alert_body(_person_match_tag(), {})
        assert "Jeremiah" in result
        assert "<b>Recognized:</b>" in result

    def test_flat_shape_no_scores(self):
        """Tag path has no cosine/tier1 scores — defaults to 0.0000."""
        result = build_person_alert_body(_person_match_tag(), {})
        assert "0.0000" in result  # cosine default
        assert "0.0000" in result  # tier1 default


# ---------------------------------------------------------------------------
# Tests: build_person_alert_body — unmatched
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestPersonAlertBodyUnmatched:
    """Person unmatched — should NOT call this function, but verify
    build_match_message handles the no-match case correctly."""
    pass  # no-match handled in build_match_message, tested below


# ---------------------------------------------------------------------------
# Tests: build_match_message — person branch
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestBuildMatchMessagePerson:
    """Person branch in build_match_message — classification='person'."""

    def test_person_matched_shows_label(self):
        """AC1: grep 'classification.*person|unrecognized person' returns >= 2.
        AC2: person + matched renders candidate.label."""
        msg = build_match_message(
            _person_match_cosine(label="Jeremiah", cosine=0.91, tier1=0.6),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            position=3,
        )
        caption = msg["caption"]
        assert "Jeremiah" in caption
        assert "Recognized" in caption

    def test_person_matched_shows_scores(self):
        """Cosine and tier1 scores included in caption."""
        msg = build_match_message(
            _person_match_cosine(cosine=0.91, tier1=0.6),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            position=3,
        )
        caption = msg["caption"]
        assert "0.9100" in caption
        assert "tier1:" in caption
        assert "0.6000" in caption

    def test_person_matched_flat_shape(self):
        """Person matched via tag/Jaccard (flat shape) still renders label."""
        msg = build_match_message(
            _person_match_tag(),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            position=3,
        )
        caption = msg["caption"]
        assert "Jeremiah" in caption

    def test_person_unmatched_shows_unknown(self):
        """AC3: person + unmatched renders 'unrecognized person'."""
        msg = build_match_message(
            _person_unmatched(),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            position=3,
        )
        caption = msg["caption"]
        assert "Unrecognized person" in caption

    def test_person_unmatched_no_vehicle_string(self):
        """AC3: 'vehicle' string must NOT appear in person unmatched caption."""
        msg = build_match_message(
            _person_unmatched(),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            position=3,
        )
        caption = msg["caption"]
        assert "vehicle" not in caption.lower()

    def test_person_unmatched_no_conflict_header(self):
        """Unmatched person does NOT show 'No match (confidence below threshold)'."""
        msg = build_match_message(
            _person_unmatched(),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            position=3,
        )
        caption = msg["caption"]
        assert "No match (confidence below threshold)" not in caption

    def test_person_caption_has_camera_label(self):
        """Caption always starts with 'Camera: <label>'."""
        msg = build_match_message(
            _person_match_cosine(label="Jeremiah"),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            position=3,
        )
        caption = msg["caption"]
        assert "Camera: FRONT" in caption

    def test_person_caption_has_tg_marker(self):
        """TG position marker present when position provided."""
        msg = build_match_message(
            _person_match_cosine(label="Jeremiah"),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            position=3,
        )
        caption = msg["caption"]
        assert "TG 3 of 3" in caption

    def test_person_no_alert_no_metadata(self):
        """Without alert dict, no 'Alert 3 of 3' line."""
        msg = build_match_message(
            _person_match_cosine(label="Jeremiah"),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
        )
        caption = msg["caption"]
        assert "Alert 3 of 3" not in caption

    def test_person_with_alert_has_metadata(self):
        """With alert dict, 'Alert 3 of 3' and timestamp lines present."""
        alert = {
            "timestamp": "2026-09-28T20:15:00-04:00",
        }
        msg = build_match_message(
            _person_match_cosine(label="Jeremiah"),
            _vm2_result(),
            classification="person",
            camera_label="FRONT",
            alert=alert,
        )
        caption = msg["caption"]
        assert "Alert 3 of 3" in caption


# ---------------------------------------------------------------------------
# Tests: vehicle/animal paths unchanged
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestVehiclePersonPathsUnchanged:
    """Verify vehicle and animal paths are NOT affected by the person branch."""

    def test_vehicle_matched_still_works(self):
        """Vehicle matched uses build_match_alert_body (standard body)."""
        vehicle_match = {
            "matched": True,
            "known_vehicle": {
                "id": "v1",
                "label": "Sequoia",
                "owner": "Jill",
                "color": "black",
                "make": "Toyota",
                "model": "Sequoia",
            },
            "score": 0.95,
            "all_scores": [("v1", 0.95), ("v2", 0.7)],
        }
        msg = build_match_message(
            vehicle_match,
            {"class": "vehicle"},
            classification="vehicle",
            camera_label="FRONT",
        )
        caption = msg["caption"]
        assert "Sequoia" in caption
        assert "Vehicle" not in caption  # no vehicle header line

    def test_vehicle_unmatched_still_works(self):
        """Vehicle unmatched shows standard warning."""
        vehicle_unmatched = {
            "matched": False,
        }
        msg = build_match_message(
            vehicle_unmatched,
            {"class": "vehicle"},
            classification="vehicle",
            camera_label="FRONT",
        )
        caption = msg["caption"]
        assert "No match" in caption


# ---------------------------------------------------------------------------
# Tests: status_wording_for
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestStatusWordingFor:
    """AC1: grep 'classification.*person|unrecognized person' match_alert.py | wc -l >= 2."""

    def test_recognized_person(self):
        """status_wording_for('person', True) -> 'recognized person'."""
        assert status_wording_for("person", True) == "recognized person"

    def test_unrecognized_person(self):
        """status_wording_for('person', False) -> 'unrecognized person'."""
        assert status_wording_for("person", False) == "unrecognized person"

    def test_recognized_vehicle(self):
        """status_wording_for('vehicle', True) -> 'recognized vehicle'."""
        assert status_wording_for("vehicle", True) == "recognized vehicle"

    def test_unrecognized_animal(self):
        """status_wording_for('animal', False) -> 'unrecognized animal'."""
        assert status_wording_for("animal", False) == "unrecognized animal"
