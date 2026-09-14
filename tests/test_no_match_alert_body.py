"""Tests for telegram_formatter.no_match_telegram — build_no_match_alert_body.

Covers reason, top candidates, thresholds, and captured_at rendering.
"""

from __future__ import annotations

from telegram_formatter.no_match_telegram import build_no_match_alert_body


def test_body_contains_reason():
    body = build_no_match_alert_body(
        reason="below confidence threshold",
        top_candidates=[("v_a", 5.0), ("v_b", 2.0)],
        match_threshold=6.0,
        gap_threshold=1.5,
        captured_at="2026-09-14T15:35:31Z",
    )
    assert "below confidence threshold" in body
    assert "Reason:" in body


def test_body_contains_top_candidates():
    body = build_no_match_alert_body(
        reason="no match found",
        top_candidates=[("v_a", 5.0), ("v_b", 2.0)],
        match_threshold=6.0,
        gap_threshold=1.5,
        captured_at="2026-09-14T15:35:31Z",
    )
    assert "Top candidates:" in body
    assert "v_a" in body
    assert "v_b" in body
    assert "#1 v_a" in body
    assert "#2 v_b" in body


def test_body_contains_thresholds():
    body = build_no_match_alert_body(
        reason="no match found",
        top_candidates=[],
        match_threshold=6.0,
        gap_threshold=1.5,
        captured_at="2026-09-14T15:35:31Z",
    )
    assert "Thresholds:" in body
    assert "Match: 6.0" in body
    assert "Gap: 1.5" in body


def test_body_empty_candidates():
    body = build_no_match_alert_body(
        reason="no match found",
        top_candidates=[],
        match_threshold=6.0,
        gap_threshold=1.5,
        captured_at="2026-09-14T15:35:31Z",
    )
    # Should not contain "Top candidates" when list is empty
    assert "Top candidates:" not in body


def test_body_truncates_to_3_candidates():
    body = build_no_match_alert_body(
        reason="no match found",
        top_candidates=[("v_a", 9.0), ("v_b", 8.0), ("v_c", 7.0), ("v_d", 6.0)],
        match_threshold=6.0,
        gap_threshold=1.5,
        captured_at="2026-09-14T15:35:31Z",
    )
    assert "#1 v_a" in body
    assert "#2 v_b" in body
    assert "#3 v_c" in body
    assert "v_d" not in body  # 4th should be dropped
