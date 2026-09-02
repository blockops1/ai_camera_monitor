"""Unit tests for per-dimension scoring functions."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

from vehicle_matcher.scoring import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_GAP_THRESHOLD,
    ScoringSpec,
    score_color,
    score_feature,
    score_features,
    score_make,
    score_model,
    score_signature_against_known,
    score_type,
)

# --- score_color ------------------------------------------------------------


def test_score_color_exact_match():
    assert score_color("white", "white") == 1.0


def test_score_color_mismatch():
    assert score_color("white", "blue") == 0.0


def test_score_color_either_missing_returns_neutral():
    assert score_color(None, "white") == 0.5
    assert score_color("white", None) == 0.5


def test_score_color_both_missing_returns_neutral():
    assert score_color(None, None) == 0.5


def test_score_color_normalizes_case():
    assert score_color("WHITE", "white") == 1.0


def test_score_color_treats_null_string_as_missing():
    assert score_color("null", "white") == 0.5
    assert score_color("white", "null") == 0.5


def test_score_color_treats_empty_string_as_missing():
    assert score_color("", "white") == 0.5


def test_score_color_normalizes_whitespace():
    assert score_color("  white  ", "white") == 1.0


# --- score_make / score_type ----------------------------------------------


def test_score_make_exact_match():
    assert score_make("GMC", "GMC") == 1.0


def test_score_make_mismatch():
    assert score_make("GMC", "Ford") == 0.0


def test_score_make_either_missing_neutral():
    assert score_make(None, "GMC") == 0.5


def test_score_type_exact_match():
    assert score_type("pickup", "pickup") == 1.0


def test_score_type_mismatch():
    assert score_type("pickup", "sedan") == 0.0


def test_score_type_either_missing_neutral():
    assert score_type(None, "pickup") == 0.5


# --- score_model ------------------------------------------------------------


def test_score_model_exact_match():
    assert score_model("F-150", "F-150") == 1.0


def test_score_model_substring_match():
    assert score_model("F-150", "F-150 XLT") == 0.8
    assert score_model("F-150 XLT", "F-150") == 0.8


def test_score_model_no_substring_mismatch():
    assert score_model("F-150", "Silverado") == 0.0


def test_score_model_either_missing_neutral():
    assert score_model(None, "F-150") == 0.5


# --- score_feature ----------------------------------------------------------


def test_score_feature_exact_match():
    assert score_feature("wheel_style", "alloy", "alloy") == 1.0


def test_score_feature_mismatch():
    assert score_feature("wheel_style", "alloy", "steel") == 0.0


def test_score_feature_either_missing_neutral():
    assert score_feature("wheel_style", None, "alloy") == 0.5
    assert score_feature("wheel_style", "alloy", None) == 0.5


def test_score_feature_both_missing_neutral():
    assert score_feature("wheel_style", None, None) == 0.5


def test_score_feature_cab_marker_both_false_does_not_match():
    """Phase 6B.84 absence-evidence fix (d99a38e6).

    Both sig and kv saying False (or 'false') is the absence of
    evidence on both sides, not a positive match. Was 1.0, now 0.0.
    """
    assert score_feature("cab_marker_lights", False, False) == 0.0
    assert score_feature("cab_marker_lights", "false", "false") == 0.0


def test_score_feature_cab_marker_one_present_mismatch():
    """One absent and one present = mismatch."""
    assert score_feature("cab_marker_lights", False, True) == 0.0
    assert score_feature("cab_marker_lights", True, False) == 0.0


def test_score_feature_bed_cover_both_none_does_not_match():
    """Phase 6B.84 — bed_cover='none' == 'none' is absence-evidence.

    Was 1.0, now 0.0.
    """
    assert score_feature("bed_cover", "none", "none") == 0.0


def test_score_feature_bed_cover_one_present_mismatch():
    assert score_feature("bed_cover", "none", "tonneau") == 0.0


def test_score_feature_normalizes_null_string():
    assert score_feature("wheel_style", "null", "alloy") == 0.5


# --- score_features ---------------------------------------------------------


def test_score_features_empty_known_returns_empty():
    sig = {"wheel_style": "alloy"}
    assert score_features(sig, {}) == {}


def test_score_features_scores_overlap():
    sig = {
        "wheel_style": "alloy",
        "wheel_color": "silver",
        "extra_field": "x",  # not in known
    }
    known = {
        "wheel_style": "alloy",
        "wheel_color": "black",
        "bed_cover": "none",
    }
    scores = score_features(sig, known)
    assert scores["wheel_style"] == 1.0
    assert scores["wheel_color"] == 0.0
    assert scores["bed_cover"] == 0.5   # missing from sig
    assert "extra_field" not in scores  # not in known


def test_score_features_with_explicit_keys():
    sig = {"wheel_style": "alloy", "wheel_color": "silver"}
    known = {"wheel_style": "alloy", "wheel_color": "silver"}
    # Pass keys not in known — should be ignored.
    scores = score_features(sig, known, feature_keys=["nope"])
    assert scores == {}


# --- score_signature_against_known -----------------------------------------


def _known_white_pickup():
    return {
        "id": "v_carson_white",
        "label": "employee_b's white pickup",
        "owner": "employee_b",
        "color": "white",
        "type": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "vehicle_features": {
            "wheel_style": "black steel",
            "cab_marker_lights": False,
            "bed_cover": "none",
        },
    }


def test_score_signature_against_known_perfect_match():
    """Phase 6B.84 absence-evidence fix.

    Pre-6B.84 the score was 7.0 because cab_marker_lights=False + bed_cover
    ='none' on both sides counted as 1.0 each (absence-evidence).
    Post-6B.84 only the 4 identity dimensions + wheel_style count, = 5.0.
    """
    sig = {
        "color": "white",
        "type": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "wheel_style": "black steel",
        "cab_marker_lights": False,
        "bed_cover": "none",
    }
    total, breakdowns = score_signature_against_known(sig, _known_white_pickup())
    # 4 identity dimensions × 1.0 + 1 positive feature (wheel_style) × 1.0 = 5.0
    # cab_marker_lights + bed_cover no longer count (Phase 6B.84).
    assert total == pytest.approx(5.0)
    assert breakdowns["color"] == 1.0
    assert breakdowns["type"] == 1.0
    assert breakdowns["make"] == 1.0
    assert breakdowns["model"] == 1.0
    assert breakdowns["wheel_style"] == 1.0
    # cab_marker_lights/bed_cover are still in the breakdown dict but
    # score 0.0 (absence-evidence fix).
    assert breakdowns["cab_marker_lights"] == 0.0
    assert breakdowns["bed_cover"] == 0.0


def test_score_signature_against_known_partial_match():
    sig = {
        "color": "blue",      # mismatch
        "type": "pickup",     # match
        "make": None,         # missing
        "model": "F-150",     # mismatch
        "wheel_style": "alloy",  # mismatch
    }
    total, breakdowns = score_signature_against_known(sig, _known_white_pickup())
    assert breakdowns["color"] == 0.0
    assert breakdowns["type"] == 1.0
    assert breakdowns["make"] == 0.5
    assert breakdowns["model"] == 0.0
    assert breakdowns["wheel_style"] == 0.0
    # 0 + 1 + 0.5 + 0 + 0 + (cab_marker_lights missing→0.5) + (bed_cover missing→0.5)
    expected = 0.0 + 1.0 + 0.5 + 0.0 + 0.0 + 0.5 + 0.5
    assert total == pytest.approx(expected)


def test_score_signature_against_known_weights_override():
    sig = {"color": "white", "type": "pickup", "make": "GMC", "model": "Sierra 1500"}
    spec = ScoringSpec(weights={"color": 2.0, "type": 0.5})
    total, _ = score_signature_against_known(sig, _known_white_pickup(), spec=spec)
    # color 1.0*2.0=2.0 + type 1.0*0.5=0.5 + make 1.0=1.0 + model 1.0=1.0
    # + cab_marker_lights missing→0.5 + bed_cover missing→0.5 + wheel_style missing→0.5
    # = 6.0
    assert total == pytest.approx(6.0)


def test_score_signature_against_known_no_features():
    known = {"id": "x", "color": "white", "type": "pickup",
             "make": "Ford", "model": "F-150"}
    sig = {"color": "white", "type": "pickup", "make": "Ford", "model": "F-150"}
    total, breakdowns = score_signature_against_known(sig, known)
    assert total == pytest.approx(4.0)
    assert "wheel_style" not in breakdowns


# --- Spec defaults ----------------------------------------------------------


def test_default_confidence_threshold():
    assert DEFAULT_CONFIDENCE_THRESHOLD == 0.6


def test_default_gap_threshold():
    assert DEFAULT_GAP_THRESHOLD == 0.15


def test_scoring_spec_defaults():
    spec = ScoringSpec()
    assert spec.confidence_threshold == 0.6
    assert spec.gap_threshold == 0.15
    assert spec.weights == {}


def test_scoring_spec_custom_values():
    spec = ScoringSpec(
        confidence_threshold=0.8,
        gap_threshold=0.25,
        weights={"color": 2.0},
    )
    assert spec.confidence_threshold == 0.8
    assert spec.gap_threshold == 0.25
    assert spec.weights == {"color": 2.0}


def test_scoring_spec_is_immutable():
    """ScoringSpec is frozen — can't be mutated mid-match."""
    spec = ScoringSpec()
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        spec.confidence_threshold = 0.99  # type: ignore[misc]
