"""
test_person_matcher.py — Tests for infra.person_matcher.match_person.

Phase 6B.106. Pure-function tests; no I/O.

Test inventory (~20 cases):
  - Color normalization (10 cases): exact enum, aliases (navy/maroon),
    case-insensitive, whitespace, invalid (returns "other"), None
  - Color similarity (6 cases): exact=1.0, related=0.7, none=0.0,
    None inputs=0.0, gray/silver=0.7
  - Match by face recognition (4 cases): high confidence wins,
    low confidence falls through to clothing, no faces=None,
    None face_recognition arg=None
  - Match by clothing color (6 cases): exact match above threshold,
    below threshold returns NoMatch, unknown color NoMatch,
    no known_persons NoMatch, empty vision_result.persons NoMatch,
    picks highest confidence
  - Integration: face wins over clothing when both would match
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


from infra.person_matcher import (
    CLOTHING_COLOR_ENUM,
    MatchVerdict,
    NoMatch,
    _color_similarity,
    _normalize_color,
    match_person,
)

# ---------------------------------------------------------------------------
# Color normalization
# ---------------------------------------------------------------------------


class TestNormalizeColor:
    def test_exact_enum_lowercase(self):
        assert _normalize_color("blue") == "blue"
        assert _normalize_color("red") == "red"

    def test_uppercase_normalized(self):
        assert _normalize_color("BLUE") == "blue"
        assert _normalize_color("Red") == "red"

    def test_whitespace_stripped(self):
        assert _normalize_color("  blue  ") == "blue"

    def test_alias_navy_to_blue(self):
        assert _normalize_color("navy") == "blue"

    def test_alias_maroon_to_red(self):
        assert _normalize_color("maroon") == "red"

    def test_alias_dark_variants(self):
        assert _normalize_color("dark blue") == "blue"
        assert _normalize_color("dark red") == "red"
        assert _normalize_color("dark green") == "green"

    def test_alias_lavender_to_purple(self):
        assert _normalize_color("lavender") == "purple"

    def test_unknown_word_returns_other(self):
        assert _normalize_color("chartreuse") == "other"

    def test_empty_string_returns_none(self):
        assert _normalize_color("") is None

    def test_none_input_returns_none(self):
        assert _normalize_color(None) is None

    def test_unknown_enum_passes_through(self):
        # "unknown" is in enum, should stay "unknown"
        assert _normalize_color("unknown") == "unknown"


# ---------------------------------------------------------------------------
# Color similarity
# ---------------------------------------------------------------------------


class TestColorSimilarity:
    def test_exact_match_is_one(self):
        assert _color_similarity("blue", "blue") == 1.0

    def test_none_inputs_return_zero(self):
        assert _color_similarity(None, "blue") == 0.0
        assert _color_similarity("blue", None) == 0.0
        assert _color_similarity(None, None) == 0.0

    def test_unrelated_colors_return_zero(self):
        assert _color_similarity("red", "blue") == 0.0
        assert _color_similarity("green", "yellow") == 0.0

    def test_gray_silver_related(self):
        assert _color_similarity("gray", "silver") == 0.7
        assert _color_similarity("silver", "gray") == 0.7

    def test_red_no_alias(self):
        # red/maroon are different enums but we'd want to catch this —
        # however the spec says enum match only. So maroon→red alias is
        # handled in normalization, not similarity.
        assert _color_similarity("red", "blue") == 0.0

    def test_clothing_color_enum_complete(self):
        # Sanity check — every enum value normalizes to itself
        for c in CLOTHING_COLOR_ENUM:
            assert _normalize_color(c) == c


# ---------------------------------------------------------------------------
# match_person — face recognition path
# ---------------------------------------------------------------------------


class TestMatchByFaceRecognition:
    def test_known_face_returns_verdict(self):
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "red"}}],
            "primary_person_index": 0,
        }
        face_recognition: dict[str, Any] = {
            "faces": [{
                "bbox": [100, 100, 200, 200],
                "embedding": [0.1] * 512,
                "identified_name": "<owner-name>",
                "confidence": 0.7,
                "is_known": True,
            }],
            "identified_person": "<owner-name>",
            "best_confidence": 0.7,
        }
        result = match_person(vision_result, [], face_recognition)
        assert isinstance(result, MatchVerdict)
        assert result.matched_name == "<owner-name>"
        assert result.matched_via == "face_recognition"
        assert result.confidence == 0.7
        assert result.face_bbox == [100, 100, 200, 200]

    def test_low_confidence_face_falls_through(self):
        # Confidence below MATCH_THRESHOLD (0.4) — fall through to
        # clothing match. With empty known_persons, that path also
        # NoMatches.
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "blue"}}],
            "primary_person_index": 0,
        }
        face_recognition = {
            "faces": [{
                "bbox": [100, 100, 200, 200],
                "embedding": [0.1] * 512,
                "identified_name": "<owner-name>",
                "confidence": 0.3,  # below 0.4
                "is_known": True,
            }],
            "identified_person": "<owner-name>",
            "best_confidence": 0.3,
        }
        result = match_person(vision_result, [], face_recognition)
        assert isinstance(result, NoMatch)

    def test_unknown_face_falls_through_to_clothing(self):
        # is_known=False → skip face path, try clothing.
        # known_persons=[<visitor-name> (blue)] → match by blue
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "blue"}}],
            "primary_person_index": 0,
        }
        face_recognition = {
            "faces": [{
                "bbox": [100, 100, 200, 200],
                "embedding": [0.1] * 512,
                "identified_name": None,
                "confidence": None,
                "is_known": False,
            }],
            "identified_person": None,
            "best_confidence": None,
        }
        known = [{"name": "<visitor-name>", "clothing_upper_color": "blue", "role": "resident"}]
        result = match_person(vision_result, known, face_recognition)
        assert isinstance(result, MatchVerdict)
        assert result.matched_name == "<visitor-name>"
        assert result.matched_via == "clothing_color"

    def test_no_faces_returns_none(self):
        # Empty faces list — _extract_face_recognition_result returns None
        face_recognition: dict[str, Any] = {
            "faces": [], "identified_person": None, "best_confidence": None
        }
        from infra.person_matcher import _extract_face_recognition_result
        assert _extract_face_recognition_result(face_recognition) is None

    def test_none_face_recognition_skips_path(self):
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "red"}}],
            "primary_person_index": 0,
        }
        known = [{"name": "<owner-name>", "clothing_upper_color": "red", "role": "owner"}]
        # face_recognition=None → skip face path, go to clothing
        result = match_person(vision_result, known, None)
        assert isinstance(result, MatchVerdict)
        assert result.matched_via == "clothing_color"


# ---------------------------------------------------------------------------
# match_person — clothing color path
# ---------------------------------------------------------------------------


class TestMatchByClothing:
    def test_exact_match_above_threshold(self):
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "red"}}],
            "primary_person_index": 0,
        }
        known = [{"name": "<owner-name>", "clothing_upper_color": "red", "role": "owner"}]
        result = match_person(vision_result, known)
        assert isinstance(result, MatchVerdict)
        assert result.matched_name == "<owner-name>"
        assert result.matched_via == "clothing_color"
        assert result.confidence == 1.0

    def test_below_threshold_returns_no_match(self):
        # Phase 6B.163 (Tier 3 stable-attribute match added) — this
        # test was written for the pre-6B.163 two-tier matcher where
        # clothing_unknown was the only failure reason. After 6B.163,
        # an unknown clothing color falls through to Tier 3, which
        # also has no data to score (no stable_attributes on the
        # detected person), so the final reason is
        # stable_attributes_no_match.
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "unknown"}}],
            "primary_person_index": 0,
        }
        known = [{"name": "<owner-name>", "clothing_upper_color": "red", "role": "owner"}]
        result = match_person(vision_result, known)
        assert isinstance(result, NoMatch)
        assert result.reason == "stable_attributes_no_match"

    def test_alias_normalization_matches(self):
        # Detected "navy" → normalized to blue, matches blue enrollment.
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "navy"}}],
            "primary_person_index": 0,
        }
        known = [{"name": "<visitor-name>", "clothing_upper_color": "blue", "role": "resident"}]
        result = match_person(vision_result, known)
        assert isinstance(result, MatchVerdict)
        assert result.matched_name == "<visitor-name>"
        assert result.confidence == 1.0  # exact after normalization

    def test_unknown_color_returns_no_match(self):
        # Phase 6B.163 (Tier 3 stable-attribute match added) — see
        # test_below_threshold_returns_no_match for the same reason
        # change rationale. "chartreuse" normalizes to "other" via
        # _normalize_color, fails clothing similarity, falls through
        # to Tier 3 which has no stable_attributes on the detected
        # person → stable_attributes_no_match.
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "chartreuse"}}],
            "primary_person_index": 0,
        }
        known = [{"name": "<owner-name>", "clothing_upper_color": "red", "role": "owner"}]
        result = match_person(vision_result, known)
        assert isinstance(result, NoMatch)
        assert result.reason == "stable_attributes_no_match"

    def test_none_color_returns_no_match(self):
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": None}}],
            "primary_person_index": 0,
        }
        known = [{"name": "<owner-name>", "clothing_upper_color": "red", "role": "owner"}]
        result = match_person(vision_result, known)
        assert isinstance(result, NoMatch)

    def test_no_persons_returns_no_match(self):
        vision_result = {"persons": [], "primary_person_index": 0}
        known = [{"name": "<owner-name>", "clothing_upper_color": "red", "role": "owner"}]
        result = match_person(vision_result, known)
        assert isinstance(result, NoMatch)
        assert result.reason == "no_person_in_frame"

    def test_no_known_persons_returns_no_match(self):
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "red"}}],
            "primary_person_index": 0,
        }
        result = match_person(vision_result, [])
        assert isinstance(result, NoMatch)
        assert result.reason == "no_known_persons"

    def test_picks_highest_confidence(self):
        # Two candidates, one is exact match (red), one is partial (gray).
        # Should pick red.
        vision_result = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "red"}}],
            "primary_person_index": 0,
        }
        known = [
            {"name": "Alice", "clothing_upper_color": "gray", "role": "resident"},
            {"name": "Bob", "clothing_upper_color": "red", "role": "owner"},
        ]
        result = match_person(vision_result, known)
        assert isinstance(result, MatchVerdict)
        assert result.matched_name == "Bob"
        assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# match_person — primary_person_index selection
# ---------------------------------------------------------------------------


class TestPrimaryPersonIndex:
    def test_uses_primary_person_index(self):
        # Two persons, primary is index 1
        vision_result = {
            "persons": [
                {"person_id": "p1", "clothing_upper": {"color": "blue"}},
                {"person_id": "p2", "clothing_upper": {"color": "red"}},
            ],
            "primary_person_index": 1,
        }
        known = [
            {"name": "Alice", "clothing_upper_color": "red", "role": "owner"},
        ]
        result = match_person(vision_result, known)
        assert isinstance(result, MatchVerdict)
        assert result.matched_name == "Alice"

    def test_invalid_index_falls_back_to_zero(self):
        vision_result = {
            "persons": [
                {"person_id": "p1", "clothing_upper": {"color": "red"}},
            ],
            "primary_person_index": 99,  # out of bounds
        }
        known = [{"name": "Bob", "clothing_upper_color": "red", "role": "owner"}]
        result = match_person(vision_result, known)
        assert isinstance(result, MatchVerdict)
        assert result.matched_name == "Bob"


# ---------------------------------------------------------------------------
# Integration — face wins over clothing
# ---------------------------------------------------------------------------


class TestFaceBeatsClothing:
    def test_face_match_takes_precedence(self):
        # Two different colors — face says "<owner-name>" (red shirt),
        # clothing would match <visitor-name> (red shirt enrolled for <visitor-name>),
        # but clothing also has a blue candidate. Face should win.
        vision_result = {
            "persons": [{
                "person_id": "p1",
                "clothing_upper": {"color": "blue"},
                "face_visible": True,
            }],
            "primary_person_index": 0,
        }
        face_recognition = {
            "faces": [{
                "bbox": [100, 100, 200, 200],
                "embedding": [0.1] * 512,
                "identified_name": "<owner-name>",
                "confidence": 0.85,
                "is_known": True,
            }],
            "identified_person": "<owner-name>",
            "best_confidence": 0.85,
        }
        known = [
            {"name": "<visitor-name>", "clothing_upper_color": "blue", "role": "resident"},
        ]
        result = match_person(vision_result, known, face_recognition)
        assert isinstance(result, MatchVerdict)
        assert result.matched_name == "<owner-name>"  # face wins
        assert result.matched_via == "face_recognition"
