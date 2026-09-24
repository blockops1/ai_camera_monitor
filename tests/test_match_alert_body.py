"""Tests for telegram_formatter.match_alert — build_match_alert_body + full message.

Updated for US-067c: stripped debug lines, TG marker, individual Make/Model fields.
"""

from __future__ import annotations

from telegram_formatter.match_alert import build_match_alert_body, build_match_message

# ---------------------------------------------------------------------------
# build_match_alert_body — high-confidence match
# ---------------------------------------------------------------------------


def test_body_high_confidence_match():
    """Matched alert contains label, and gap (no Confidence/Runner-ups lines)."""
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
    # US-067c: individual Make/Model fields
    assert "Make: Ford" in body
    assert "Model: F150" in body
    assert "ID: v_test" in body
    assert "Color: red" in body
    assert "Owner: Jane Doe" in body
    assert "Label: Test car" in body
    assert "Gap: 3.0" in body
    # Debug lines removed
    assert "Confidence:" not in body
    assert "Runner-ups:" not in body


def test_body_low_confidence_with_gap_zero():
    """Matched alert with score in [0,1] range and gap=0."""
    match_result = {
        "matched": True,
        "known_vehicle": {
            "id": "v2",
            "label": "Blue sedan",
        },
    }
    body = build_match_alert_body(match_result, {}, score=0.85, gap=0.0,
                                   runner_ups=[("v2_copy", 0.85)])
    assert "Blue sedan" in body
    # US-067c: gap=0 -> no Gap line, no runner-ups section
    assert "Gap:" not in body
    assert "Runner-ups:" not in body
    # No color/make/model/body lines
    assert "Color:" not in body
    assert "Make:" not in body
    assert "Model:" not in body


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
    """When known_vehicle has no label, body shows '*'?*' (bold fallback)."""
    match_result = {
        "matched": True,
        "known_vehicle": {
            "id": "v_nolabel",
        },
    }
    body = build_match_alert_body(match_result, {}, score=3.0, gap=0.5)
    # US-067c: label is always bold
    assert "Match \u2014 *?*" in body
    assert "Gap: 0.5" in body


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
    result = build_match_message(match_result, {}, camera_label="Front Gate", position=2)
    caption = result["caption"]
    assert "Camera: Front Gate" in caption
    assert "Match" in caption
    assert "Test car" in caption
    # US-067c: no Confidence line, gap instead
    assert "Gap:" in caption
    assert "Confidence:" not in caption
    assert "Runner-ups:" not in caption
    # TG marker
    assert "TG 2 of 3" in caption


def test_message_unmatched():
    """Unmatched alert shows minimal format with runner-up."""
    match_result = {"matched": False}
    result = build_match_message(match_result, {}, camera_label="Gate",
                                  top_candidates=[("v_a", 5.0)],
                                  position=1)
    caption = result["caption"]
    assert "⚠️ No match (confidence below threshold)" in caption
    assert "Runner-up: *v_a* (5.0)" in caption
    assert "TG 1 of 3" in caption
    assert "Match" not in caption
    assert "Thresholds:" not in caption
    assert "Reason:" not in caption


def test_message_unmatched_person_classification():
    """Unmatched alert uses classification-aware wording."""
    match_result = {"matched": False}
    vm2_result = {"class": "person"}
    result = build_match_message(match_result, vm2_result, camera_label="Gate",
                                  top_candidates=[("v_a", 5.0)],
                                  position=1)
    caption = result["caption"]
    # No-match: no status line, just emoji header
    assert "unrecognized person" not in caption  # old behavior removed
    # But the header is still the no-match header
    assert "⚠️ No match" in caption


def test_message_unmatched_with_top_candidates():
    """Unmatched alert with top_candidates shows slimmed body."""
    match_result = {"matched": False}
    vm2_result = {"class": "vehicle"}
    result = build_match_message(
        match_result,
        vm2_result,
        camera_label="Gate",
        classification="vehicle",
        reason="below confidence threshold",
        top_candidates=[("v_a", 5.0), ("v_b", 2.0)],
        match_threshold=6.0,
        gap_threshold=1.5,
        position=1,
    )
    caption = result["caption"]
    # US-067c: no Status: line, no Thresholds:, no Reason:
    assert "Status:" not in caption
    assert "Thresholds:" not in caption
    assert "Reason:" not in caption
    # Only top 1 runner-up
    assert "Runner-up: *v_a*" in caption
    assert "v_b" not in caption


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
        match_result, {}, camera_label="Cam1", alert=alert, position=3,
    )
    caption = result["caption"]
    assert "Alert 3 of 3" in caption
    # Alert ID removed per US-073g
    assert "Alert ID:" not in caption
    assert "Timestamp: <code>2026-09-14 06:00:00 EDT</code>" in caption


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


# ---------------------------------------------------------------------------
# AC (US-056a port): flat match-result dict shape (the actual runtime shape)
# ---------------------------------------------------------------------------


def test_body_flat_match_result_shape():
    """Match result is a FLAT dict (dict(best_candidate)), no known_vehicle envelope.

    match_vehicle() returns dict(best_candidate) with matched/score/all_scores
    merged on top. The old buggy code did match_result.get("known_vehicle")
    and got None, so all fields rendered as "?". After the fix, the body
    correctly reads the flat dict.
    """
    flat_match = {
        "id": "v_flat",
        "label": "Flat Truck",
        "color": "blue",
        "make": "Chevy",
        "model": "Silverado",
        "owner": "Bob",
        "type": "pickup",
        "matched": True,
        "score": 7.5,
        "all_scores": [("v_flat", 7.5), ("v_other", 3.0)],
    }
    body = build_match_alert_body(flat_match, {}, score=7.5, gap=4.5,
                                   runner_ups=[("v_other", 3.0)])
    assert "Flat Truck" in body, f"label missing from body: {body!r}"
    assert "v_flat" in body, f"id missing: {body!r}"
    assert "Color: blue" in body
    assert "Owner: Bob" in body
    # US-067c: individual Make/Model lines
    assert "Make: Chevy" in body
    assert "Model: Silverado" in body
    # CRITICAL: the bug rendered "?" when flat dict was passed. Make sure
    # that "?" is NOT in label/id positions.
    assert "Match \u2014 ?" not in body, f"label collapsed to ?: {body!r}"


def test_body_flat_no_match():
    """Flat dict with matched=False still produces a body (caller decides routing).

    build_match_alert_body is the MATCHED body builder. The dispatcher only
    calls it when match_result['matched'] is True. If a caller passes
    matched=False, the body still renders (with '?' fields) but the caller
    should route to the no-match path. This test pins the contract:
    the function does not raise and returns a string.
    """
    flat_no_match = {"matched": False}
    body = build_match_alert_body(flat_no_match, {}, score=0.0, gap=0.0)
    assert isinstance(body, str)
    # Confirmed: function doesn't crash on flat no-match shape.
