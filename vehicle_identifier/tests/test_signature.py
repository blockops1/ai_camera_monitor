"""Unit tests for signature extraction."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

from vehicle_identifier.signature import extract_signature, is_empty_signature


def test_extracts_basic_identification():
    vr = {
        "color": "white",
        "body_style_hint": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
    }
    sig = extract_signature(vr)
    assert sig["color"] == "white"
    assert sig["type"] == "pickup"           # canonicalized from body_style_hint
    assert sig["make"] == "GMC"
    assert sig["model"] == "Sierra 1500"


def test_canonicalizes_body_style_hint_to_type():
    vr = {"color": "red", "body_style_hint": "sedan"}
    sig = extract_signature(vr)
    assert sig["type"] == "sedan"
    assert "body_style_hint" not in sig


def test_flattens_vehicle_features():
    vr = {
        "color": "blue",
        "vehicle_features": {
            "wheel_style": "alloy",
            "wheel_color": "silver",
            "bed_cover": "none",
            "cab_marker_lights": False,
        },
    }
    sig = extract_signature(vr)
    assert sig["wheel_style"] == "alloy"
    assert sig["wheel_color"] == "silver"
    assert sig["bed_cover"] == "none"
    assert sig["cab_marker_lights"] is False


def test_drops_null_string_values():
    vr = {
        "color": "white",
        "make": "null",          # literal string
        "model": "",             # empty
        "body_style_hint": None,
    }
    sig = extract_signature(vr)
    assert sig == {"color": "white"}


def test_drops_null_features():
    vr = {
        "vehicle_features": {
            "wheel_style": "alloy",
            "wheel_color": None,
            "bed_cover": "null",
            "cab_marker_lights": "",
        },
    }
    sig = extract_signature(vr)
    assert sig == {"wheel_style": "alloy"}


def test_preserves_description():
    vr = {
        "color": "white",
        "description": "  A white pickup truck with chrome grille.  ",
    }
    sig = extract_signature(vr)
    assert sig["description"] == "A white pickup truck with chrome grille."


def test_drops_empty_or_whitespace_description():
    vr = {"color": "white", "description": "   "}
    sig = extract_signature(vr)
    assert "description" not in sig


def test_preserves_qwen_confidence_as_float():
    vr = {"color": "white", "confidence": 0.95}
    sig = extract_signature(vr)
    assert sig["confidence"] == 0.95
    assert isinstance(sig["confidence"], float)


def test_handles_int_confidence():
    vr = {"color": "white", "confidence": 1}
    sig = extract_signature(vr)
    assert sig["confidence"] == 1.0


def test_drops_non_numeric_confidence():
    vr = {"color": "white", "confidence": "high"}
    sig = extract_signature(vr)
    assert "confidence" not in sig


def test_forward_compat_passes_through_unknown_keys():
    """Any future field Qwen adds gets preserved in the signature."""
    vr = {
        "color": "white",
        "future_field": "future_value",
        "another_new_field": 42,
    }
    sig = extract_signature(vr)
    assert sig["future_field"] == "future_value"
    assert sig["another_new_field"] == 42


def test_drops_null_in_forward_compat():
    vr = {
        "color": "white",
        "future_field": None,
        "another": "null",
        "kept": "value",
    }
    sig = extract_signature(vr)
    assert "future_field" not in sig
    assert "another" not in sig
    assert sig["kept"] == "value"


def test_empty_vision_result_returns_empty_signature():
    assert extract_signature({}) == {}
    assert extract_signature(None) == {}


def test_non_dict_returns_empty():
    assert extract_signature("not a dict") == {}
    assert extract_signature([1, 2, 3]) == {}


def test_is_empty_signature_true_for_empty_dict():
    assert is_empty_signature({}) is True
    assert is_empty_signature(None) is True
    assert is_empty_signature({"confidence": 0.95}) is True  # no identification fields


def test_is_empty_signature_false_when_color_present():
    sig = {"color": "white"}
    assert is_empty_signature(sig) is False


def test_is_empty_signature_false_when_type_present():
    sig = {"type": "pickup"}
    assert is_empty_signature(sig) is False


def test_is_empty_signature_false_when_make_present():
    sig = {"make": "GMC"}
    assert is_empty_signature(sig) is False


def test_is_empty_signature_false_when_model_present():
    sig = {"model": "Sierra 1500"}
    assert is_empty_signature(sig) is False


def test_full_real_world_qwen_response():
    """The exact shape the production Qwen3-VL model produces on a
    real crop — confirms extraction preserves every usable field."""
    vr = {
        "color": "blue",
        "body_style_hint": "pickup",
        "make": "Chevrolet",
        "model": "Silverado 1500",
        "vehicle_features": {
            "wheel_style": "steel",
            "wheel_arch": "standard",
            "wheel_color": "silver",
            "roofline_style": "standard",
            "front_grille_style": "none",
            "headlight_signature": "none",
            "rear_lights_signature": "none",
            "tailgate_type": "none",
            "badge_text_readable": False,
            "window_tint": "none",
            "cab_marker_lights": False,
            "bed_cover": "none",
        },
        "description": (
            "A blue Chevrolet Silverado 1500 pickup truck with a steel "
            "wheel and no bed cover. Standard roofline, no visible "
            "grille, headlights, or taillights in this view."
        ),
        "confidence": 0.95,
    }
    sig = extract_signature(vr)
    assert sig["color"] == "blue"
    assert sig["type"] == "pickup"
    assert sig["make"] == "Chevrolet"
    assert sig["model"] == "Silverado 1500"
    assert sig["wheel_style"] == "steel"
    assert sig["cab_marker_lights"] is False
    assert sig["badge_text_readable"] is False
    assert sig["description"].startswith("A blue Chevrolet Silverado")
    assert sig["confidence"] == 0.95


# Phase.87.A (added 2026-08-17) — regression tests for the
# listener's per-vehicle wrap shape. The listener calls
# `extract_signature(_wrap)` where `_wrap = {"vehicles": [_mv],
# "primary_vehicle_index": 0}` — this is the per-vehicle score
# shape at the gatekeeper match site (listener.py:3475). Before
# 6B.87.A the function did not unwrap the wrap and every match
# scored 0.00 because the signature dict had no identification
# fields. See PLAN §11.17.A for the full bug story.


def test_unwraps_single_vehicle_wrap():
    """Wrap with one vehicle and primary_vehicle_index=0 must unwrap
    to the vehicle's identification, not return the wrap as-is."""
    _mv = {
        "color": "black",
        "body_style_hint": "pickup",
        "make": "Ford",
        "model": "F-150",
        "vehicle_features": {
            "wheel_style": "standard",
            "wheel_arch": "standard",
            "wheel_color": "black",
            "roofline_style": "standard",
            "front_grille_style": "chrome horizontal bars",
            "headlight_signature": "rectangular",
            "rear_lights_signature": "vertical",
            "tailgate_type": "standard",
            "badge_text_readable": "F-150",
            "window_tint": "none",
            "cab_marker_lights": "false",
            "bed_cover": "none",
        },
        "confidence": 0.98,
    }
    wrap = {"vehicles": [_mv], "primary_vehicle_index": 0}
    sig = extract_signature(wrap)
    assert sig["color"] == "black"
    assert sig["type"] == "pickup"
    assert sig["make"] == "Ford"
    assert sig["model"] == "F-150"
    assert sig["badge_text_readable"] == "F-150"
    assert sig["front_grille_style"] == "chrome horizontal bars"
    assert sig["confidence"] == 0.98
    # The wrap envelope keys must NOT leak into the signature —
    # the matcher would score them as zero and muddy diagnostics.
    assert "vehicles" not in sig
    assert "primary_vehicle_index" not in sig


