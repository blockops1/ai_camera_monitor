"""
test_vision_schema_lift.py — Regression tests for the crop-prompt →
alert-prompt schema adapter.

Phase.94 (2026-08-18) — fixes the alert-LLM "empty exterior scene"
L0 failure mode caused by the schema gap between
vehicle_identifier/prompt_template.py (crop prompt) and
infra/alert_prompt.py (alert prompt). The crop prompt returns
color/make/model/description/confidence; the alert prompt expects
primary_subject/objects_detected/actions/scene_description. Without
the lift, the alert LLM sees an effectively-empty dict and writes L0
even when vision correctly identified a vehicle.

Verified on alert 0eefa8e9-656f-4868-a802-ee682bf928a4 (Tesla drive-by
15:08 EDT, CAM2): crop vision returned Tesla Model Y
blue conf=0.98 on 3 crops, alert LLM produced "empty exterior scene"
L0. Post-6B.94, the lift populates primary_subject="blue tesla model y"
which the alert LLM can render into a meaningful body.

These tests pin the lift contract:
  - Lift populates fields when crop succeeded with identification
  - Lift is a no-op when crop produced no identification
  - Lift is a no-op when alert fields already populated
  - Lift handles the partial-identification edge cases (color only, no
    description, etc.)
  - Lift is idempotent (re-running with already-lifted dict is a no-op)
  - Lift accepts non-dict input without raising
"""
from __future__ import annotations

from typing import Any

from infra.vision_schema_lift import lift_crop_to_alert_schema

# ---------------------------------------------------------------------------
# Success path — full identification
# ---------------------------------------------------------------------------


def test_lift_full_identification_populates_all_alert_fields():
    """The Tesla drive-by case (alert 0eefa8e9): crop returned
    color/make/model/description. Lift should populate all four
    alert-prompt fields so the alert LLM can render a meaningful body.
    """
    crop = {
        "color": "blue",
        "body_style_hint": "suv",
        "make": "Tesla",
        "model": "Model Y",
        "vehicle_features": {"wheel_style": "aero_cover"},
        "description": "A blue Tesla Model Y SUV driving past on the gravel road.",
        "confidence": 0.98,
    }

    out = lift_crop_to_alert_schema(crop)

    # All four alert-prompt fields must be populated.
    assert out["primary_subject"] == "blue tesla model y"
    assert out["objects_detected"] == ["vehicle"]
    assert out["actions"] == []
    assert out["scene_description"] == "A blue Tesla Model Y SUV driving past on the gravel road."

    # Original crop fields must be preserved (the lift is additive).
    assert out["color"] == "blue"
    assert out["make"] == "Tesla"
    assert out["model"] == "Model Y"
    assert out["confidence"] == 0.98
    assert out["vehicle_features"] == {"wheel_style": "aero_cover"}


def test_lift_returns_same_dict():
    """Lift mutates and returns the input dict for caller chaining
    convenience. Verify identity (not just equality) so callers don't
    accidentally handle a copy.
    """
    crop = {"color": "white", "make": "Ford", "model": "F-350"}
    out = lift_crop_to_alert_schema(crop)
    assert out is crop


# ---------------------------------------------------------------------------
# Partial identification — lift with what we have
# ---------------------------------------------------------------------------


def test_lift_color_only():
    """Crop returned color but no make/model. Lift should still produce
    a primary_subject from the color, falling back to "vehicle" if
    nothing else is available.
    """
    crop = {"color": "white"}
    out = lift_crop_to_alert_schema(crop)
    assert out["primary_subject"] == "white"
    assert out["objects_detected"] == ["vehicle"]
    assert out["actions"] == []
    # No description in source — scene_description falls back to a
    # constructed sentence so the alert LLM has prose to work with.
    assert "white" in out["scene_description"]


def test_lift_make_model_only_no_color():
    """Crop returned make/model but no color. Lift should still produce
    a primary_subject from the make/model.
    """
    crop = {"make": "Yanmar", "model": "324"}
    out = lift_crop_to_alert_schema(crop)
    assert out["primary_subject"] == "yanmar 324"
    assert out["objects_detected"] == ["vehicle"]


def test_lift_make_only():
    """Crop returned make only. Common for tractors enrolled with
    model aliases that Qwen doesn't always populate.
    """
    crop = {"make": "Yanmar"}
    out = lift_crop_to_alert_schema(crop)
    assert out["primary_subject"] == "yanmar"


def test_lift_description_falls_back_to_constructed_sentence():
    """When crop has no description, scene_description should be a
    constructed fallback so the alert LLM has prose. This matters for
    partial successes where Qwen identified the vehicle but didn't
    write a description.
    """
    crop = {"color": "red", "make": "Yanmar"}
    out = lift_crop_to_alert_schema(crop)
    # Constructed sentence must reference the primary_subject.
    assert "red yanmar" in out["scene_description"]
    # Must end with a period (prose-ready).
    assert out["scene_description"].endswith(".")


