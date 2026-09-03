"""Unit tests for the crop prompt template + schema."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import json

from vehicle_identifier.prompt_template import (
    VEHICLE_CROP_SCHEMA,
    render_crop_prompt,
)


def test_render_fills_camera_name():
    out = render_crop_prompt("Outside Front Solar", "2026-08-11T18:10:38.000+0000")
    assert 'Camera "Outside Front Solar"' in out
    assert "2026-08-11T18:10:38.000+0000" in out


def test_render_handles_event_hint_block():
    out = render_crop_prompt("Cam", "now", event_hint_block="hint here")
    assert "hint here" in out


def test_render_default_event_hint_block_empty():
    out = render_crop_prompt("Cam", "now")
    # No leftover "{event_hint_block}" placeholder.
    assert "{event_hint_block}" not in out


def test_prompt_preserves_vehicle_features_brace():
    """The schema block contains literal { that .format() would mangle.
    render_crop_prompt must use safe substitution."""
    out = render_crop_prompt("Cam", "now")
    assert "vehicle_features {" in out
    assert "wheel_style, wheel_arch" in out


def test_prompt_contains_description_field():
    """Phase.77: free-text description is part of the schema."""
    out = render_crop_prompt("Cam", "now")
    assert "description (1-2 sentence free-text identification" in out


def test_prompt_contains_all_required_fields():
    out = render_crop_prompt("Cam", "now")
    for field in ("color", "body_style_hint", "make", "model",
                  "vehicle_features", "description", "confidence"):
        assert field in out, f"required field {field!r} missing from prompt"


def test_schema_required_fields():
    required = VEHICLE_CROP_SCHEMA["required"]
    assert "color" in required
    assert "body_style_hint" in required
    assert "make" in required
    assert "model" in required
    assert "vehicle_features" in required
    assert "description" in required
    assert "confidence" in required


def test_schema_color_enum():
    color_enum = VEHICLE_CROP_SCHEMA["properties"]["color"]["enum"]
    assert "white" in color_enum
    assert "blue" in color_enum
    assert "black" in color_enum
    assert "gray" in color_enum
    assert "silver" in color_enum


def test_schema_confidence_range():
    conf = VEHICLE_CROP_SCHEMA["properties"]["confidence"]
    assert conf["minimum"] == 0.0
    assert conf["maximum"] == 1.0


def test_schema_vehicle_features_required_keys():
    feats_required = VEHICLE_CROP_SCHEMA["properties"]["vehicle_features"]["required"]
    expected = {
        "wheel_style", "wheel_arch", "wheel_color", "roofline_style",
        "front_grille_style", "headlight_signature", "rear_lights_signature",
        "tailgate_type", "badge_text_readable", "window_tint",
        "cab_marker_lights", "bed_cover",
    }
    assert set(feats_required) == expected


def test_schema_cab_marker_lights_accepts_bool_or_string():
    """Qwen has been emitting 'false' as a string; schema must accept."""
    cab = VEHICLE_CROP_SCHEMA["properties"]["vehicle_features"]["properties"]["cab_marker_lights"]
    assert "boolean" in cab["type"]
    assert "string" in cab["type"]
    assert "null" in cab["type"]


def test_schema_is_json_serializable():
    """If the orchestrator wants to pass the schema to the vision API
    as a 'json_schema' constraint, it must serialize cleanly."""
    json.dumps(VEHICLE_CROP_SCHEMA)
