"""Tests for telegram_formatter.match_alert — animal-aware match alert paths (US-045e).

Covers:
  - build_match_alert_body — vehicle/person (unchanged, not re-tested here)
  - build_animal_alert_body — matched animal rendering (label, species, breed, scores)
  - build_match_message — animal + matched (uses build_animal_alert_body)
  - build_match_message — animal + unmatched ("unrecognized animal")
  - build_match_message — no "vehicle" string in animal paths

AC2: animal + matched path renders candidate.label (no license_plate fallback)
AC3: animal + unmatched path renders 'unrecognized animal' (no 'vehicle' string anywhere)
"""

from __future__ import annotations

from telegram_formatter.match_alert import (
    build_animal_alert_body,
    build_match_message,
    status_wording_for,
)

# ---------------------------------------------------------------------------
# status_wording_for — animal variants (already in test_match_alert_status_wording,
# but included here for completeness on the animal path).
# ---------------------------------------------------------------------------


class TestStatusWordingAnimal:
    """Animal status wording via status_wording_for."""

    def test_animal_recognized(self) -> None:
        assert status_wording_for("animal", True) == "recognized animal"

    def test_animal_unrecognized(self) -> None:
        assert status_wording_for("animal", False) == "unrecognized animal"


# ---------------------------------------------------------------------------
# build_animal_alert_body — matched animal rendering
# ---------------------------------------------------------------------------


class TestBuildAnimalAlertBody:
    """Verify build_animal_alert_body output for matched animal alerts."""

    def test_basic_animal_match(self) -> None:
        """Matched animal renders label, species, breed."""
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
        body = build_animal_alert_body(match_result, {})
        assert "Recognized: Finnegan" in body
        assert "species: cow" in body
        assert "breed: Holstein" in body
        assert "0.9234" in body
        assert "3.125" in body
        # No vehicle fields
        assert "license_plate" not in body
        assert "Make/Model" not in body
        assert "Owner:" not in body
        assert "Body:" not in body

    def test_animal_match_with_runner_ups(self) -> None:
        """Matched animal with runner-ups renders them."""
        match_result = {
            "matched": True,
            "candidate": {
                "id": "a_bessie",
                "label": "Bessie",
                "species": "cow",
                "breed": "Jersey",
            },
            "cosine_score": 0.88,
            "tier1_score": 3.0,
            "runner_ups": [
                {"id": "a_daisy", "cosine_score": 0.75, "tier1_score": 2.5},
                {"id": "a_dolly", "cosine_score": 0.68, "tier1_score": 2.0},
            ],
        }
        body = build_animal_alert_body(match_result, {})
        assert "Bessie" in body
        assert "Runner-ups:" in body
        assert "daisy" in body.lower() or "Daisy" in body
        assert "cosine=" in body
        assert "tier1=" in body

    def test_animal_match_minimal_candidate(self) -> None:
        """Matched animal with minimal candidate uses '?' and 'unknown' fallbacks."""
        match_result = {
            "matched": True,
            "candidate": {
                "id": "a_unknown",
            },
            "cosine_score": 0.5,
            "tier1_score": 1.0,
        }
        body = build_animal_alert_body(match_result, {})
        assert "Recognized: ?" in body
        assert "species: unknown" in body
        assert "breed: unknown" in body

    def test_animal_match_no_scores(self) -> None:
        """Matched animal with missing scores uses defaults (0.0)."""
        match_result = {
            "matched": True,
            "candidate": {
                "label": "Rover",
                "species": "dog",
                "breed": "Labrador",
            },
        }
        body = build_animal_alert_body(match_result, {})
        assert "Recognized: Rover" in body
        assert "Cosine: 0.0000" in body
        assert "tier1: 0.0000" in body


# ---------------------------------------------------------------------------
# build_match_message — animal + matched
# ---------------------------------------------------------------------------


class TestMatchMessageAnimalMatched:
    """build_match_message animal + matched path."""

    def test_animal_match_includes_label_and_scores(self) -> None:
        """Animal matched alert shows Recognized + species + breed + scores."""
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
        result = build_match_message(match_result, vm2_result, camera_label="Pasture Cam")
        caption = result["caption"]
        assert "Recognized: Finnegan" in caption
        assert "species: cow" in caption
        assert "breed: Holstein" in caption
        assert "Cosine: 0.9234" in caption
        assert "tier1: 3.1250" in caption

    def test_animal_match_no_vehicle_fallback(self) -> None:
        """Animal matched alert must NOT render license_plate / Make/Model / Body fields."""
        match_result = {
            "matched": True,
            "candidate": {
                "id": "a_001",
                "label": "Buster",
                "species": "horse",
                "breed": "Thoroughbred",
                "license_plate": "SHOULD_NOT_APPEAR",  # deliberate pollution
            },
            "cosine_score": 0.95,
            "tier1_score": 3.5,
        }
        vm2_result = {"class": "animal"}
        result = build_match_message(match_result, vm2_result, camera_label="Gate")
        caption = result["caption"]
        assert "License Plate" not in caption
        assert "Make/Model" not in caption
        assert "Body:" not in caption

    def test_animal_match_with_alert_metadata(self) -> None:
        """Animal matched alert with alert dict includes Alert 3 of 3 metadata."""
        match_result = {
            "matched": True,
            "candidate": {
                "id": "a_002",
                "label": "Clover",
                "species": "goat",
                "breed": "Nubian",
            },
            "cosine_score": 0.88,
            "tier1_score": 3.0,
        }
        alert = {"id": "evt-animal-001", "timestamp": "2026-09-14T12:00:00Z"}
        vm2_result = {"class": "animal"}
        result = build_match_message(
            match_result, vm2_result, camera_label="Barn Cam", alert=alert,
        )
        caption = result["caption"]
        assert "Alert 3 of 3" in caption
        assert "Alert ID: evt-animal-001" in caption
        assert "Recognized: Clover" in caption


