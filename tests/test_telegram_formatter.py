"""Unit tests for build_match_message() compact mode captions.

Tests the 'mode' parameter on build_match_message():
- mode='person' matched: 'person: <label> (cosine=X.XX, tier1=X.XX)'
- mode='person' no-match: 'person (unknown)'

No real Telegram dispatch — pure string assertions on caption format.
"""
from __future__ import annotations

from telegram_formatter.match_alert import build_match_message

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _person_match_result(label="Jeremiah", cosine=0.91, tier1=0.6):
    """Build a person match result dict for mode='person' tests."""
    return {
        "matched": True,
        "candidate": {"id": "p1", "label": label},
        "cosine_score": cosine,
        "tier1_score": tier1,
    }


def _person_no_match_result():
    """Build a person no-match result dict."""
    return {
        "matched": False,
    }


# ---------------------------------------------------------------------------
# Tests: mode='person'
# ---------------------------------------------------------------------------

class TestBuildMatchMessagePersonMode:
    """Compact 'mode=person' caption format for build_match_message."""

    def test_matched_person_caption(self):
        """AC1: mode='person' matched produces 'person: <label> (cosine=X.XX, tier1=X.XX)'.

        Exact format: 'person: Jeremiah (cosine=0.91, tier1=0.60)'.
        """
        msg = build_match_message(
            _person_match_result(label="Jeremiah", cosine=0.91, tier1=0.6),
            {},
            camera_label="FRONT",
            mode="person",
            position=3,
        )
        caption = msg["caption"]
        assert "person: Jeremiah (cosine=0.91, tier1=0.60)" in caption

    def test_unmatched_person_caption(self):
        """AC2: mode='person' no-match produces 'person (unknown)'.

        Exact format: 'person (unknown)'.
        """
        msg = build_match_message(
            _person_no_match_result(),
            {},
            camera_label="FRONT",
            mode="person",
            position=3,
        )
        caption = msg["caption"]
        assert "person (unknown)" in caption