def test_unwraps_uses_primary_vehicle_index_when_present():
    """When primary_vehicle_index points to a specific vehicle in a
    multi-vehicle scene, extract_signature must use that one — not
    always vehicles[0]."""
    primary = {
        "color": "white",
        "body_style_hint": "suv",
        "make": "Tesla",
        "model": "Model Y",
        "vehicle_features": {},
    }
    secondary = {
        "color": "gray",
        "body_style_hint": "sedan",
        "make": "Honda",
        "model": "Civic",
        "vehicle_features": {},
    }
    wrap = {"vehicles": [primary, secondary], "primary_vehicle_index": 1}
    sig = extract_signature(wrap)
    assert sig["color"] == "gray"
    assert sig["make"] == "Honda"
    assert sig["model"] == "Civic"


def test_unwraps_falls_back_to_first_vehicle_if_index_out_of_range():
    """A primary_vehicle_index that's out of range (negative, beyond
    length, wrong type) must not crash — fall back to vehicles[0]."""
    primary = {"color": "white", "body_style_hint": "sedan",
               "make": "Tesla", "model": "Model 3",
               "vehicle_features": {}}
    wrap = {"vehicles": [primary], "primary_vehicle_index": 99}
    sig = extract_signature(wrap)
    assert sig["make"] == "Tesla"


def test_unwraps_handles_missing_primary_vehicle_index():
    """Wrap without primary_vehicle_index defaults to vehicles[0]."""
    only = {"color": "red", "body_style_hint": "coupe",
            "make": "Ford", "model": "Mustang", "vehicle_features": {}}
    wrap = {"vehicles": [only]}
    sig = extract_signature(wrap)
    assert sig["make"] == "Ford"
    assert sig["model"] == "Mustang"


def test_passthrough_when_no_vehicles_key():
    """Legacy top-level shape (no vehicles[] wrapper) must still work
    unchanged — extract_signature is the public contract used by
    both the listener wrap path AND the legacy Qwen top-level path."""
    vr = {
        "color": "silver",
        "body_style_hint": "suv",
        "make": "Subaru",
        "model": "Outback",
        "vehicle_features": {"wheel_style": "alloy"},
        "confidence": 0.85,
    }
    sig = extract_signature(vr)
    assert sig["color"] == "silver"
    assert sig["make"] == "Subaru"
    assert sig["model"] == "Outback"
    assert sig["type"] == "suv"
    assert sig["wheel_style"] == "alloy"
    assert sig["confidence"] == 0.85


def test_empty_vehicles_list_falls_through():
    """Wrap with empty vehicles[] is malformed input — fall through
    to top-level extraction (which returns empty since the wrap
    itself has no identification fields)."""
    wrap = {"vehicles": [], "primary_vehicle_index": 0}
    sig = extract_signature(wrap)
    # No color/make/model expected — wrap has no identification
    # fields and the empty vehicles list can't be unwrapped.
    assert "color" not in sig
    assert "make" not in sig