def test_lift_preserves_existing_description():
    """When crop has a description, use it verbatim — don't replace with
    constructed sentence.
    """
    crop = {"color": "blue", "make": "Tesla", "model": "Model Y",
            "description": "A blue Tesla Model Y parked at the gravel curve."}
    out = lift_crop_to_alert_schema(crop)
    assert out["scene_description"] == "A blue Tesla Model Y parked at the gravel curve."


# ---------------------------------------------------------------------------
# No-op paths — lift should not modify these inputs
# ---------------------------------------------------------------------------


def test_lift_no_identification_is_no_op():
    """Crop produced no identification (empty dict). Lift should return
    unchanged so the alert LLM falls through to its "empty exterior
    scene" L0 fallback, which is the correct behavior when vision
    really saw nothing.
    """
    empty: dict[str, Any] = {}
    out = lift_crop_to_alert_schema(empty)
    assert out == {}
    # Specifically: do NOT populate primary_subject with "vehicle".
    # The alert LLM needs to know vision actually saw nothing.
    assert "primary_subject" not in out


def test_lift_empty_string_color_does_not_count_as_identification():
    """Crop returned color="" (empty string, not None). Lift should
    not treat this as identification. The existing code path uses
    `if part` so empty strings are filtered out.
    """
    crop = {"color": "", "make": "", "model": "Model Y"}
    out = lift_crop_to_alert_schema(crop)
    # Only model is truthy, so primary_subject is "model y".
    assert out["primary_subject"] == "model y"


def test_lift_no_op_when_alert_fields_already_populated():
    """If a caller already populated primary_subject (e.g. legacy
    schema path, manual test setup), lift must not overwrite. This
    protects the case where the legacy general-prompt returned full
    alert-prompt fields (no lift needed) and the crop-prompt also
    returned (caller wants to use the existing fields).
    """
    existing = {
        "primary_subject": "a person walking their dog",
        "objects_detected": ["person", "dog"],
        "actions": ["walking"],
        "scene_description": "A person and a dog on the gravel road.",
        # Crop-prompt fields also present (mixed schema):
        "color": "white",
        "make": "Ford",
    }
    out = lift_crop_to_alert_schema(existing)
    # Pre-existing alert fields must be preserved.
    assert out["primary_subject"] == "a person walking their dog"
    assert out["objects_detected"] == ["person", "dog"]
    assert out["actions"] == ["walking"]
    assert out["scene_description"] == "A person and a dog on the gravel road."


def test_lift_non_dict_input_is_no_op():
    """Defensive: lift must not raise on non-dict input. The listener
    unwraps VisionResult to dict but legacy callers may pass None or
    other types. Returning the input unchanged is the safe behavior.
    """
    assert lift_crop_to_alert_schema(None) is None
    assert lift_crop_to_alert_schema("not a dict") == "not a dict"
    assert lift_crop_to_alert_schema(42) == 42


# ---------------------------------------------------------------------------
# Idempotency — running the lift twice produces the same result
# ---------------------------------------------------------------------------


def test_lift_is_idempotent():
    """Running the lift twice on the same dict must produce the same
    result. Verifies the setdefault semantics don't double-populate
    or recurse.
    """
    crop = {"color": "blue", "make": "Tesla", "model": "Model Y", "description": "blue Tesla"}
    once = lift_crop_to_alert_schema(crop)
    twice = lift_crop_to_alert_schema(once)
    assert once == twice
    # Specifically: primary_subject was not double-set or modified.
    assert twice["primary_subject"] == "blue tesla model y"
    # objects_detected is a list — re-running setdefault should not
    # append another "vehicle" entry.
    assert twice["objects_detected"] == ["vehicle"]


# ---------------------------------------------------------------------------
# Field preservation — the lift does not strip crop-prompt fields
# ---------------------------------------------------------------------------


def test_lift_preserves_vehicle_features():
    """vehicle_features is a nested dict that the matcher reads.
    Lift must not touch it.
    """
    crop = {
        "color": "black",
        "make": "Ford",
        "model": "F-150",
        "vehicle_features": {
            "wheel_style": "aftermarket_alloy",
            "wheel_arch": "raptor_style_flare",
            "bed_cover": "camper_shell",
        },
    }
    out = lift_crop_to_alert_schema(crop)
    assert out["vehicle_features"] == {
        "wheel_style": "aftermarket_alloy",
        "wheel_arch": "raptor_style_flare",
        "bed_cover": "camper_shell",
    }


def test_lift_preserves_body_style_hint():
    """body_style_hint is read by the matcher for type matching.
    Lift must not touch it.
    """
    crop = {"color": "blue", "make": "Tesla", "model": "Model Y", "body_style_hint": "suv"}
    out = lift_crop_to_alert_schema(crop)
    assert out["body_style_hint"] == "suv"


def test_lift_preserves_confidence():
    """confidence is the alert LLM's confidence indicator. Lift must
    not touch it.
    """
    crop = {"color": "blue", "make": "Tesla", "model": "Model Y", "confidence": 0.98}
    out = lift_crop_to_alert_schema(crop)
    assert out["confidence"] == 0.98
