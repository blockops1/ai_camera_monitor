"""Unit tests for build_no_match_telegram_body."""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


from telegram_formatter.no_match_telegram import (
    NoMatchTelegramInput,
    _format_reason,
    build_no_match_telegram_body,
)
from vehicle_matcher.matcher import NoMatch


def _nm_below_threshold():
    return NoMatch(
        reason="below_threshold",
        top_candidates=[
            ("v_carson_white", 0.4),
            ("v_jayco_camper", 0.3),
            ("v_brown_f150", 0.2),
        ],
    )


def _nm_below_gap():
    return NoMatch(
        reason="below_gap",
        top_candidates=[
            ("v_carson_white", 4.5),
            ("v_carson_clone", 4.4),
        ],
    )


def _nm_no_known_vehicles():
    return NoMatch(reason="no_known_vehicles", top_candidates=[])


def _top_n():
    return [
        ("v_carson_white", 0.4, {
            "color": 1.0, "type": 0.0, "make": 0.0, "model": 0.0,
        }),
        ("v_jayco_camper", 0.3, {
            "color": 1.0, "type": 0.0, "make": 0.0, "model": 0.0,
        }),
        ("v_brown_f150", 0.2, {
            "color": 0.0, "type": 0.0, "make": 0.0, "model": 0.0,
        }),
    ]


# --- _format_reason --------------------------------------------------------


def test_format_reason_below_threshold():
    assert "below confidence threshold" in _format_reason(_nm_below_threshold())


def test_format_reason_below_gap():
    assert "gap too small" in _format_reason(_nm_below_gap())


def test_format_reason_no_known_vehicles():
    assert "No known vehicles" in _format_reason(_nm_no_known_vehicles())


def test_format_reason_unknown_falls_back():
    nm = NoMatch(reason="mystery")
    assert "mystery" in _format_reason(nm)


# --- build_no_match_telegram_body -----------------------------------------


def test_includes_header():
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_below_threshold(),
        top_n_breakdowns=_top_n(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert "OFS" in body
    assert "t" in body
    assert "❌ No match" in body


def test_includes_edt_timestamp_string():
    """Phase.99 (PLAN.md §11.26): no-match Telegram body must carry the
    EDT string the listener converted at the webhook boundary.
    """
    edt = "2026-08-19 12:14:32 EDT"
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso=edt,
        no_match=_nm_below_threshold(),
        top_n_breakdowns=_top_n(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert edt in body
    assert "+0000" not in body


def test_includes_reason_text():
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_below_threshold(),
        top_n_breakdowns=_top_n(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert "Reason: Top score below confidence threshold" in body


def test_includes_top_n_candidates():
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_below_threshold(),
        top_n_breakdowns=_top_n(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert "Top candidates:" in body
    assert "v_carson_white" in body
    assert "v_jayco_camper" in body
    assert "v_brown_f150" in body
    assert "#1" in body
    assert "#2" in body
    assert "#3" in body


def test_includes_breakdowns_for_each_candidate():
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_below_threshold(),
        top_n_breakdowns=_top_n(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert "color: 1.00" in body
    assert "type: 0.00" in body


def test_includes_thresholds():
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_below_threshold(),
        top_n_breakdowns=_top_n(),
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert "Thresholds: confidence≥0.60" in body
    assert "gap≥0.15" in body


def test_handles_empty_top_n():
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_no_known_vehicles(),
        top_n_breakdowns=[],
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert "no candidates" in body


def test_handles_below_gap_reason():
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_below_gap(),
        top_n_breakdowns=[
            ("v_carson_white", 4.5, {"color": 1.0}),
            ("v_carson_clone", 4.4, {"color": 1.0}),
        ],
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert "gap too small" in body


def test_handles_no_known_vehicles_reason():
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_no_known_vehicles(),
        top_n_breakdowns=[],
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    assert "No known vehicles" in body


def test_alert_id_removed_phase_6b114():
    """Phase.114: alert_id removed from header (diagnostic noise).

    Timestamp moves to footer for user-facing visibility.
    """
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_below_threshold(),
        top_n_breakdowns=_top_n(),
        match_threshold=0.6,
        gap_threshold=0.15,
        alert_id="abc123",
    )
    body = build_no_match_telegram_body(inp)
    assert "[abc123]" not in body  # Phase.114
    assert body.endswith("t")  # footer is captured_at_iso


def test_top_n_breakingdowns_sorted_by_dim_score_descending():
    breakdowns = {
        "color": 0.0,
        "type": 1.0,
        "make": 0.5,
        "model": 0.0,
    }
    inp = NoMatchTelegramInput(
        camera_name="OFS",
        captured_at_iso="t",
        no_match=_nm_below_threshold(),
        top_n_breakdowns=[("v_x", 1.0, breakdowns)],
        match_threshold=0.6,
        gap_threshold=0.15,
    )
    body = build_no_match_telegram_body(inp)
    # type (1.0) should appear before make (0.5) before color/model (0.0).
    body_lines = body.split("\n")
    type_idx = next(i for i, line in enumerate(body_lines) if "type: 1.00" in line)
    make_idx = next(i for i, line in enumerate(body_lines) if "make: 0.50" in line)
    color_idx = next(i for i, line in enumerate(body_lines) if "color: 0.00" in line)
    assert type_idx < make_idx < color_idx
