"""Unit tests for render_qwen_dict_lines.

Note 2026-08-11: "when I tell you that I want all the output
of the identifier vision model output sent to me in the telegram
that's actually what I mean. It's not up to you to interpret my
request when I tell you I want something very specific."

These tests pin: every key Qwen returns must appear in the body.
No curation. No truncation. Forward-compat for fields Qwen adds
later. Defense-in-depth against matcher-output leaks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Make `refactor.telegram_formatter` importable.
_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

from telegram_formatter.render_qwen import (
    MATCHER_OUTPUT_SKIP_KEYS,
    render_qwen_dict_lines,
)


def _body(lines):
    return "\n".join(lines)


def test_renders_every_known_field():
    """Pre-existing structured fields + nested vehicle_features dict."""
    v: dict[str, Any] = {
        "color": "white",
        "body_style_hint": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "vehicle_features": {
            "wheel_style": "black steel",
            "cab_marker_lights": False,
            "bed_cover": "none",
        },
        "confidence": 0.62,
    }
    body = _body(render_qwen_dict_lines(v))
    assert "color: white" in body
    assert "body_style_hint: pickup" in body
    assert "make: GMC" in body
    assert "model: Sierra 1500" in body
    assert "vehicle_features:" in body
    assert "wheel_style: black steel" in body
    assert "cab_marker_lights: false" in body
    assert "bed_cover: none" in body


def test_renders_brand_new_field_qwen_never_returned():
    """Forward-compat: any key Qwen adds in a future version renders."""
    v: dict[str, Any] = {
        "color": "white",
        "cargo_visible": "wood planks in bed",
        "tinted_rear_window": True,
    }
    body = _body(render_qwen_dict_lines(v))
    assert "cargo_visible: wood planks in bed" in body
    assert "tinted_rear_window: true" in body


def test_skips_none_and_empty_and_null():
    """None / "" / 'null' / empty-list / empty-dict → no body line."""
    v: dict[str, Any] = {
        "color": "white",
        "body_style_hint": None,
        "make": "",
        "model": "null",
        "description": "   ",         # whitespace-only also skipped
        "vehicle_features": {},
        "tags": [],
    }
    lines = render_qwen_dict_lines(v)
    body = _body(lines)
    assert "color: white" in body
    assert "body_style_hint:" not in body
    assert "make:" not in body
    assert "model:" not in body
    assert "description:" not in body
    assert "vehicle_features:" not in body
    assert "tags:" not in body


def test_skip_keys_excludes_match_output():
    """Defensive pin: matcher fields NEVER leak into the Motion body."""
    v: dict[str, Any] = {
        "color": "white",
        "identified_label": "Jayco Jay Feather travel trailer",
        "identified_owner": "name four",
        "identification_confidence": 3.20,
        "identification_crops_used": 3,
        "identification_fallback": False,
        "kv_id": "v_jayco_camper",
        "label": "Jayco Jay Feather",
        "owner": "name four",
        "signature": {"color": "white"},
        "breakdown": {"color": 0.70},
        "vision_classification": {"x": 1},
        "best_crop_path": "/tmp/x.jpg",
        "crops_used": 3,
        "fallback_used": None,
        "elapsed_ms": 1234.0,
        "frame_positions": ["T1", "T2"],
        "motion": "moving",
        "identified": True,
    }
    body = _body(render_qwen_dict_lines(v))
    assert "Jayco" not in body
    assert "name four" not in body
    assert "identified_label" not in body
    assert "identification_confidence" not in body
    assert "kv_id" not in body
    assert "label:" not in body        # the key itself
    assert "owner:" not in body        # the key itself
    assert "signature" not in body
    assert "breakdown" not in body
    assert "best_crop_path" not in body
    assert "frame_positions" not in body


def test_confidence_field_renders():
    """Qwen's confidence is a real structured field — must render."""
    v: dict[str, Any] = {
        "color": "blue",
        "make": "Chevrolet",
        "model": "Silverado 1500",
        "vehicle_features": {"wheel_style": "steel"},
        "description": "A blue pickup truck.",
        "confidence": 0.95,
    }
    body = _body(render_qwen_dict_lines(v))
    assert "confidence: 0.95" in body


