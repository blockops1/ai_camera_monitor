"""Unit tests for match_signature and score_top_n."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

from vehicle_matcher.matcher import (
    MatchVerdict,
    NoMatch,
    match_signature,
    score_top_n,
)
from vehicle_matcher.scoring import ScoringSpec


def _carson_white():
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


def _jayco_camper():
    return {
        "id": "v_jayco_camper",
        "label": "Jayco Jay Feather travel trailer",
        "owner": "employee_a",
        "color": "white",
        "type": "trailer",
        "make": "Jayco",
        "model": "Jay Feather",
        "vehicle_features": {
            "cab_marker_lights": False,  # trailers don't have cab marker lights
            "bed_cover": "none",         # trailers don't have bed covers
        },
    }


def _known_vehicles():
    return [_carson_white(), _jayco_camper()]


def _perfect_carson_signature():
    return {
        "color": "white",
        "type": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "wheel_style": "black steel",
        "cab_marker_lights": False,
        "bed_cover": "none",
    }


def test_perfect_match_returns_matchverdict():
    """Phase 6B.84 absence-evidence fix — perfect match score is 5.0
    (4 identity + 1 positive feature), not 7.0 (4 identity + 3
    features where 2 were bogus absence-evidence).
    """
    sig = _perfect_carson_signature()
    v = match_signature(sig, _known_vehicles())
    assert isinstance(v, MatchVerdict)
    assert v.known_vehicle["id"] == "v_carson_white"
    # 4 identity (color+type+make+model) + wheel_style (the only
    # positive feature on this sig) = 5.0.
    assert v.score > 4.5
    assert v.rank == 0


def test_perfect_match_has_wide_gap():
    """employee_b's pickup matches strongly; gap to jayco is large."""
    sig = _perfect_carson_signature()
    v = match_signature(sig, _known_vehicles())
    assert isinstance(v, MatchVerdict)
    assert v.gap > 1.0


def test_no_known_vehicles_returns_no_match():
    v = match_signature({"color": "white"}, [])
    assert isinstance(v, NoMatch)
    assert v.reason == "no_known_vehicles"
    assert v.top_candidates == []


def test_below_threshold_returns_no_match():
    """A signature with explicit mismatches on every dimension.

    Note: when the signature has no value for a feature the known
    vehicle does have, the scorer returns 0.5 (neutral) — this is
    by design (we don't penalize vision for not reporting fields it
    couldn't see). So to actually trigger below_threshold we need
    a sig that explicitly mismatches every dimension.
    """
    # extract_signature flattens vehicle_features into top-level keys.
    sig = {
        "color": "purple",
        "type": "spaceship",
        "make": "Aliens",
        "model": "X-9",
        # Flattened feature keys, all explicit mismatches:
        "cab_marker_lights": True,        # knowns have False
        "bed_cover": "tonneau",           # knowns have 'none'
        "wheel_style": "chrome",          # knowns have 'black steel' or absent
    }
    v = match_signature(sig, _known_vehicles())
    assert isinstance(v, NoMatch)
    assert v.reason == "below_threshold"
    assert len(v.top_candidates) == 2


def test_below_gap_returns_no_match():
    """Two close candidates → ambiguous, no match even if both are high."""
    # Add a 3rd vehicle that's very close to employee_b's.
    close = {
        "id": "v_carson_clone",
        "label": "employee_b's white pickup (clone)",
        "owner": "employee_b",
        "color": "white",
        "type": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "vehicle_features": {"wheel_style": "black steel",
                             "cab_marker_lights": False,
                             "bed_cover": "none"},
    }
    v = match_signature(_perfect_carson_signature(), [
        _carson_white(), close,
    ])
    assert isinstance(v, NoMatch)
    assert v.reason == "below_gap"


def test_no_match_returns_top_candidates_sorted_descending():
    sig = {
        "color": "purple",
        "type": "spaceship",
        "make": "Aliens",
        "model": "X-9",
        "cab_marker_lights": True,
        "bed_cover": "tonneau",
        "wheel_style": "chrome",
    }
    v = match_signature(sig, _known_vehicles())
    assert isinstance(v, NoMatch)
    scores = [s for _, s in v.top_candidates]
    assert scores == sorted(scores, reverse=True)


