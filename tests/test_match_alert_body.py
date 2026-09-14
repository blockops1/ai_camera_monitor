"""Tests for telegram_formatter.match_alert — build_match_alert_body + full message."""

from __future__ import annotations

from telegram_formatter.match_alert import build_match_alert_body, build_match_message

# ---------------------------------------------------------------------------
# AC1: build_match_alert_body — high-confidence match
# ---------------------------------------------------------------------------


def test_body_high_confidence_match():
    """Matched alert contains label, confidence, and runner-ups."""
    match_result = {
        "matched": True,
        "known_vehicle": {
            "id": "v_test",
            "label": "Test car",
            "color": "red",
            "make": "Ford",
            "model": "F150",
            "owner": "Jane Doe",
            "type": "pickup",
        },
    }
    vm2_result = {"better_crop": "crop_a", "confidence": 0.85}
    body = build_match_alert_body(match_result, vm2_result, score=8.5, gap=3.0,
                                   runner_ups=[("v_other", 2.0), ("v_third", 1.0)])
    assert "Match" in body
    assert "Test car" in body
    assert "Confidence: 8.5" in body
    assert "gap: 3.00" in body
    assert "Runner-ups:" in body
    assert "v_other" in body
    assert "v_third" in body
    assert "ID: v_test" in body
    assert "Color: red" in body
    assert "Make/Model: Ford F150" in body
    assert "Body: pickup" in body
    assert "Owner: Jane Doe" in body


def test_body_low_confidence_with_gap_zero():
    """Matched alert with score in [0,1] range shows decimal format and gap=0."""
    match_result = {
        "matched": True,
        "known_vehicle": {
            "id": "v2",
            "label": "Blue sedan",
        },
    }
    body = build_match_alert_body(match_result, {}, score=0.85, gap=0.0,
                                   runner_ups=[("v2_copy", 0.85)])
    assert "0.85" in body
    assert "gap: 0.00" in body
    assert "v2_copy" in body
    # No color/make/model/body lines
    assert "Color:" not in body
    assert "Make/Model:" not in body
    assert "Body:" not in body


def test_body_no_plate_no_optional_fields():
    """Matched alert with minimal known_vehicle — only required fields."""
    match_result = {
        "matched": True,
        "known_vehicle": {
            "id": "v_minimal",
            "label": "Unknown vehicle",
        },
    }
    body = build_match_alert_body(match_result, {}, score=5.0, gap=1.5, runner_ups=None)
    assert "Match" in body
    assert "Unknown vehicle" in body
    assert "ID: v_minimal" in body
    # No optional fields present
    assert "Owner:" not in body
    assert "Color:" not in body
    assert "Runner-ups:" not in body


def test_body_no_runner_ups():
    """Matched alert with no runner_ups — no Runner-ups section."""
    match_result = {
        "matched": True,
        "known_vehicle": {
            "id": "v_single",
            "label": "Single match",
        },
    }
    body = build_match_alert_body(match_result, {}, score=7.0, gap=0.0, runner_ups=[])
    assert "Runner-ups:" not in body


def test_body_label_fallback():
    """When known_vehicle has no label, body shows '?'."""
    match_result = {
        "matched": True,
        "known_vehicle": {
            "id": "v_nolabel",
        },
    }
    body = build_match_alert_body(match_result, {}, score=3.0, gap=0.5)
    assert "Match \u2014 ?" in body


# ---------------------------------------------------------------------------
# build_match_message integration — matched branch
# ---------------------------------------------------------------------------


def test_message_match_includes_details():
    """build_match_message calls build_match_alert_body when matched."""
    match_result = {
        "matched": True,
        "known_vehicle": {
            "id": "v_test",
            "label": "Test car",
            "color": "red",
            "make": "Ford",
            "model": "F150",
        },
        "score": 8.5,
        "all_scores": [("v_test", 8.5), ("v_other", 5.5)],
    }
    result = build_match_message(match_result, {}, camera_label="Front Gate")
    caption = result["caption"]
    assert "Camera: Front Gate" in caption
    assert "Match" in caption
    assert "Test car" in caption
    assert "Confidence: 8.5" in caption
    assert "gap: 3.00" in caption
    assert "v_other" in caption


def test_message_unmatched():
    """Unmatched alert shows 'unrecognized vehicle'."""
    match_result = {"matched": False}
    result = build_match_message(match_result, {}, camera_label="Gate")
    caption = result["caption"]
    assert "unrecognized vehicle" in caption
    assert "Match" not in caption


def test_message_alert_metadata_included():
    """Match with alert dict includes Alert 3 of 3 / ID / Timestamp."""
    match_result = {
        "matched": True,
        "known_vehicle": {"id": "v1", "label": "A"},
        "score": 8.0,
        "all_scores": [("v1", 8.0)],
    }
    alert = {"id": "evt-001", "timestamp": "2026-09-14T10:00:00Z"}
    result = build_match_message(
        match_result, {}, camera_label="Cam1", alert=alert,
    )
    caption = result["caption"]
    assert "Alert 3 of 3" in caption
    assert "Alert ID: evt-001" in caption
    assert "Timestamp: 2026-09-14T10:00:00Z" in caption


def test_message_no_alert_metadata():
    """Match without alert dict omits Alert lines."""
    match_result = {
        "matched": True,
        "known_vehicle": {"id": "v1", "label": "A"},
        "score": 8.0,
        "all_scores": [("v1", 8.0)],
    }
    result = build_match_message(match_result, {}, camera_label="Cam1", alert=None)
    caption = result["caption"]
    assert "Alert 3 of 3" not in caption
    assert "Alert ID:" not in caption
