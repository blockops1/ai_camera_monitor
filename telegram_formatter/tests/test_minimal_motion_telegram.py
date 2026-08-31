"""Unit tests for build_minimal_motion_telegram_body (Phase.89).

Pins the exact body shape so the CAM1 lead motion alert can't regress
silently. Note 2026-08-18: "just one picture, the fourth frame, and
Qwen's vehicle identification, color, and confidence. All the other
text I don't want."
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


from telegram_formatter.motion_telegram import (
    MotionTelegramInput,
    build_minimal_motion_telegram_body,
)


def _qwen_response(*, vehicles=None, confidence: float | None = 0.95):
    """Build a Qwen vision_result with the shape the listener passes.

    `confidence` may be a number or None (the latter exercises the
    `(no confidence)` branch in the formatter)."""
    return {
        "color": "black",
        "body_style_hint": "pickup",
        "make": "Ford",
        "model": "F-150",
        "vehicle_features": {"bed_cover": "camper_shell"},
        "description": "",
        "confidence": confidence,
        "vehicles": vehicles if vehicles is not None else [
            {
                "color": "black",
                "body_style_hint": "pickup",
                "make": "Ford",
                "model": "F-150",
                "description": "black Ford F-150 pickup with camper shell",
                "confidence": confidence,
            }
        ],
    }


# --- shape pins --------------------------------------------------------------


def test_body_has_exactly_three_lines():
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=["UM1"],
        avg_area=6183,
        vision_result=_qwen_response(),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert body.count("\n") + 1 == 3, (
        f"Minimal body must be exactly 3 lines, got: {body!r}"
    )


def test_body_includes_camera_name():
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "CAM1" in body
    assert body.startswith("🚗 <b>Motion — CAM1</b>")


def test_body_includes_captured_at():
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "2026-08-18T10:11:13-04:00" in body


def test_body_includes_confidence_with_two_decimals():
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(confidence=0.95),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "(confidence: 0.95)" in body


def test_body_uses_qwen_description_when_present():
    """description field is preferred over color/bsh/make/model."""
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "black Ford F-150 pickup with camper shell" in body


def test_body_falls_back_to_structured_fields_when_no_description():
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(vehicles=[
            {
                "color": "white",
                "body_style_hint": "pickup",
                "make": "Chevrolet",
                "model": "Silverado 1500",
                "description": "",
                "confidence": 0.85,
            }
        ], confidence=0.85),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "white pickup" in body
    assert "Chevrolet Silverado 1500" in body


def test_body_handles_missing_vision_result():
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=None,
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "(no vehicle description)" in body
    assert "🚗 <b>Motion — CAM1</b>" in body


def test_body_handles_missing_confidence():
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(vehicles=[{
            "color": "black",
            "body_style_hint": "pickup",
            "make": "Ford",
            "model": "F-150",
            "description": "black Ford F-150 pickup",
        }], confidence=None),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "(no confidence)" in body


def test_body_handles_zero_confidence():
    """Zero confidence is treated as missing (model returned but said 'no')."""
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(confidence=0),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "(no confidence)" in body


def test_body_does_not_leak_matcher_or_pipeline_fields():
    """Note 2026-08-11: motion body must NEVER show matcher output.

    The match alert is a separate Telegram. The motion alert is the
    vision model's output, not the matcher's interpretation.
    """
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(vehicles=[
            {
                "color": "black",
                "body_style_hint": "pickup",
                "make": "Ford",
                "model": "F-150",
                "description": "black Ford F-150 pickup",
                "identified_label": "Brown F150 pickup (camper top)",  # matcher's verdict
                "identified_owner": "name one",  # matcher's verdict
                "identified": True,
                "identification_confidence": 9.0,
            }
        ]),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "Brown F150" not in body, (
        "matcher label leaked into motion body — would re-create the "
        "6B.77 'Jayco Jay Feather' false-positive bug"
    )
    assert "identified_label" not in body
    assert "identified_owner" not in body
    assert "name one" not in body


def test_body_omits_detector_metadata():
    """Detector trajectory, total_motion_px, avg_area, frame_positions
    must NOT appear — they are operator-debug fields, not the alert."""
    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=["absent", "UM1", "UM1", "UM1", "UM1", "UM1"],
        avg_area=6183,
        vision_result=_qwen_response(),
    )
    body = build_minimal_motion_telegram_body(inp)
    assert "trajectory" not in body
    assert "UM1" not in body
    assert "avg_area" not in body
    assert "frame" not in body.lower() or "frame trajectory" not in body


def test_vehicle_idx_out_of_range_renders_empty_fallback():
    inp = MotionTelegramInput(
        camera_name="Fake Front Solar",
        captured_at_iso="2026-08-18T10:11:13-04:00",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(),
    )
    body = build_minimal_motion_telegram_body(inp, vehicle_idx=5)
    assert "(no vehicle description)" in body
    assert "🚗 <b>Motion — Fake Front Solar</b>" in body


# ---------------------------------------------------------------------------
# Phase.167 §13.5 Commit 13: display-name lookup contract.
# ---------------------------------------------------------------------------


def test_minimal_body_header_uses_display_name_for_lookup(monkeypatch):
    """Phase.167 §13.5 (Commit 13): the minimal body header reflects
    display_name_for(input.camera_name), not the raw caller string.
    """
    from telegram_formatter import motion_telegram as m_t

    monkeypatch.setattr(
        m_t, "display_name_for",
        lambda identifier, env_path=None: "Fake Porch" if identifier == "CAM1" else identifier,
    )

    inp = MotionTelegramInput(
        camera_name="CAM1",
        captured_at_iso="t",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(),
    )
    body = build_minimal_motion_telegram_body(inp, vehicle_idx=0)
    # Header line uses the looked-up name.
    header = body.split("\n", 1)[0]
    assert "Motion — Fake Porch" in header
    assert "CAM1" not in header


def test_minimal_body_header_passes_through_when_unknown(monkeypatch):
    """Unknown identifier → body shows the original string."""

    from telegram_formatter import motion_telegram as m_t

    monkeypatch.setattr(m_t, "display_name_for", lambda i, env_path=None: i)

    inp = MotionTelegramInput(
        camera_name="Z9_UNKNOWN",
        captured_at_iso="t",
        trajectory=[],
        avg_area=0,
        vision_result=_qwen_response(),
    )
    body = build_minimal_motion_telegram_body(inp, vehicle_idx=0)
    header = body.split("\n", 1)[0]
    assert "Z9_UNKNOWN" in header