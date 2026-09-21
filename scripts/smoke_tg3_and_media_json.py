#!/usr/bin/env python3.11
"""Smoke test for US-056a (TG#3 vehicle display) + US-056b
(sendMediaGroup JSON encoding).

End-to-end smoke:
  1. Load real data/vehicles/known_vehicles.json
  2. Run match_vehicle() against a synthetic VM2 result
  3. build_match_message -> assert no '?' placeholder in body
  4. Build send_media_group data with a quoted caption -> json.loads valid
  5. Print "SMOKE OK" on success

Does NOT call the Telegram API — all I/O is local.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure project root is on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telegram_formatter.match_alert import build_match_alert_body, build_match_message


def load_known_vehicles() -> dict:
    """Load the real known_vehicles.json from the data directory."""
    data_dir = PROJECT_ROOT / "data" / "vehicles"
    path = data_dir / "known_vehicles.json"
    assert path.exists(), f"known_vehicles.json not found at {path}"
    with open(path) as f:
        return json.load(f)


def smoke_build_match_message():
    """Test build_match_message with a real known vehicle from disk."""
    vehicles = load_known_vehicles()
    entries = vehicles.get("entries", [])
    if not entries:
        print("SMOKE SKIP: known_vehicles.json has no entries")
        return

    # Pick the first real vehicle entry.
    real = entries[0]
    plate = real.get("plate", "SMOKE_TEST")
    label = real.get("label", "Smoke Car")
    owner = real.get("owner", "Test Owner")

    # Build a synthetic match_result as match_vehicle() would return
    # after matching this vehicle (flat dict shape).
    match_result = {
        "matched": True,
        "id": plate,
        "label": label,
        "color": real.get("color", "unknown"),
        "make": real.get("make", ""),
        "model": real.get("model", ""),
        "owner": owner,
        "type": real.get("body_type", real.get("type", "")),
        "score": 8.7,
        "all_scores": [(plate, 8.7), ("other_plate", 3.1)],
    }
    vm2_result = {
        "class": "vehicle",
        "license_plate": plate,
        "distinctive_features": [],
    }

    # Build the message.
    result = build_match_message(
        match_result, vm2_result,
        camera_label="Front Gate",
    )
    caption = result["caption"]

    # CRITICAL: no '?' placeholders for label or id.
    assert "?" not in caption.split("ID:")[1].split("\n")[0], \
        f"ID placeholder found in: {caption!r}"
    assert label in caption, \
        f"Real label '{label}' not in caption: {caption!r}"
    assert owner in caption, \
        f"Real owner '{owner}' not in caption: {caption!r}"

    # Photos must be empty (text-only).
    assert result["photos"] == [], "build_match_message should not set photos"

    print(f"  build_match_message OK — label='{label}', id='{plate}'")


def smoke_build_match_alert_body():
    """Test build_match_alert_body with flat dict (US-056a fix)."""
    match_result = {
        "id": "SMOKE_FLAT",
        "label": "Flat Match Car",
        "color": "blue",
        "matched": True,
        "score": 7.0,
        "all_scores": [("SMOKE_FLAT", 7.0)],
    }
    body = build_match_alert_body(
        match_result, {}, score=7.0, gap=2.0, runner_ups=None,
    )
    assert "Flat Match Car" in body, f"label missing: {body!r}"
    assert "SMOKE_FLAT" in body, f"id missing: {body!r}"
    assert "Match \u2014 ?" not in body, f"label collapsed to ?: {body!r}"
    print("  build_match_alert_body OK — flat dict shape works")


def smoke_media_group_json():
    """Test send_media_group data builder with nested-quote caption."""
    caption = 'Plate: "SMOKE TEST 123"'
    photo_paths = ["/tmp/fake_1.jpg", "/tmp/fake_2.jpg"]

    media: list[dict[str, str]] = []
    for i, path in enumerate(photo_paths):
        attach_ref = f"attach://photo_{i}"
        entry = {"type": "photo", "media": attach_ref}
        if i == 0:
            entry["caption"] = caption
        media.append(entry)

    media_json = json.dumps(media)
    parsed = json.loads(media_json)

    assert parsed[0]["caption"] == caption, \
        f"caption mismatch after round-trip: {parsed[0]['caption']!r}"
    print("  send_media_group JSON OK — nested quotes preserved")


def smoke_no_match_path():
    """Test no-match path of build_match_message."""
    match_result = {"matched": False}
    vm2_result = {"class": "vehicle"}
    result = build_match_message(
        match_result, vm2_result,
        camera_label="Lot B",
        classification="vehicle",
        reason="below confidence threshold",
        top_candidates=[("suspect_1", 2.5), ("suspect_2", 1.8)],
        match_threshold=6.0,
        gap_threshold=1.5,
    )
    assert "unrecognized vehicle" in result["caption"]
    assert "suspect_1" in result["caption"]
    print("  no-match path OK")


if __name__ == "__main__":
    print("=== smoke_tg3_and_media_json.py ===")

    smoke_build_match_alert_body()
    smoke_build_match_message()
    smoke_media_group_json()
    smoke_no_match_path()

    print("SMOKE OK")