# ---------------------------------------------------------------------------
# build_match_message — animal + unmatched
# ---------------------------------------------------------------------------


class TestMatchMessageAnimalUnmatched:
    """build_match_message animal + unmatched path."""

    def test_animal_unrecognized_shows_status(self) -> None:
        """Unmatched animal alert shows 'unrecognized animal'."""
        match_result = {"matched": False}
        vm2_result = {"class": "animal"}
        result = build_match_message(match_result, vm2_result, camera_label="Gate")
        caption = result["caption"]
        assert "unrecognized animal" in caption
        assert "Status: unrecognized animal" in caption

    def test_animal_unmatched_no_vehicle_string(self) -> None:
        """Unmatched animal must not contain 'vehicle' or 'Recognized:'. """
        match_result = {"matched": False}
        vm2_result = {"class": "animal"}
        result = build_match_message(match_result, vm2_result, camera_label="Gate")
        caption = result["caption"]
        assert "vehicle" not in caption.lower().replace("unrecognized vehicle", "")
        # Make sure 'Recognized:' doesn't appear (it's a match-only prefix)
        assert "Recognized:" not in caption
        # The word 'vehicle' must not appear at all in the caption
        assert "vehicle" not in caption

    def test_animal_unrecognized_with_top_candidates(self) -> None:
        """Unmatched animal with top_candidates shows full no-match body."""
        match_result = {"matched": False}
        vm2_result = {"class": "animal"}
        result = build_match_message(
            match_result,
            vm2_result,
            camera_label="Gate",
            classification="animal",
            reason="below confidence threshold",
            top_candidates=[("a_001", 0.72), ("a_002", 0.65)],
            match_threshold=0.85,
            gap_threshold=0.1,
        )
        caption = result["caption"]
        assert "unrecognized animal" in caption
        assert "below confidence threshold" in caption

    def test_animal_unmatched_default_class_fallback(self) -> None:
        """When vm2_result has no 'class' key, classification defaults to 'vehicle'.
        This ensures the animal path only triggers when 'class' is explicitly 'animal'."""
        match_result = {"matched": False}
        vm2_result = {}  # no class key
        result = build_match_message(match_result, vm2_result, camera_label="Gate")
        caption = result["caption"]
        # Should fall back to vehicle, NOT animal
        assert "unrecognized vehicle" in caption
        assert "unrecognized animal" not in caption


# ---------------------------------------------------------------------------
# build_match_message — animal vs vehicle path divergence
# ---------------------------------------------------------------------------


class TestVehicleVsAnimalDivergence:
    """Verify vehicle and animal paths diverge correctly."""

    def test_vehicle_match_unchanged(self) -> None:
        """Vehicle matched alert still uses build_match_alert_body (label, confidence, gap)."""
        match_result = {
            "matched": True,
            "known_vehicle": {
                "id": "v_test",
                "label": "Red Ford F150",
                "color": "red",
                "make": "Ford",
                "model": "F150",
            },
            "score": 8.5,
            "all_scores": [("v_test", 8.5), ("v_other", 5.5)],
        }
        vm2_result = {"class": "vehicle"}
        result = build_match_message(match_result, vm2_result, camera_label="Gate")
        caption = result["caption"]
        assert "Match" in caption
        assert "Red Ford F150" in caption
        assert "Confidence: 8.5" in caption
        assert "gap: 3.00" in caption
        assert "Recognized:" not in caption

    def test_animal_match_uses_recognized_not_match(self) -> None:
        """Animal matched alert uses 'Recognized:' prefix, not 'Match —'."""
        match_result = {
            "matched": True,
            "candidate": {
                "id": "a_001",
                "label": "Spot",
                "species": "dog",
                "breed": "Beagle",
            },
            "cosine_score": 0.91,
            "tier1_score": 3.2,
        }
        vm2_result = {"class": "animal"}
        result = build_match_message(match_result, vm2_result, camera_label="Gate")
        caption = result["caption"]
        assert "Recognized: Spot" in caption
        assert "Match" not in caption
        assert "Confidence:" not in caption  # animal uses Cosine, not Confidence
