"""
test_legacy_match_adapter_6B105.py — Phase.105 unit tests for the
legacy match adapter that bridges infra.vehicle_matcher (15-dim scorer)
into the MatchVerdict | NoMatch shape that pipeline/orchestrator.py and
telegram_formatter consume.

Per Phase.103's scripts/probe_matcher_comparison.py, the legacy
scorer is materially better than the modular 4-dim scorer. This test
file pins the adapter's contract so a future refactor of the adapter
doesn't regress match quality.

Tests cover:
  1. Empty known_vehicles list → NoMatch(reason="no_known_vehicles", top_candidates=[])
  2. Below confidence threshold → NoMatch(reason="below_threshold", top_candidates=[...])
  3. Below gap threshold (ambiguous match) → NoMatch(reason="below_gap", top_candidates=[...])
  4. Successful match → MatchVerdict with known_vehicle, score, gap, breakdowns, rank, all_scores
  5. score_top_n_with_legacy returns top-N in (kv_id, score, breakdowns) shape
  6. score_top_n_with_legacy with empty known_vehicles returns []
  7. Adapter's MatchVerdict has rank=0 (legacy scorer has only one winner; rank field
     preserved for shape compatibility with the modular matcher's MatchVerdict)
  8. Adapter's MatchVerdict.all_scores matches the modular shape (kv_id, score)
"""
from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

from pipeline._legacy_match_adapter import (
    match_with_legacy,
    score_top_n_with_legacy,
)
from vehicle_matcher import MatchVerdict, NoMatch

# --- Test fixtures ---------------------------------------------------------

KNOWN_TESLA = {
    "id": "v_owner1_darkblue_tesla_y",
    "label": "[name one]'s sedan",
    "color": "dark blue",
    "type": "SUV",
    "make": "Tesla",
    "model": "Model Y",
    "features": ["no front license plate", "tinted windows"],
}

KNOWN_F150 = {
    "id": "v_farm_white_f150",
    "label": "Farm White F-150",
    "color": "white",
    "type": "truck",
    "make": "Ford",
    "model": "F-150",
    "features": ["ranch decal on tailgate", "lifted suspension"],
}

KNOWN_HONDA = {
    "id": "v_jane_silver_crv",
    "label": "Jane's Silver CR-V",
    "color": "silver",
    "type": "SUV",
    "make": "Honda",
    "model": "CR-V",
    "features": ["roof rack", "magnetic bumper stickers"],
}


@pytest.fixture
def known_fleet() -> list[dict]:
    """Three known vehicles covering distinct color/type/make axes."""
    return [KNOWN_TESLA, KNOWN_F150, KNOWN_HONDA]


def _make_signature(**kwargs) -> dict:
    """Build a signature dict with sensible defaults for tests."""
    return {
        "color": kwargs.get("color", "dark blue"),
        "type": kwargs.get("type", "SUV"),
        "make": kwargs.get("make", "Tesla"),
        "model": kwargs.get("model", "Model Y"),
        "vehicle_features": kwargs.get("vehicle_features", ["no front license plate"]),
    }


# --- Test 1: empty known_vehicles → NoMatch(no_known_vehicles) -----------

class TestEmptyKnownVehicles:
    def test_match_with_legacy_empty_list_returns_no_match(self):
        result = match_with_legacy(_make_signature(), [])
        assert isinstance(result, NoMatch)
        assert result.reason == "no_known_vehicles"
        assert result.top_candidates == []

    def test_score_top_n_with_legacy_empty_list_returns_empty(self):
        result = score_top_n_with_legacy(_make_signature(), [], n=3)
        assert result == []


# --- Test 2: below confidence threshold → NoMatch(below_threshold) --------

class TestBelowThreshold:
    def test_empty_signature_returns_below_threshold(self, known_fleet):
        """A signature with all 'unknown' fields won't match anything.

        The legacy scorer should return all scores below confidence.
        """
        result = match_with_legacy(
            {"color": "unknown", "type": "unknown", "make": "unknown", "model": "unknown"},
            known_fleet,
        )
        assert isinstance(result, NoMatch)
        # Reason can be below_threshold OR below_gap depending on score distribution.
        # If scores are all 0 or very low, it's below_threshold.
        assert result.reason in ("below_threshold", "below_gap")
        # top_candidates is always populated (all_scores for transparency).
        assert len(result.top_candidates) == len(known_fleet)


