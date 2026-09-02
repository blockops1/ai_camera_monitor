"""
Tests for infra/matcher_scoring.py — per-dimension scoring engine.

Pure unit tests. No I/O, no vision, no Telegram.

Covered:
    - score_vehicle aggregates per-dimension weights correctly
    - score_vehicle returns (0.0, {}) when no dimensions match
    - score_vehicle applies negative weights (color_mismatch penalty)
    - score_vehicle skips dimensions with weight=0 in spec
    - score_vehicle swallows per-dimension exceptions without raising
    - DIMENSION_FUNCTIONS has 23 entries with the expected keys
    - DEFAULT_DIMENSION_WEIGHTS has matching keys (positive + negative
      weight dimensions cover same set)

    - _normalize_color maps navy -> blue, dark_blue -> blue, etc.
    - _normalize_color returns None for None or empty input

    - _build_type_to_group inverts {group: [types]} correctly

    - _dim_color_match fires when normalized colors equal
    - _dim_color_alt_match fires when sig color matches one of kv.colors_alt
    - _dim_make_match / _dim_model_match substring & exact matching
    - _dim_model_in_label fires when normalized model is in label
    - _dim_distinctive_keyword fires when kv keyword matches sig evidence
    - _dim_color_mismatch fires only when no colors_alt overlap
    - _dim_color_alt_mismatch requires kv.colors_alt to be present
    - _dim_bed_cover_match handles topper == camper_shell alias
    - _dim_feature_match handles flat + nested sig.vehicle_features

    - score_vehicle() does NOT mutate the input kv dict
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from infra.matcher_scoring import (
    DEFAULT_DIMENSION_WEIGHTS,
    DIMENSION_FUNCTIONS,
    KnownVehicle,  # re-exported from infra.known_vehicles
    Sig,  # noqa: F401  (exported from the module but tests use dict directly)
    _build_type_to_group,
    # Dimension functions (private with leading underscore but intentionally
    # importable for unit testing)
    _dim_bed_cover_match,
    _dim_body_style_flex,
    _dim_color_alt_match,
    _dim_color_alt_mismatch,
    _dim_color_match,
    _dim_color_mismatch,
    _dim_distinctive_keyword,
    _dim_feature_match,
    _dim_make_in_label,
    _dim_make_match,
    _dim_model_aliases_match,
    _dim_model_in_label,
    _dim_model_match,
    _dim_type_group_flex,
    _dim_type_match,
    _normalize_bed_cover,
    _normalize_color,
    score_vehicle,
)
from infra.matcher_spec import DEFAULT_SPEC


# Helpers
def _kv(**kw: Any) -> dict[str, Any]:
    """Build a known_vehicle dict with sensible defaults."""
    base: dict[str, Any] = {"id": "v_test", "make": "ford", "model": "f150",
                            "type": "pickup", "color": "blue"}
    base.update(kw)
    return base


def _sig(**kw: Any) -> dict[str, Any]:
    """Build a signature dict with sensible defaults."""
    base: dict[str, Any] = {"make": "ford", "model": "f150", "color": "blue",
                            "type": "pickup"}
    base.update(kw)
    return base


# ===========================================================================
# Module shape — DIMENSION_FUNCTIONS + DEFAULT_DIMENSION_WEIGHTS contract
# ===========================================================================


def test_dimension_functions_count_matches_weights() -> None:
    """Both tables cover the same set of dimensions."""
    assert set(DIMENSION_FUNCTIONS.keys()) == set(DEFAULT_DIMENSION_WEIGHTS.keys())


def test_dimension_functions_count_23() -> None:
    """23 dimensions per the PRD (Phase 6B.29b)."""
    assert len(DIMENSION_FUNCTIONS) == 23


def test_default_dimension_weights_have_negative_color_mismatch() -> None:
    """6B.75 — color_mismatch and color_alt_mismatch have negative weights."""
    assert DEFAULT_DIMENSION_WEIGHTS["color_mismatch"] < 0
    assert DEFAULT_DIMENSION_WEIGHTS["color_alt_mismatch"] < 0


def test_default_dimension_weights_have_positive_color_match() -> None:
    assert DEFAULT_DIMENSION_WEIGHTS["color_match"] > 0
    assert DEFAULT_DIMENSION_WEIGHTS["color_alt_match"] > 0


def test_default_dimension_weights_make_model_strongest() -> None:
    """model_match is the heaviest weight (3.0)."""
    weights = DEFAULT_DIMENSION_WEIGHTS
    assert weights["model_match"] >= weights["make_match"]
    assert weights["model_match"] == 3.0
    assert weights["make_match"] == 2.0


# ===========================================================================
# _normalize_color
# ===========================================================================


def test_normalize_color_navy_to_blue() -> None:
    assert _normalize_color("navy", DEFAULT_SPEC) == "blue"


def test_normalize_color_dark_blue_to_blue() -> None:
    assert _normalize_color("dark_blue", DEFAULT_SPEC) == "blue"


def test_normalize_color_silver_to_gray() -> None:
    """Phase 6B.26a decision — silver is gray, not silver is metal."""
    assert _normalize_color("silver", DEFAULT_SPEC) == "gray"


def test_normalize_color_pearl_to_white() -> None:
    assert _normalize_color("pearl", DEFAULT_SPEC) == "white"


def test_normalize_color_returns_none_for_none() -> None:
    assert _normalize_color(None, DEFAULT_SPEC) is None


def test_normalize_color_returns_none_for_empty() -> None:
    assert _normalize_color("", DEFAULT_SPEC) is None


def test_normalize_color_returns_none_for_unknown_color() -> None:
    """A color outside the normalization table returns None (not the raw value)."""
    assert _normalize_color("chartreuse", DEFAULT_SPEC) is None


def test_normalize_color_case_insensitive() -> None:
    assert _normalize_color("NAVY", DEFAULT_SPEC) == "blue"
    assert _normalize_color("Navy", DEFAULT_SPEC) == "blue"


# ===========================================================================
# _build_type_to_group
# ===========================================================================


def test_build_type_to_group_basic() -> None:
    """Inverts {group: [types]} into {type: group}."""
    spec = {
        "type_groups": {
            "vehicle": ["pickup", "truck"],
            "sedan": ["sedan"],
        },
    }
    t2g = _build_type_to_group(spec)
    assert t2g["pickup"] == "vehicle"
    assert t2g["truck"] == "vehicle"
    assert t2g["sedan"] == "sedan"


def test_build_type_to_group_lowercases_keys() -> None:
    spec = {"type_groups": {"vehicle": ["Pickup", "TRUCK"]}}
    t2g = _build_type_to_group(spec)
    assert t2g["pickup"] == "vehicle"
    assert t2g["truck"] == "vehicle"


def test_build_type_to_group_handles_missing() -> None:
    """Missing 'type_groups' returns empty dict (no crash)."""
    assert _build_type_to_group({}) == {}
    assert _build_type_to_group({"type_groups": None}) == {}


# ===========================================================================
# _dim_color_match
# ===========================================================================


def test_dim_color_match_fires_on_equal_normalized() -> None:
    """sig:navy + kv:blue both normalize to 'blue' → match."""
    assert _dim_color_match(
        _sig(color="navy"),
        _kv(color="dark_blue"),
        DEFAULT_SPEC,
    ) is True


def test_dim_color_match_skips_when_colors_differ() -> None:
    """sig:navy + kv:red → 'blue' vs 'red' → no match."""
    assert _dim_color_match(
        _sig(color="navy"),
        _kv(color="red"),
        DEFAULT_SPEC,
    ) is False


def test_dim_color_match_skips_missing_colors() -> None:
    """Either color missing → no match (don't penalize missing data)."""
    assert _dim_color_match(_sig(color=None), _kv(color="blue"), DEFAULT_SPEC) is False
    assert _dim_color_match(_sig(color="navy"), _kv(color=None), DEFAULT_SPEC) is False


# ===========================================================================
# _dim_color_alt_match
# ===========================================================================


def test_dim_color_alt_match_fires_on_alt_hit() -> None:
    """sig:navy + kv.color:red + kv.colors_alt:[blue] → match (alt hit)."""
    sig: Sig = _sig(color="navy")
    kv: KnownVehicle = _kv(color="red", colors_alt=["blue"])
    assert _dim_color_alt_match(sig, kv, DEFAULT_SPEC) is True


def test_dim_color_alt_match_skips_when_no_alts() -> None:
    sig = _sig(color="navy")
    kv = _kv(color="red")
    assert _dim_color_alt_match(sig, kv, DEFAULT_SPEC) is False


# ===========================================================================
# _dim_color_mismatch (6B.75 negative penalty)
# ===========================================================================


def test_dim_color_mismatch_fires_on_different_colors() -> None:
    """sig:blue + kv:red → mismatch."""
    assert _dim_color_mismatch(
        _sig(color="blue"),
        _kv(color="red"),
        DEFAULT_SPEC,
    ) is True


def test_dim_color_mismatch_skips_when_alt_hits() -> None:
    """sig:navy + kv.color:red + kv.colors_alt:[blue] → alt covers the mismatch."""
    sig = _sig(color="navy")
    kv = _kv(color="red", colors_alt=["blue"])
    assert _dim_color_mismatch(sig, kv, DEFAULT_SPEC) is False


def test_dim_color_mismatch_skips_when_colors_equal() -> None:
    """sig:blue + kv.blue → not a mismatch (color_match fires)."""
    assert _dim_color_mismatch(
        _sig(color="navy"),
        _kv(color="dark_blue"),
        DEFAULT_SPEC,
    ) is False


# ===========================================================================
# _dim_color_alt_mismatch (6B.75)
# ===========================================================================


def test_dim_color_alt_mismatch_fires_when_no_alt_hits() -> None:
    """sig:blue, kv.color:red, kv.colors_alt:[yellow] (no blue) → mismatch."""
    sig = _sig(color="blue")
    kv = _kv(color="red", colors_alt=["yellow"])
    assert _dim_color_alt_mismatch(sig, kv, DEFAULT_SPEC) is True


def test_dim_color_alt_mismatch_skips_when_alt_hits() -> None:
    """sig:blue, kv.color:red, kv.colors_alt:[blue] → not a mismatch."""
    sig = _sig(color="blue")
    kv = _kv(color="red", colors_alt=["blue"])
    assert _dim_color_alt_mismatch(sig, kv, DEFAULT_SPEC) is False


def test_dim_color_alt_mismatch_skips_when_no_alts() -> None:
    """No colors_alt → don't penalize (just like color_match would)."""
    sig = _sig(color="blue")
    kv = _kv(color="red")
    assert _dim_color_alt_mismatch(sig, kv, DEFAULT_SPEC) is False


# ===========================================================================
# _dim_make_match / _dim_make_in_label
# ===========================================================================


def test_dim_make_match_fires_on_exact() -> None:
    assert _dim_make_match(_sig(make="ford"), _kv(make="Ford"), DEFAULT_SPEC) is True


def test_dim_make_match_skips_case_mismatch_after_lower() -> None:
    """Case-insensitive but still requires exact (after lowering)."""
    assert _dim_make_match(_sig(make="Ford"), _kv(make="ford"), DEFAULT_SPEC) is True


def test_dim_make_in_label_fires_on_substring() -> None:
    sig = _sig(make="ford")
    kv = _kv(label="Ford F-150")
    assert _dim_make_in_label(sig, kv, DEFAULT_SPEC) is True


def test_dim_make_in_label_skips_when_label_empty() -> None:
    sig = _sig(make="ford")
    kv = _kv(label=None)
    assert _dim_make_in_label(sig, kv, DEFAULT_SPEC) is False


# ===========================================================================
# _dim_model_match / _dim_model_aliases_match / _dim_model_in_label
# ===========================================================================


def test_dim_model_match_fires_on_bidirectional_substring() -> None:
    """sig:F150 + kv:F150Raptor → both directions contain each other."""
    sig = _sig(model="F150")
    kv = _kv(model="F150Raptor")
    assert _dim_model_match(sig, kv, DEFAULT_SPEC) is True


def test_dim_model_match_strips_dashes_and_spaces() -> None:
    """sig:F-150 + kv:F 150 → normalize to 'f150' both sides."""
    sig = _sig(model="F-150")
    kv = _kv(model="F 150")
    assert _dim_model_match(sig, kv, DEFAULT_SPEC) is True


def test_dim_model_aliases_match_fires_on_substring() -> None:
    """sig:'F150' + kv.model_aliases:['F150Raptor'] → substring match."""
    sig = _sig(model="F150")
    kv = _kv(model="anything", model_aliases=["F150Raptor"])
    assert _dim_model_aliases_match(sig, kv, DEFAULT_SPEC) is True


def test_dim_model_in_label_fires_after_normalization() -> None:
    """sig:'f-150' + kv.label:'Cousin Freds old F 150 truck' → match."""
    sig = _sig(model="f-150")
    kv = _kv(label="Cousin Freds old F 150 truck")
    assert _dim_model_in_label(sig, kv, DEFAULT_SPEC) is True


def test_dim_model_in_label_skips_when_label_empty() -> None:
    sig = _sig(model="F150")
    kv = _kv(label=None)
    assert _dim_model_in_label(sig, kv, DEFAULT_SPEC) is False


# ===========================================================================
# _dim_feature_match — flat + nested sig.vehicle_features
# ===========================================================================


def test_dim_feature_match_flat_sig() -> None:
    """Post-6B.46: sig['wheel_style'] = '5-spoke' (no nested vehicle_features)."""
    sig = {"wheel_style": "5-spoke"}
    kv = {"vehicle_features": {"wheel_style": "5-spoke"}}
    assert _dim_feature_match(sig, kv, "wheel_style") is True


def test_dim_feature_match_nested_sig_fallback() -> None:
    """6B.46 backward compat: sig.vehicle_features['wheel_style'] = ... still works."""
    sig = {"vehicle_features": {"wheel_style": "5-spoke"}}
    kv = {"vehicle_features": {"wheel_style": "5-spoke"}}
    assert _dim_feature_match(sig, kv, "wheel_style") is True


def test_dim_feature_match_skips_on_missing() -> None:
    sig: Sig = {"vehicle_features": {}}
    kv: KnownVehicle = {"vehicle_features": {"wheel_style": "5-spoke"}}
    assert _dim_feature_match(sig, kv, "wheel_style") is False


def test_dim_feature_match_case_insensitive() -> None:
    sig = {"wheel_style": "5-Spoke"}
    kv = {"vehicle_features": {"wheel_style": "5-spoke"}}
    assert _dim_feature_match(sig, kv, "wheel_style") is True


# Phase 6B.84 (2026-08-16) — absence-evidence fallacy fix.
# Sharing a default value is not a match. cab_marker_lights=False
# in both sig and kv must NOT count as a +1.0 cab_marker_match.
# Same for bed_cover=none/none, window_tint=unknown/unknown, etc.
def test_dim_feature_match_both_false_does_not_match() -> None:
    """Phase 6B.84: cab_marker_lights=False == False is absence-evidence,
    not a positive match. Sharing a default value is not signal."""
    sig = {"cab_marker_lights": False}
    kv = {"vehicle_features": {"cab_marker_lights": False}}
    assert _dim_feature_match(sig, kv, "cab_marker_lights") is False


def test_dim_feature_match_both_none_string_does_not_match() -> None:
    """bed_cover='none' == 'none' is the d99a38e6 absence-evidence pattern."""
    sig = {"bed_cover": "none"}
    kv = {"vehicle_features": {"bed_cover": "none"}}
    assert _dim_feature_match(sig, kv, "bed_cover") is False


def test_dim_feature_match_both_missing_does_not_match() -> None:
    """Both sides absent entirely: no positive signal, no match."""
    sig: Sig = {}
    kv: KnownVehicle = {"vehicle_features": {}}
    assert _dim_feature_match(sig, kv, "wheel_style") is False


def test_dim_feature_match_positive_match_still_works() -> None:
    """Phase 6B.84: when values are genuinely present and equal, the
    match still fires. Don't break the legitimate cases."""
    sig = {"wheel_style": "5-spoke"}
    kv = {"vehicle_features": {"wheel_style": "5-spoke"}}
    assert _dim_feature_match(sig, kv, "wheel_style") is True


def test_dim_feature_match_one_side_only_present_is_not_a_match() -> None:
    """One side present (positive), other side missing/False. Not a
    match — present evidence doesn't become absent."""
    sig = {"wheel_style": "5-spoke"}
    kv = {"vehicle_features": {"wheel_style": False}}
    assert _dim_feature_match(sig, kv, "wheel_style") is False


def test_dim_feature_match_true_equals_true_matches() -> None:
    """Positive boolean on both sides: a real match (e.g. both have
    cab_marker_lights=True — both are 'real' trucks with the lights)."""
    sig = {"cab_marker_lights": True}
    kv = {"vehicle_features": {"cab_marker_lights": True}}
    assert _dim_feature_match(sig, kv, "cab_marker_lights") is True


def test_default_dimension_weights_color_mismatch_dominant() -> None:
    """Phase 6B.84 (2026-08-16) — maintainer OOB: 'if a blue truck is
    trying to be matched to a white truck then the color mismatch
    should be a big penalty.' The penalty must clearly dominate the
    reward (|color_mismatch| >> color_match)."""
    weights = DEFAULT_DIMENSION_WEIGHTS
    assert weights["color_mismatch"] < 0
    # Penalty >= 4x the credit so a wrong-color match loses more than
    # it gains on a same-color match's reward alone.
    assert abs(weights["color_mismatch"]) >= 4 * weights["color_match"]
    assert weights["color_mismatch"] <= -2.8


# ===========================================================================
# _dim_type_match / _dim_type_group_flex / _dim_body_style_flex
# ===========================================================================


def test_dim_type_match_fires_on_exact() -> None:
    assert _dim_type_match(_sig(type="pickup"), _kv(type="pickup"), DEFAULT_SPEC) is True


def test_dim_type_match_skips_when_differ() -> None:
    assert _dim_type_match(_sig(type="pickup"), _kv(type="sedan"), DEFAULT_SPEC) is False


def test_dim_type_match_falls_back_to_body_style_hint() -> None:
    """sig has body_style_hint but no type → use body_style_hint."""
    sig = {"body_style_hint": "pickup"}
    kv = {"type": "pickup"}
    assert _dim_type_match(sig, kv, DEFAULT_SPEC) is True


def test_dim_type_group_flex_pickup_to_truck() -> None:
    """sig:pickup + kv:truck → both in 'vehicle' group, different → fires."""
    assert _dim_type_group_flex(
        _sig(type="pickup"),
        _kv(type="truck"),
        DEFAULT_SPEC,
    ) is True


def test_dim_type_group_flex_skips_sedan_to_sedan() -> None:
    """Cross-group flex only applies to 'vehicle' supergroup."""
    assert _dim_type_group_flex(
        _sig(type="sedan"),
        _kv(type="sedan"),
        DEFAULT_SPEC,
    ) is False


def test_dim_body_style_flex_requires_opt_in() -> None:
    """kv.body_style_flex: true required for the dim to fire."""
    sig = _sig(type="coupe")
    # Without body_style_flex
    kv = _kv(type="sedan", body_style_flex=False, body_style_aliases=["coupe", "sedan"])
    assert _dim_body_style_flex(sig, kv, DEFAULT_SPEC) is False


def test_dim_body_style_flex_fires_on_alias_match() -> None:
    sig = _sig(type="coupe")
    kv = _kv(
        type="sedan",
        body_style_flex=True,
        body_style_aliases=["coupe", "sedan"],
    )
    assert _dim_body_style_flex(sig, kv, DEFAULT_SPEC) is True


# ===========================================================================
# _dim_bed_cover_match (topper == camper_shell alias, 6B.48)
# ===========================================================================


def test_dim_bed_cover_match_topper_alias() -> None:
    """sig:topper + kv:camper_shell → match (both normalize to 'camper_shell')."""
    sig = _sig(bed_cover="topper")
    kv = _kv(vehicle_features={"bed_cover": "camper_shell"})
    assert _dim_bed_cover_match(sig, kv, DEFAULT_SPEC) is True


def test_dim_bed_cover_match_no_match() -> None:
    """sig:topper + kv:none → no match."""
    sig = _sig(bed_cover="topper")
    kv = _kv(vehicle_features={})
    assert _dim_bed_cover_match(sig, kv, DEFAULT_SPEC) is False


def test_normalize_bed_cover_aliases() -> None:
    assert _normalize_bed_cover("topper") == "camper_shell"
    assert _normalize_bed_cover("camper_shell") == "camper_shell"
    # Phase 6B.84 absence-evidence fix: 'none' (and 'null', 'false')
    # are absence signals — _normalize_bed_cover returns None for
    # them so the matcher doesn't score absence-on-absence as a match.
    assert _normalize_bed_cover("none") is None
    assert _normalize_bed_cover("null") is None
    assert _normalize_bed_cover("") is None
    assert _normalize_bed_cover(False) is None
    assert _normalize_bed_cover(None) is None


# ===========================================================================
# _dim_distinctive_keyword
# ===========================================================================


def test_dim_distinctive_keyword_4char_word() -> None:
    """A 4+ char keyword in distinctive_features must appear in sig.evidence."""
    sig = _sig(notes="Has a roof rack with bike mounted")
    kv = _kv(distinctive_features=["roof rack", "bike holder"])
    assert _dim_distinctive_keyword(sig, kv, DEFAULT_SPEC) is True


def test_dim_distinctive_keyword_short_word_ignored() -> None:
    """Words shorter than 4 chars (e.g. 'the') are skipped."""
    sig = _sig(notes="the car has the rack")
    kv = _kv(distinctive_features=["the", "and"])  # all short words
    assert _dim_distinctive_keyword(sig, kv, DEFAULT_SPEC) is False


def test_dim_distinctive_keyword_no_match() -> None:
    sig = _sig(notes="plain notes")
    kv = _kv(distinctive_features=["moonroof", "leather"])
    assert _dim_distinctive_keyword(sig, kv, DEFAULT_SPEC) is False


# ===========================================================================
# score_vehicle — orchestrator
# ===========================================================================


def test_score_vehicle_aggregates_matching_dimensions() -> None:
    """Color + type + make + model all match → 0.7 + 0.8 + 2.0 + 3.0 = 6.5."""
    sig = _sig(make="ford", model="f150", color="navy", type="pickup")
    kv = _kv(make="ford", model="f150", color="dark_blue", type="pickup")
    total, breakdown = score_vehicle(sig, kv, DEFAULT_SPEC)
    assert total == pytest.approx(6.5)
    assert breakdown == {
        "color_match": 0.7,
        "type_match": 0.8,
        "make_match": 2.0,
        "model_match": 3.0,
        # (type_group_flex is False, no body_style_flex False)
    }


def test_score_vehicle_no_match_returns_zero() -> None:
    """When no dimensions fire, total is the color_mismatch penalty.

    Phase 6B.84 (2026-08-16) — color_mismatch bumped -2.0 → -4.0
    (maintainer OOB: 'big penalty'). total is now -4.0 instead of -2.0.
    """
    sig = _sig(make="ford", model="f150", color="blue", type="pickup")
    kv = _kv(make="toyota", model="camry", color="red", type="sedan")
    total, breakdown = score_vehicle(sig, kv, DEFAULT_SPEC)
    # color_mismatch fires (-4.0).
    # color_alt_mismatch does NOT fire (no alts).
    # Other dimensions (make_match, model_match, type_match) all False.
    assert "make_match" not in breakdown
    assert "model_match" not in breakdown
    assert breakdown["color_mismatch"] == -4.0
    assert total == -4.0


def test_score_vehicle_no_match_when_everything_identical_zero() -> None:
    """Use a sig with missing fields so no dimension fires."""
    sig: dict[str, Any] = {}  # empty sig
    kv = _kv()
    total, breakdown = score_vehicle(sig, kv, DEFAULT_SPEC)
    # No color, no make, no model, no type → no positive dimensions fire.
    # color_mismatch requires BOTH colors normalized and differing — fails
    # because sig has no color → does not fire.
    assert total == 0.0
    assert breakdown == {}


def test_score_vehicle_skips_weight_zero_dimensions() -> None:
    """A dimension with weight=0 in the spec is not evaluated."""
    spec = dict(DEFAULT_SPEC)
    spec["dimension_weights"] = dict(DEFAULT_DIMENSION_WEIGHTS)
    spec["dimension_weights"]["color_match"] = 0.0  # disable
    sig = _sig(color="navy")
    kv = _kv(color="dark_blue")
    _, breakdown = score_vehicle(sig, kv, spec)
    assert "color_match" not in breakdown


def test_score_vehicle_applies_negative_weight() -> None:
    """color_mismatch has weight=-4.0; fires and subtracts from total.

    With sig.color=blue vs kv.color=red (rest identical):
        - color_mismatch: -4.0 (fires because sig and kv differ)
        - color_match: False (different)
        - color_alt_match: False (no alts)
        - type_match: +0.8 (both pickup)
        - make_match: +2.0 (both ford)
        - model_match: +3.0 (both f150)
        Total: 0.8 + 2.0 + 3.0 - 4.0 = 1.8

    Phase 6B.84 (2026-08-16) — bumped from -2.0 to -4.0 per maintainer OOB
    'big penalty' requirement.
    """
    sig = _sig(color="blue")
    kv = _kv(color="red")
    total, breakdown = score_vehicle(sig, kv, DEFAULT_SPEC)
    assert "color_mismatch" in breakdown
    assert breakdown["color_mismatch"] == -4.0
    assert total == pytest.approx(1.8)


def test_score_vehicle_exception_in_dimension_is_swallowed() -> None:
    """A dimension that raises is logged at debug and treated as a non-match."""
    # The matcher continues even if a per-dimension function blows up.
    # We inject a broken dim_fn via a custom DIMENSION_FUNCTIONS patch.
    import infra.matcher_scoring as scoring_mod

    original = dict(scoring_mod.DIMENSION_FUNCTIONS)
    # Replace color_match with a function that raises on every call.
    scoring_mod.DIMENSION_FUNCTIONS["color_match"] = lambda s, k, spec: (_ for _ in ()).throw(
        KeyError("intentional test failure"),
    )
    try:
        sig = _sig(color="navy")
        kv = _kv(color="dark_blue")
        total, breakdown = score_vehicle(sig, kv, DEFAULT_SPEC)
        # color_match still fails (treated as no-match), other dims work
        assert "color_match" not in breakdown
        assert total > 0  # other dimensions still fire
    finally:
        scoring_mod.DIMENSION_FUNCTIONS.clear()
        scoring_mod.DIMENSION_FUNCTIONS.update(original)


def test_score_vehicle_does_not_mutate_inputs() -> None:
    """score_vehicle is pure — kv and sig dicts are unchanged after scoring."""
    sig = _sig(color="navy", make="ford", model="f150")
    kv = _kv(color="dark_blue", make="ford", model="f150", type="pickup")
    sig_before = copy.deepcopy(sig)
    kv_before = copy.deepcopy(kv)
    score_vehicle(sig, kv, DEFAULT_SPEC)
    assert sig == sig_before
    assert kv == kv_before


def test_score_vehicle_breakdown_keys_always_match_fired_dims() -> None:
    """Every key in breakdown has the same value as the dim's weight."""
    sig = _sig()
    kv = _kv()
    _, breakdown = score_vehicle(sig, kv, DEFAULT_SPEC)
    for name, value in breakdown.items():
        # Negative weights produce a contribution equal to the weight when fired.
        assert value == DEFAULT_DIMENSION_WEIGHTS[name]
