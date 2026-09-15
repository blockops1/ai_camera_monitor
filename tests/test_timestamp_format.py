"""Tests for infra.format_ts.format_local_timestamp — UTC ISO → local display.

Covers:
  1. UTC ISO +0000 suffix → EDT
  2. Z-suffix input
  3. Empty string
  4. Garbage input (graceful fallback)
  5. Alternative timezone (America/Los_Angeles → PDT)
  6. Integration: build_match_message caption contains formatted timestamp
"""

from __future__ import annotations

from infra.format_ts import format_local_timestamp
from telegram_formatter.match_alert import build_match_message

# ---------------------------------------------------------------------------
# 1. UTC ISO +0000 → EDT (default TZ America/New_York)
# ---------------------------------------------------------------------------

def test_utc_iso_plus0000():
    """UTC ISO with +0000 suffix converts to EDT."""
    result = format_local_timestamp("2026-09-15T15:10:48.000+0000")
    assert result == "2026-09-15 11:10:48 EDT"


# ---------------------------------------------------------------------------
# 2. Z-suffix
# ---------------------------------------------------------------------------

def test_z_suffix():
    """Z-suffix ISO converts correctly."""
    result = format_local_timestamp("2026-09-15T15:10:48Z")
    assert result == "2026-09-15 11:10:48 EDT"


# ---------------------------------------------------------------------------
# 3. Empty string
# ---------------------------------------------------------------------------

def test_empty_string():
    """Empty string returns empty string."""
    assert format_local_timestamp("") == ""


# ---------------------------------------------------------------------------
# 4. Garbage / unparseable
# ---------------------------------------------------------------------------

def test_garbage_fallback():
    """Unparseable input returns the original string unchanged."""
    assert format_local_timestamp("not-a-timestamp") == "not-a-timestamp"


def test_none_returns_empty():
    """None input returns empty string."""
    assert format_local_timestamp(None) == ""


# ---------------------------------------------------------------------------
# 5. Alternative timezone (America/Los_Angeles → PDT)
# ---------------------------------------------------------------------------

def test_alt_tz_los_angeles():
    """America/Los_Angeles converts UTC 15:10 to PDT 08:10."""
    result = format_local_timestamp(
        "2026-09-15T15:10:48.000+0000",
        tz_name="America/Los_Angeles",
    )
    assert result == "2026-09-15 08:10:48 PDT"


def test_alt_tz_utc():
    """UTC timezone preserves the original time."""
    result = format_local_timestamp(
        "2026-09-15T15:10:48.000+0000",
        tz_name="UTC",
    )
    assert result == "2026-09-15 15:10:48 UTC"


# ---------------------------------------------------------------------------
# 7. Invalid timezone name falls back to raw input (regression for QA BLOCK)
# ---------------------------------------------------------------------------

def test_invalid_tz_name_falls_back_gracefully():
    """A typo in DISPLAY_TZ must NOT crash the daemon — falls back to raw UTC string."""
    # Realistic operator typo: extra characters in the IANA name
    result = format_local_timestamp(
        "2026-09-15T15:10:48.000+0000",
        tz_name="America/New_Yorxk",  # intentional typo
    )
    # Must return the raw input, NOT raise ZoneInfoNotFoundError
    assert result == "2026-09-15T15:10:48.000+0000"


def test_empty_tz_name_falls_back_to_default():
    """An empty tz_name falls back to the module default (America/New_York)."""
    result = format_local_timestamp(
        "2026-09-15T15:10:48.000+0000",
        tz_name="",
    )
    # Empty string is falsy → falls back to default TZ
    assert result == "2026-09-15 11:10:48 EDT"


# ---------------------------------------------------------------------------
# 6. Integration: build_match_message body contains formatted timestamp
# ---------------------------------------------------------------------------

def test_build_match_message_caption_has_local_timestamp():
    """build_match_message caption includes formatted local timestamp."""
    match_result = {
        "matched": True,
        "known_vehicle": {"id": "v1", "label": "Test car"},
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
    assert "Timestamp: 2026-09-14 06:00:00 EDT" in caption
    # Verify the raw UTC string is NOT present
    assert "2026-09-14T10:00:00Z" not in caption
