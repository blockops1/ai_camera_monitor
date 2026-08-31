"""Unit tests for build_motion_telegram_body."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


from telegram_formatter.motion_telegram import (
    MotionTelegramInput,
    _format_position_section,
    build_motion_telegram_body,
)


def _qwen_response():
    return {
        "color": "white",
        "body_style_hint": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "vehicle_features": {
            "wheel_style": "black steel",
            "cab_marker_lights": False,
            "bed_cover": "none",
        },
        "description": "A white GMC Sierra 1500 pickup.",
        "confidence": 0.92,
    }


# --- _format_position_section ----------------------------------------------


def test_position_section_no_motion():
    out = _format_position_section(["absent", "absent"], 0)
    assert "no motion detected" in out


def test_position_section_empty_trajectory():
    out = _format_position_section([], 0)
    assert "no motion detected" in out


def test_position_section_single_label():
    out = _format_position_section(["B2", "B2", "B2"], 1200)
    assert "Position: B2" in out
    assert "1,200" in out


def test_position_section_trajectory():
    out = _format_position_section(["B2", "B3", "B4"], 1200)
    assert "B2 → B4" in out


def test_position_section_filters_absent():
    out = _format_position_section(["absent", "B2", "B3"], 1200)
    assert "B2 → B3" in out


# --- build_motion_telegram_body --------------------------------------------


def test_build_body_includes_header():
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-11 14:10:38 EDT",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=_qwen_response(),
    )
    body = build_motion_telegram_body(inp)
    assert "CAM1" in body
    assert "2026-08-11 14:10:38 EDT" in body


def test_build_body_uses_edt_not_utc():
    """Phase.99 (PLAN.md §11.26): the motion Telegram body must show the
    EDT string, not a raw Reolink UTC ISO. The listener converts at the
    webhook boundary; the formatter's job is to pass it through.
    """
    edt = "2026-08-19 12:14:32 EDT"
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso=edt,
        trajectory=["B2"],
        avg_area=1200,
        vision_result=_qwen_response(),
    )
    body = build_motion_telegram_body(inp)
    assert edt in body
    assert "+0000" not in body


def test_build_body_includes_position():
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2", "B3"],
        avg_area=1200,
        vision_result=_qwen_response(),
    )
    body = build_motion_telegram_body(inp)
    assert "Position: B2 → B3" in body
    assert "1,200" in body


def test_build_body_renders_every_qwen_field():
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=_qwen_response(),
    )
    body = build_motion_telegram_body(inp)
    # Every top-level Qwen field must appear.
    assert "color: white" in body
    assert "body_style_hint: pickup" in body
    assert "make: GMC" in body
    assert "model: Sierra 1500" in body
    assert "confidence: 0.92" in body
    # Nested feature fields must appear.
    assert "wheel_style: black steel" in body
    assert "cab_marker_lights: false" in body
    assert "bed_cover: none" in body
    # Description must appear.
    assert "A white GMC Sierra 1500 pickup." in body


def test_build_body_renders_unknown_qwen_field():
    """The Telegram must render any new field Qwen invents."""
    response = _qwen_response()
    response["cargo_visible"] = "hay bales"
    response["license_plate_state"] = "TX"
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=response,
    )
    body = build_motion_telegram_body(inp)
    assert "cargo_visible: hay bales" in body
    assert "license_plate_state: TX" in body


def test_build_body_handles_no_vision_result():
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=None,
    )
    body = build_motion_telegram_body(inp)
    assert "no vision result" in body


def test_build_body_handles_empty_vision_result():
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result={},
    )
    body = build_motion_telegram_body(inp)
    assert "vision result was empty" in body


def test_build_body_alert_id_removed_phase_6b114():
    """Phase.114: alert_id removed from header (diagnostic noise).

    Timestamp moves to footer for user-facing visibility.
    """
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=_qwen_response(),
        alert_id="abc123",
    )
    body = build_motion_telegram_body(inp)
    assert "[abc123]" not in body  # Phase.114
    assert body.endswith("t")  # footer is captured_at_iso


def test_build_body_includes_crops_when_present():
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=_qwen_response(),
        crop_paths=["/tmp/crop_0.jpg", "/tmp/crop_1.jpg"],
    )
    body = build_motion_telegram_body(inp)
    assert "Crops (2):" in body
    assert "/tmp/crop_0.jpg" in body
    assert "/tmp/crop_1.jpg" in body


def test_build_body_no_motion_clean():
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["absent"],
        avg_area=0,
        vision_result=None,
    )
    body = build_motion_telegram_body(inp)
    assert "Motion — Cam" in body
    assert "no motion detected" in body
    assert "no vision result" in body


def test_build_body_has_no_curated_key_list():
    """The body must render based on what's in vision_result, not on
    a hardcoded list of expected keys. This is the 6B.78 contract."""
    # Build a vision_result with only ONE unexpected field.
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result={"unicorn_detection": True},
    )
    body = build_motion_telegram_body(inp)
    assert "unicorn_detection: true" in body


def test_build_body_no_trailing_extra_blank():
    """Body shouldn't have a trailing blank line but should end neatly."""
    inp = MotionTelegramInput(
        camera_name="Cam",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=_qwen_response(),
    )
    body = build_motion_telegram_body(inp)
    # Last line should not be empty.
    assert body.split("\n")[-1].strip() != ""


# ---------------------------------------------------------------------------
# Phase.167 §13.5 Commit 13: display-name lookup contract.
# ---------------------------------------------------------------------------


def test_full_body_header_uses_display_name_for_lookup(monkeypatch):
    """Phase.167 §13.5 (Commit 13): build_motion_telegram_body's
    header reflects display_name_for(input.camera_name), NOT the raw
    caller-supplied string. Mock the helper to verify.
    """
    from telegram_formatter import motion_telegram as m_t

    monkeypatch.setattr(
        m_t, "display_name_for",
        lambda identifier, env_path=None: "Fake Porch" if identifier == "CAM1" else identifier,
    )

    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=_qwen_response(),
    )
    body = build_motion_telegram_body(inp)
    # Header line uses the looked-up name.
    header = body.split("\n", 1)[0]
    assert "Motion — Fake Porch" in header
    assert "CAM1" not in header


def test_full_body_header_passes_through_when_unknown(monkeypatch):
    """Unknown identifier → body shows the original string."""

    from telegram_formatter import motion_telegram as m_t

    monkeypatch.setattr(m_t, "display_name_for", lambda i, env_path=None: i)

    inp = MotionTelegramInput(
        camera_name="Z9_UNKNOWN",
        captured_at_iso="t",
        trajectory=["B2"],
        avg_area=1200,
        vision_result=None,
    )
    body = build_motion_telegram_body(inp)
    header = body.split("\n", 1)[0]
    assert "Z9_UNKNOWN" in header
