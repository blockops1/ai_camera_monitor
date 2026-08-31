"""
test_format_detector_metadata.py — Tests for
listener.listener.format_detector_metadata_lines.

This helper renders the CAM1 lead motion Telegram's "detector metadata"
section (Phase.81 / PLAN.md §11.14.3.A). It surfaces the motion
detector's structured output as labeled lines so the operator can tell at a
glance whether the alert is a real tracked vehicle (high total_motion_px,
many frames seen, large position_change) vs. noise (small, single-frame
flicker). The helper takes a `MotionResult` and returns a list of lines
that the alert body builder concatenates.

Tests use synthetic `MotionResult` instances constructed directly via the
dataclass. No real RTSP, no real frame_capture.
"""
from __future__ import annotations

from infra.motion_detector import MotionResult, MovingObject
from telegram_formatter.vehicle_alert import format_detector_metadata_lines


def test_format_returns_lines_for_each_non_zero_field():
    """Core behavior: given a MotionResult with all fields populated,
    the helper returns one labeled line per non-zero field.

    This is the smallest end-to-end behavior slice — what the alert
    body looks like when the detector saw a real vehicle. Other tests
    (zero-field omission, missing primary object, None input) build on
    this.
    """
    # Arrange: a fully populated MotionResult + primary MovingObject.
    primary = MovingObject(
        avg_area=1284,
        frames_seen=4,
        position_change_max=287,
    )
    result = MotionResult(
        primary_moving_object=primary,
        total_motion_pixels=33704,
        reference_method="pairwise",
        elapsed_ms=23,
    )

    # Act
    lines = format_detector_metadata_lines(result)

    # Assert: one line per non-zero field, in the documented format.
    assert isinstance(lines, list)
    assert "   detector total_motion_px: 33704" in lines
    assert "   detector reference_method: pairwise" in lines
    assert "   detector object avg_area: 1284" in lines
    assert "   detector object frames_seen: 4/6" in lines
    assert "   detector object position_change_max: 287 px" in lines
    assert "   detector elapsed_ms: 23" in lines