def test_confidence_not_in_skip_keys():
    """Defensive: confidence must NOT be in the skip list (Phase.78
    fix — earlier draft incorrectly added it)."""
    assert "confidence" not in MATCHER_OUTPUT_SKIP_KEYS
    # The matcher-injected equivalent IS in the skip list:
    assert "identification_confidence" in MATCHER_OUTPUT_SKIP_KEYS


def test_renders_nested_dict_recursively():
    """vehicle_features is a nested dict — each child key on its own line."""
    v: dict[str, Any] = {
        "vehicle_features": {
            "wheel_style": "alloy",
            "wheel_color": "silver",
        },
    }
    body = _body(render_qwen_dict_lines(v))
    assert "vehicle_features:" in body
    assert "wheel_style: alloy" in body
    assert "wheel_color: silver" in body


def test_renders_list_inline_when_short():
    """Short scalar lists render inline: `tags: [a, b, c]`."""
    v = {"tags": ["pickup", "white"]}
    body = _body(render_qwen_dict_lines(v))
    assert "tags: [pickup, white]" in body


def test_renders_list_one_per_line_when_long():
    """Long or non-scalar lists render one item per line."""
    v = {"objects_detected": ["vehicle_a", "vehicle_b", "vehicle_c",
                              "vehicle_d", "vehicle_e"]}
    body = _body(render_qwen_dict_lines(v))
    assert "objects_detected:" in body
    assert "vehicle_a" in body
    assert "vehicle_e" in body


def test_renders_long_string_with_wrap():
    """Long description wraps to multiple lines, indented."""
    v: dict[str, Any] = {
        "description": (
            "A very long description that goes on and on about every "
            "conceivable aspect of this vehicle including the wheel "
            "arches, the roofline, the headlights, and more details "
            "than fit on a single line of a Telegram message body."
        ),
    }
    lines = render_qwen_dict_lines(v)
    body = _body(lines)
    assert "description:" in body
    # At least one wrapped line indented further than the parent:
    indented_count = sum(1 for ln in lines if ln.startswith(" "))
    assert indented_count >= 2, f"expected wrap:\n{body}"


def test_renders_bool_as_true_false():
    """Booleans render as 'true'/'false', not Python's True/False."""
    v = {"cab_marker_lights": True, "bed_cover_present": False}
    body = _body(render_qwen_dict_lines(v))
    assert "cab_marker_lights: true" in body
    assert "bed_cover_present: false" in body


def test_renders_zero_and_falsy_non_empty():
    """0 / 0.0 / False are real values — only None / empty skip."""
    v = {"wheel_count": 0, "weight_kg": 0.0, "is_electric": False}
    body = _body(render_qwen_dict_lines(v))
    assert "wheel_count: 0" in body
    # 0.0 should render as 0.0 or 0 — accept either canonical form.
    assert ("weight_kg: 0.0" in body) or ("weight_kg: 0" in body)
    assert "is_electric: false" in body


def test_renders_top_level_string_non_dict():
    """Top-level non-dict input (a bare string) renders as one line."""
    lines = render_qwen_dict_lines("just a vehicle")
    assert lines == ["just a vehicle"]


def test_renders_dict_with_indent():
    """Caller-provided indent pads every top-level line."""
    v = {"color": "white", "make": "GMC"}
    lines = render_qwen_dict_lines(v, indent=6)
    assert lines[0].startswith("      ")
    assert lines[1].startswith("      ")


def test_skip_keys_is_overrideable():
    """Tests can pass a custom skip_keys set."""
    v = {"color": "white", "internal_id": "abc"}
    lines = render_qwen_dict_lines(
        v, skip_keys=frozenset({"internal_id"})
    )
    body = _body(lines)
    assert "color: white" in body
    assert "internal_id" not in body


def test_empty_dict_returns_empty_list():
    """Empty dict input → empty list, no body lines."""
    assert render_qwen_dict_lines({}) == []


def test_all_empty_dict_returns_empty_list():
    """Dict with only empty values → empty list."""
    v: dict[str, Any] = {"a": None, "b": "", "c": [], "d": {}}
    assert render_qwen_dict_lines(v) == []
