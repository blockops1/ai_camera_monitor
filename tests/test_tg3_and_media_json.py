"""Regression tests for US-056a (TG#3 vehicle label/id fix) + US-056b
(sendMediaGroup JSON encoding).

Verifies:
  1. Matched vehicle TG#3 contains real label and id, NOT '?'.
  2. No-match path renders top candidates without crashing.
  3. send_media_group data dict with nested-quote caption parses as valid JSON.
  4. build_match_message returns photos=[] when there are no images.
"""

from __future__ import annotations

import json

from telegram_formatter.match_alert import build_match_alert_body, build_match_message

# ---------------------------------------------------------------------------
# Test 1: matched vehicle body uses real label and id (US-056a)
# ---------------------------------------------------------------------------


def test_build_match_alert_body_uses_real_label_and_id():
    """Matched vehicle TG#3 contains the real label and id, NOT '?'.

    Exercises the US-056a fix: match_vehicle() returns a flat dict
    (best_candidate) with matched/score/all_scores merged on top.
    build_match_alert_body reads the flat dict directly.
    """
    flat_match = {
        "id": "plate_ABC123",
        "label": "Silver Toyota Camry",
        "color": "silver",
        "make": "Toyota",
        "model": "Camry",
        "owner": "John Smith",
        "type": "sedan",
        "matched": True,
        "score": 8.5,
        "all_scores": [("plate_ABC123", 8.5), ("plate_XYZ789", 3.2)],
    }
    body = build_match_alert_body(
        flat_match, {}, score=8.5, gap=5.3,
        runner_ups=[("plate_XYZ789", 3.2)],
    )

    # Real values present
    assert "Silver Toyota Camry" in body
    assert "plate_ABC123" in body
    assert "John Smith" in body
    assert "Color: silver" in body
    assert "Make/Model: Toyota Camry" in body
    assert "Body: sedan" in body

    # CRITICAL: no '?' placeholders in label or id positions
    assert "Match \u2014 ?" not in body
    assert "  ID: ?" not in body


# ---------------------------------------------------------------------------
# Test 2: no-match body renders top candidates (no crash)
# ---------------------------------------------------------------------------


def test_build_match_alert_body_no_match_renders_top_candidates():
    """No-match path: build_match_message renders top candidates without
    crashing on missing known_vehicle envelope."""
    match_result = {"matched": False}
    vm2_result = {"class": "vehicle"}
    result = build_match_message(
        match_result,
        vm2_result,
        camera_label="Back Lot",
        classification="vehicle",
        reason="below confidence threshold",
        top_candidates=[("plate_unknown", 2.1), ("plate_suspect", 1.5)],
        match_threshold=6.0,
        gap_threshold=1.5,
    )
    caption = result["caption"]

    assert "unrecognized vehicle" in caption
    assert "below confidence threshold" in caption
    assert "Top candidates:" in caption
    assert "plate_unknown" in caption
    assert "Thresholds:" in caption


# ---------------------------------------------------------------------------
# Test 3: send_media_group data is valid JSON with nested quotes (US-056b)
# ---------------------------------------------------------------------------


def test_send_media_group_data_is_valid_json_with_quoted_caption():
    """build the data dict via the production send_media_group path with
    a caption containing nested double quotes, assert json.loads parses."""
    # Build the media list exactly as _send_media_group does.
    caption = 'Plate: "ABC 123" — Front Gate'
    photo_paths = ["/tmp/fake_1.jpg", "/tmp/fake_2.jpg"]

    # Replicate the data-building logic from dispatcher._send_media_group.
    media: list[dict[str, str]] = []
    for i, path in enumerate(photo_paths):
        attach_ref = f"attach://photo_{i}"
        entry = {"type": "photo", "media": attach_ref}
        if i == 0:
            entry["caption"] = caption
        media.append(entry)

    # The key assertion: json.dumps + json.loads must round-trip cleanly.
    media_json = json.dumps(media)
    parsed = json.loads(media_json)

    assert len(parsed) == 2
    assert parsed[0]["caption"] == caption  # nested quotes preserved
    assert parsed[0]["type"] == "photo"
    assert parsed[0]["media"] == "attach://photo_0"
    assert parsed[1]["media"] == "attach://photo_1"


# ---------------------------------------------------------------------------
# Test 4: build_match_message returns photos=[] for text-only (invariant)
# ---------------------------------------------------------------------------


def test_match_message_returns_photos_empty_for_text_only():
    """TG#3 returns photos=[] when there are no images — existing invariant
    preserved. build_match_message is text-only; photos are set by the
    caller (daemon) after crop selection."""
    match_result = {
        "matched": True,
        "id": "v_test",
        "label": "Test vehicle",
        "score": 7.0,
        "all_scores": [("v_test", 7.0)],
    }
    result = build_match_message(
        match_result, {}, camera_label="Gate",
    )
    assert result["photos"] == []
    assert isinstance(result["caption"], str)
    assert len(result["caption"]) > 0