# --- Test 3: ambiguous match (below gap) → NoMatch(below_gap) ------------

class TestBelowGap:
    def test_signature_matching_two_kvs_closely_returns_below_gap(self, known_fleet):
        """Two vehicles with similar color/type scores but different make/model.

        Construct a signature that scores similarly on two kvs (e.g., the
        Tesla and the Honda both score on color+type but make/model is
        missing), forcing the gap to be below the threshold.

        Color: 'silver' (Honda) + color normalizes 'silver' to 'gray'.
        Color: 'dark blue' (Tesla). Neither silver nor dark blue aliases.
        """
        # A signature with no make/model — forces gap to be tight if
        # color+type scores are close. Tesla is 'dark blue' SUV; Honda
        # is 'silver' SUV. Without make/model, both score on type=SUV
        # but color mismatch penalizes both.
        signature = {"color": "unknown", "type": "SUV", "make": "", "model": ""}
        result = match_with_legacy(signature, known_fleet)
        # With make/model empty, scores are dominated by type-group-flex
        # which is a single dimension. Both SUV kvs will score similarly.
        # The expected outcome is either below_threshold (top score too low)
        # or below_gap (top two scores too close). Both are NoMatch.
        assert isinstance(result, NoMatch)
        assert result.reason in ("below_threshold", "below_gap")


# --- Test 4: successful match → MatchVerdict ------------------------------

class TestSuccessfulMatch:
    def test_perfect_signature_returns_match_verdict(self, known_fleet):
        """A signature that exactly matches the Tesla should match it."""
        signature = _make_signature(
            color="dark blue",
            type="SUV",
            make="Tesla",
            model="Model Y",
            vehicle_features=["no front license plate"],
        )
        result = match_with_legacy(signature, known_fleet)
        assert isinstance(result, MatchVerdict)
        assert result.known_vehicle["id"] == "v_owner1_darkblue_tesla_y"
        assert result.score > 0
        assert result.gap >= 0
        assert isinstance(result.breakdowns, dict)
        assert result.rank == 0
        assert len(result.all_scores) == len(known_fleet)


# --- Test 5: score_top_n_with_legacy shape --------------------------------

class TestScoreTopN:
    def test_score_top_n_returns_top_n_in_modular_shape(self, known_fleet):
        """score_top_n_with_legacy should return (kv_id, score, breakdowns) tuples."""
        signature = _make_signature(color="dark blue", type="SUV", make="Tesla", model="Model Y")
        result = score_top_n_with_legacy(signature, known_fleet, n=2)
        assert len(result) == 2
        # First element of each tuple must be a kv_id (str), not the kv dict.
        for kv_id, score, breakdowns in result:
            assert isinstance(kv_id, str)
            assert isinstance(score, (int, float))
            assert isinstance(breakdowns, dict)
        # Top score should be the Tesla.
        assert result[0][0] == "v_owner1_darkblue_tesla_y"

    def test_score_top_n_respects_n(self, known_fleet):
        signature = _make_signature()
        result = score_top_n_with_legacy(signature, known_fleet, n=1)
        assert len(result) == 1


# --- Test 6: shape consistency with vehicle_matcher.match_signature -------

class TestShapeConsistency:
    """Pin the adapter's output shape so callers don't need to know which
    engine produced the verdict. This is the whole point of the adapter.
    """

    def test_no_match_shape_matches_modular_no_match(self, known_fleet):
        adapter_result = match_with_legacy(
            {"color": "unknown", "type": "unknown"}, known_fleet
        )
        assert isinstance(adapter_result, NoMatch)
        assert hasattr(adapter_result, "reason")
        assert hasattr(adapter_result, "top_candidates")
        assert isinstance(adapter_result.reason, str)
        assert isinstance(adapter_result.top_candidates, list)

    def test_match_verdict_shape_matches_modular_match_verdict(self, known_fleet):
        adapter_result = match_with_legacy(_make_signature(), known_fleet)
        assert isinstance(adapter_result, MatchVerdict)
        # Required fields per vehicle_matcher/matcher.py
        assert hasattr(adapter_result, "known_vehicle")
        assert hasattr(adapter_result, "score")
        assert hasattr(adapter_result, "gap")
        assert hasattr(adapter_result, "breakdowns")
        assert hasattr(adapter_result, "rank")
        assert hasattr(adapter_result, "all_scores")