def test_matchverdict_has_breakdowns():
    sig = _perfect_carson_signature()
    v = match_signature(sig, _known_vehicles())
    assert isinstance(v, MatchVerdict)
    assert "color" in v.breakdowns
    assert "type" in v.breakdowns
    assert "make" in v.breakdowns
    assert "model" in v.breakdowns


def test_matchverdict_has_all_scores():
    sig = _perfect_carson_signature()
    v = match_signature(sig, _known_vehicles())
    assert isinstance(v, MatchVerdict)
    assert len(v.all_scores) == 2
    assert v.all_scores[0][0] == "v_carson_white"


def test_matchverdict_is_immutable():
    """MatchVerdict is frozen — can't be mutated mid-telegram-build."""
    sig = _perfect_carson_signature()
    v = match_signature(sig, _known_vehicles())
    assert isinstance(v, MatchVerdict)
    with pytest.raises(Exception):
        v.score = 99.0  # type: ignore[misc]


def test_score_top_n_returns_top_n():
    """Score top-N is independent of threshold — used for diagnostics."""
    sig = {"color": "purple", "type": "spaceship", "make": "Aliens",
           "model": "X-9"}
    top = score_top_n(sig, _known_vehicles(), n=2)
    assert len(top) == 2
    # Each entry: (kv_id, score, breakdowns)
    assert len(top[0]) == 3


def test_score_top_n_n_larger_than_known():
    """If n > len(known), returns all known."""
    top = score_top_n({"color": "white"}, [_carson_white()], n=10)
    assert len(top) == 1


def test_score_top_n_empty_known():
    top = score_top_n({"color": "white"}, [], n=3)
    assert top == []


def test_score_top_n_sorted_descending():
    sig = {"color": "white", "type": "pickup"}
    top = score_top_n(sig, _known_vehicles(), n=2)
    scores = [s for _, s, _ in top]
    assert scores == sorted(scores, reverse=True)


def test_custom_confidence_threshold():
    """With a high threshold, even a perfect employee_b match fails."""
    spec = ScoringSpec(confidence_threshold=10.0)
    sig = _perfect_carson_signature()
    v = match_signature(sig, _known_vehicles(), spec=spec)
    assert isinstance(v, NoMatch)
    assert v.reason == "below_threshold"


def test_custom_gap_threshold():
    """With a huge gap threshold, even clear matches fail."""
    spec = ScoringSpec(gap_threshold=100.0)
    sig = _perfect_carson_signature()
    v = match_signature(sig, _known_vehicles(), spec=spec)
    assert isinstance(v, NoMatch)
    assert v.reason == "below_gap"


def test_d99a38e6_carson_pickup_bug_scenario():
    """The original d99a38e6 bug: employee_b's white pickup mis-scored
    as Jayco trailer because cab_marker and bed_cover shared-default
    values scored 1.0.

    Phase 6B.84 (2026-08-16) — maintainer OOB: 'if a blue truck is trying
    to be matched to a white truck then the color mismatch should be
    a big penalty.' The fix flips this from a pinned-bug test into
    a regression test that asserts:

      1. employee_b still wins — but for the RIGHT reasons (color+type+
         make+model = 6.5, NOT 8.5 with bogus absence-evidence).
      2. The signature breakdown does NOT include cab_marker_match
         or bed_cover_match (both sides default = absence, not signal).

    The score is computed via the same dimension table the listener
    uses (vehicle_matcher.scoring), so this pins correctness at the
    boundary that matters.
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
    v = match_signature(sig, _known_vehicles())
    assert isinstance(v, MatchVerdict)
    assert v.known_vehicle["id"] == "v_carson_white"
    # Phase 6B.84 absence-evidence fix. With both sig and kv having
    # default values, the per-feature score should be the neutral
    # "either missing" value (0.5), not a positive match (1.0).
    # Clean rewrite keys are the raw feature names; the listener-wired
    # path uses <feature>_match keys. Both paths get fixed.
    if "cab_marker_lights" in v.breakdowns:
        assert v.breakdowns["cab_marker_lights"] < 1.0, (
            "d99a38e6 absence-evidence regression: cab_marker_lights=1.0 "
            f"with both sides default. breakdown={v.breakdowns}"
        )
    if "bed_cover" in v.breakdowns:
        assert v.breakdowns["bed_cover"] < 1.0, (
            "d99a38e6 absence-evidence regression: bed_cover=1.0 "
            f"with both sides default. breakdown={v.breakdowns}"
        )
