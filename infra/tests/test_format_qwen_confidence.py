"""
test_format_qwen_confidence.py — Tests for
listener.listener.format_qwen_confidence_line.

This helper renders the Qwen confidence line for the OFS lead motion
Telegram (Phase.81 / PLAN.md §11.14.3.B). It surfaces the vision
model's per-vehicle confidence at the top of the alert body so Note can
tell at a glance whether Qwen had high confidence in its identification
("0.85" — likely real) vs. low confidence ("0.32" — uncertain) vs. no
confidence at all ("(empty)" — vision call failed or returned nothing).

The helper reads `vision_result["confidence"]` directly (a float 0.0-1.0)
and renders it as either `   qwen confidence: 0.62` (2-decimal float) or
`   qwen confidence: (empty)` (when the value is missing or zero).

Per §11.14.3.B this is a SCOPED change to the lead motion alert only —
it does NOT modify `render_qwen_dict_lines` so other call sites (match,
no-match alerts) keep their existing behavior.
"""
from __future__ import annotations

from telegram_formatter.vehicle_alert import format_qwen_confidence_line


def test_format_returns_confidence_line_when_present():
    """Core behavior: when vision_result has a numeric confidence, the
    helper returns a single line with the value formatted to 2 decimals.

    This is the smallest end-to-end behavior slice. Other tests (missing
    key, zero/None, non-numeric) build on this.
    """
    # Act
    line = format_qwen_confidence_line({"confidence": 0.62})

    # Assert: single string, 2-decimal format, matches the documented
    # leading indent.
    assert isinstance(line, str)
    assert line == "   qwen confidence: 0.62"