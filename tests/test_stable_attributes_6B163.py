"""
test_stable_attributes_6B163.py — Phase 6B.163 Tier 3 stable-attribute
matching tests.

Tests for the 6 stable visual attributes (silhouette, skin_tone,
age_range, hair, facial_hair, glasses) used as a Tier 3 fallback in
infra/person_matcher.py when face recognition (Tier 1) and clothing
color (Tier 2) both miss.

Per AGENTS.md §3.4: tests must pass before commit. Per PLAN.md §11.85:
7 tests covering schema, similarity helpers, scoring, match_person
integration, backward-compat, threshold, and weight-sum sanity.

These tests are pure-Python (no Qwen, no InsightFace, no network) —
they exercise the matcher logic with synthetic vision_result and
known_persons dicts.
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from infra.person_matcher import (
    AGE_RANGE_NEIGHBORS,
    HAIR_LENGTH_NEIGHBORS,
    STABLE_ATTRIBUTES_MATCH_THRESHOLD,
    STABLE_ATTRIBUTES_WEIGHTS,
    MatchVerdict,
    NoMatch,
    _attr_similarity,
    _attr_similarity_with_neighbors,
    _extract_stable_attributes,
    _score_stable_attributes,
    match_person,
)

# ---------------------------------------------------------------------------
# Fixtures — synthetic primary-person and known-identity dicts
# ---------------------------------------------------------------------------


def _primary_person(stable: dict | None = None, clothing_color: str = "blue") -> dict:
    """Build a vision_result-shaped primary person dict.

    The stable dict mirrors the new PERSON_SCHEMA_JSON stable_attributes
    shape (silhouette.{build,height}, skin_tone, age_range,
    hair.{color,length,style}, facial_hair, glasses).
    """
    person = {
        "person_id": "p1",
        "clothing_upper": {"color": clothing_color, "type": "shirt"},
        "clothing_lower": {"color": "black", "type": "pants"},
        "carrying": [],
        "action": "standing",
        "face_visible": False,  # Tier 3 fires when face_visible=False (no Tier 1)
        "face_bbox": None,
        "scene_description": "test",
        "confidence": 0.9,
    }
    if stable is not None:
        person.update(stable)
    return person


def _vision(person: dict) -> dict:
    return {"persons": [person], "primary_person_index": 0}


def _known_person(name: str, clothing_color: str = "blue", stable: dict | None = None) -> dict:
    out: dict = {"name": name, "role": "owner", "clothing_upper_color": clothing_color}
    if stable is not None:
        out["stable_attributes"] = stable
    return out


def _full_stable(
    build: str | None = "average",
    height: str | None = "tall",
    skin_tone: str | None = "light",
    age_range: str | None = "middle_aged",
    hair_color: str | None = "brown",
    hair_length: str | None = "short",
    hair_style: str | None = "straight",
    facial_hair: str | None = "stubble",
    glasses: str | None = "none",
) -> dict:
    """A complete stable_attributes block (all 6 fields).

    All defaults are non-None for the common case. To test None/None
    on both sides, pass skin_tone=None etc. explicitly.
    """
    return {
        "silhouette": {"build": build, "height": height},
        "skin_tone": skin_tone,
        "age_range": age_range,
        "hair": {"color": hair_color, "length": hair_length, "style": hair_style},
        "facial_hair": facial_hair,
        "glasses": glasses,
    }


# ---------------------------------------------------------------------------
# Test 1 — schema: PERSON_SCHEMA_JSON includes all 6 stable attributes
# ---------------------------------------------------------------------------


def test_schema_includes_all_six_stable_attributes() -> None:
    """The person prompt schema must request the 6 new fields."""
    from infra.person_prompt_template import PERSON_SCHEMA_JSON

    required = [
        "silhouette",
        "skin_tone",
        "age_range",
        "hair",
        "facial_hair",
        "glasses",
    ]
    for field in required:
        assert field in PERSON_SCHEMA_JSON, f"PERSON_SCHEMA_JSON missing required field: {field}"

    # Silhouette sub-fields
    assert "build" in PERSON_SCHEMA_JSON, "silhouette must declare 'build'"
    assert "height" in PERSON_SCHEMA_JSON, "silhouette must declare 'height'"

    # Hair sub-fields
    assert "color" in PERSON_SCHEMA_JSON, "hair must declare 'color'"
    assert "length" in PERSON_SCHEMA_JSON, "hair must declare 'length'"
    assert "style" in PERSON_SCHEMA_JSON, "hair must declare 'style'"


# ---------------------------------------------------------------------------
# Test 2 — similarity helpers: exact match + None handling
# ---------------------------------------------------------------------------


def test_attr_similarity_exact_match_and_none() -> None:
    """Exact match → 1.0; either side None → 0.0; mismatch → 0.0."""
    assert _attr_similarity("average", "average") == 1.0
    assert _attr_similarity("average", "athletic") == 0.0
    assert _attr_similarity(None, "average") == 0.0
    assert _attr_similarity("average", None) == 0.0
    assert _attr_similarity(None, None) == 0.0
    # Case insensitive
    assert _attr_similarity("AVERAGE", "average") == 1.0


def test_attr_similarity_with_neighbors() -> None:
    """Exact match → 1.0; neighbor → 0.5; non-neighbor → 0.0."""
    # age_range: young_adult is neighbor of middle_aged
    assert (
        _attr_similarity_with_neighbors(
            "young_adult",
            "middle_aged",
            AGE_RANGE_NEIGHBORS,
        )
        == 0.5
    )
    assert (
        _attr_similarity_with_neighbors(
            "child",
            "senior",
            AGE_RANGE_NEIGHBORS,
        )
        == 0.0
    )
    # Exact
    assert (
        _attr_similarity_with_neighbors(
            "middle_aged",
            "middle_aged",
            AGE_RANGE_NEIGHBORS,
        )
        == 1.0
    )
    # hair_length neighbors
    assert (
        _attr_similarity_with_neighbors(
            "bald",
            "shaved",
            HAIR_LENGTH_NEIGHBORS,
        )
        == 0.5
    )
    # None on either side
    assert (
        _attr_similarity_with_neighbors(
            None,
            "middle_aged",
            AGE_RANGE_NEIGHBORS,
        )
        == 0.0
    )


# ---------------------------------------------------------------------------
# Test 3 — scoring: weighted sum lands in expected range
# ---------------------------------------------------------------------------


def test_score_stable_attributes_perfect_match() -> None:
    """All 6 attributes match → combined score = 1.0."""
    detected = _full_stable()
    known = _full_stable()
    score, breakdown = _score_stable_attributes(detected, known)
    assert score == 1.0, f"perfect match should score 1.0, got {score}"
    for bucket in STABLE_ATTRIBUTES_WEIGHTS:
        assert breakdown[bucket] == 1.0, f"{bucket} should be 1.0"


def test_score_stable_attributes_partial_match() -> None:
    """Some matches + some neighbors → score between threshold and 1.0."""
    detected = _full_stable(
        build="average",
        height="tall",
        skin_tone="light",
        age_range="middle_aged",
        hair_color="brown",
        hair_length="short",
        hair_style="straight",
        facial_hair="stubble",
        glasses="none",
    )
    # Known is mostly identical, but mismatched on glasses + neighbor on age
    known = _full_stable(
        build="average",  # 1.0
        height="tall",  # 1.0
        skin_tone="light",  # 1.0
        age_range="young_adult",  # neighbor → 0.5
        hair_color="brown",  # 1.0
        hair_length="short",  # 1.0
        hair_style="straight",  # 1.0
        facial_hair="stubble",  # 1.0
        glasses="prescription",  # mismatch → 0.0
    )
    score, breakdown = _score_stable_attributes(detected, known)

    # Compute expected: silhouette (avg of 1.0+1.0 = 1.0)
    #                  hair (avg of 1.0+1.0+1.0 = 1.0)
    #                  skin_tone (1.0)
    #                  facial_hair (1.0)
    #                  age_range (0.5 — neighbor)
    #                  glasses (0.0)
    expected = (
        STABLE_ATTRIBUTES_WEIGHTS["silhouette"] * 1.0
        + STABLE_ATTRIBUTES_WEIGHTS["hair"] * 1.0
        + STABLE_ATTRIBUTES_WEIGHTS["skin_tone"] * 1.0
        + STABLE_ATTRIBUTES_WEIGHTS["facial_hair"] * 1.0
        + STABLE_ATTRIBUTES_WEIGHTS["age_range"] * 0.5
        + STABLE_ATTRIBUTES_WEIGHTS["glasses"] * 0.0
    )
    assert abs(score - expected) < 1e-9, f"expected {expected}, got {score}"
    assert 0.0 < score < 1.0
    assert breakdown["age_range"] == 0.5
    assert breakdown["glasses"] == 0.0


def test_score_stable_attributes_none_on_both_sides() -> None:
    """None/None on both sides → 0.0 (we can't reward unknown)."""
    detected = _full_stable(skin_tone=None, glasses=None)
    known = _full_stable(skin_tone=None, glasses=None)
    score, breakdown = _score_stable_attributes(detected, known)
    # Silhouette and hair are full matches; skin_tone/glasses are None
    expected = (
        STABLE_ATTRIBUTES_WEIGHTS["silhouette"] * 1.0
        + STABLE_ATTRIBUTES_WEIGHTS["hair"] * 1.0
        + STABLE_ATTRIBUTES_WEIGHTS["skin_tone"] * 0.0
        + STABLE_ATTRIBUTES_WEIGHTS["facial_hair"] * 1.0
        + STABLE_ATTRIBUTES_WEIGHTS["age_range"] * 1.0
        + STABLE_ATTRIBUTES_WEIGHTS["glasses"] * 0.0
    )
    assert abs(score - expected) < 1e-9
    assert breakdown["skin_tone"] == 0.0


# ---------------------------------------------------------------------------
# Test 4 — match_person integration: Tier 3 fires when T1+T2 miss
# ---------------------------------------------------------------------------


def test_match_person_tier3_fires_after_t2_miss() -> None:
    """When face is invisible + clothing is unknown → Tier 3 should match
    a known person whose stable_attributes align with the detection."""
    stable_known = _full_stable(
        build="average",
        height="tall",
        skin_tone="light",
        age_range="middle_aged",
        hair_color="brown",
        hair_length="short",
        hair_style="straight",
        facial_hair="stubble",
        glasses="none",
    )
    known = [_known_person("<owner-name>", clothing_color="other", stable=stable_known)]

    # Detected: same as known, but clothing is "other" (won't match clothing)
    person = _primary_person(
        stable=_full_stable(
            build="average",
            height="tall",
            skin_tone="light",
            age_range="middle_aged",
            hair_color="brown",
            hair_length="short",
            hair_style="straight",
            facial_hair="stubble",
            glasses="none",
        ),
        clothing_color="other",
    )
    result = match_person(_vision(person), known, face_recognition=None)
    assert isinstance(result, MatchVerdict), (
        f"expected MatchVerdict from Tier 3, got {type(result).__name__}: {result}"
    )
    assert result.matched_name == "<owner-name>"
    assert result.matched_via == "stable_attributes"
    assert result.confidence == 1.0
    assert result.stable_attribute_scores is not None
    assert all(v == 1.0 for v in result.stable_attribute_scores.values())


def test_match_person_tier3_no_match_below_threshold() -> None:
    """If stable-attribute score is below 0.65, return NoMatch."""
    stable_known = _full_stable(
        build="slim",
        height="short",  # most things mismatch
        skin_tone="dark",
        age_range="child",
        hair_color="blonde",
        hair_length="long",
        hair_style="curly",
        facial_hair="beard",
        glasses="sunglasses",
    )
    known = [_known_person("Other", clothing_color="other", stable=stable_known)]

    # Detected: nearly everything is different from "Other"
    person = _primary_person(
        stable=_full_stable(
            build="heavy",
            height="tall",
            skin_tone="light",
            age_range="senior",
            hair_color="black",
            hair_length="bald",
            hair_style="straight",
            facial_hair="clean_shaven",
            glasses="prescription",
        ),
        clothing_color="other",
    )
    result = match_person(_vision(person), known, face_recognition=None)
    assert isinstance(result, NoMatch), (
        f"expected NoMatch for low-score Tier 3, got {type(result).__name__}"
    )
    assert result.reason in ("stable_attributes_no_match", "clothing_no_match")
    if result.best_candidate_name is not None:
        assert result.best_candidate_confidence is not None
        assert result.best_candidate_confidence < STABLE_ATTRIBUTES_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# Test 5 — backward compatibility: known persons with no stable_attributes
# ---------------------------------------------------------------------------


def test_backward_compat_no_stable_attributes_block() -> None:
    """Known persons enrolled before 6B.163 have no stable_attributes
    block. Tier 3 must not crash — it should just return NoMatch with
    the existing tier-2 clothing reason."""
    # Known: pre-6B.163 shape — only name, role, clothing_upper_color
    known = [{"name": "<owner-name>", "role": "owner", "clothing_upper_color": "blue"}]
    person = _primary_person(
        stable=_full_stable(),  # detected person has all 6 stable
        clothing_color="green",  # but clothing doesn't match "blue"
    )
    result = match_person(_vision(person), known, face_recognition=None)
    # Clothing misses → Tier 3 finds no stable_attributes block → NoMatch
    assert isinstance(result, NoMatch)
    # Should be the clothing_no_match reason (clothing tier runs first)
    assert result.reason in ("clothing_no_match", "stable_attributes_no_match")


# ---------------------------------------------------------------------------
# Test 6 — threshold + weights sanity
# ---------------------------------------------------------------------------


def test_stable_attributes_weights_sum_to_one() -> None:
    """STABLE_ATTRIBUTES_WEIGHTS must sum to 1.0 (per-bucket weight
    convention)."""
    total = sum(STABLE_ATTRIBUTES_WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9, f"weights sum to {total}, expected 1.0"
    # Each weight positive
    for name, w in STABLE_ATTRIBUTES_WEIGHTS.items():
        assert w > 0.0, f"{name} weight must be positive, got {w}"
    # Threshold in [0, 1]
    assert 0.0 < STABLE_ATTRIBUTES_MATCH_THRESHOLD <= 1.0


def test_silhouette_has_highest_weight() -> None:
    """Per maintainer 2026-08-29 priority order: silhouette is most important."""
    weights = STABLE_ATTRIBUTES_WEIGHTS
    assert weights["silhouette"] == max(weights.values()), (
        f"silhouette must be the highest-weighted bucket, got {weights}"
    )


# ---------------------------------------------------------------------------
# Test 7 — extract_stable_attributes: round-trip on the schema shape
# ---------------------------------------------------------------------------


def test_extract_stable_attributes_from_primary_person() -> None:
    """_extract_stable_attributes returns a flat dict with all 9 leaf keys
    when given a complete primary person dict."""
    person = _primary_person(
        stable=_full_stable(
            build="athletic",
            height="medium",
            skin_tone="olive",
            age_range="young_adult",
            hair_color="black",
            hair_length="medium",
            hair_style="wavy",
            facial_hair="mustache",
            glasses="sunglasses",
        ),
    )
    flat = _extract_stable_attributes(person)
    expected_keys = {
        "silhouette.build",
        "silhouette.height",
        "skin_tone",
        "age_range",
        "hair.color",
        "hair.length",
        "hair.style",
        "facial_hair",
        "glasses",
    }
    assert set(flat.keys()) == expected_keys, (
        f"expected keys {expected_keys}, got {set(flat.keys())}"
    )
    assert flat["silhouette.build"] == "athletic"
    assert flat["hair.style"] == "wavy"
    assert flat["facial_hair"] == "mustache"
    assert flat["glasses"] == "sunglasses"


def test_extract_stable_attributes_handles_missing_blocks() -> None:
    """Missing silhouette/hair blocks → None for each sub-field."""
    person = _primary_person()  # no stable attrs at all
    flat = _extract_stable_attributes(person)
    for key in (
        "silhouette.build",
        "silhouette.height",
        "hair.color",
        "hair.length",
        "hair.style",
    ):
        assert flat[key] is None, f"{key} should default to None"
    for key in ("skin_tone", "age_range", "facial_hair", "glasses"):
        assert flat[key] is None, f"{key} should default to None"


# ---------------------------------------------------------------------------
# Test 8 — face recognition still wins over Tier 3
# ---------------------------------------------------------------------------


def test_face_recognition_wins_over_stable_attributes() -> None:
    """If Tier 1 (face recognition) identifies the person, Tier 3 should
    not fire even if stable_attributes would also match."""
    stable_known = _full_stable()
    known = [_known_person("<owner-name>", clothing_color="blue", stable=stable_known)]

    person = _primary_person(stable=_full_stable(), clothing_color="blue")
    vision = _vision(person)

    # Pass a face_recognition result that identifies someone ELSE
    face_recognition = {
        "faces": [
            {
                "is_known": True,
                "identified_name": "Alice",
                "confidence": 0.85,  # above MATCH_THRESHOLD (0.4)
                "bbox": [10, 10, 100, 100],
            }
        ]
    }
    result = match_person(vision, known, face_recognition=face_recognition)
    assert isinstance(result, MatchVerdict)
    assert result.matched_via == "face_recognition"
    assert result.matched_name == "Alice"
    assert result.confidence == 0.85


# ---------------------------------------------------------------------------
# Test 9 — clothing still wins over Tier 3
# ---------------------------------------------------------------------------


def test_clothing_match_wins_over_stable_attributes() -> None:
    """If Tier 2 (clothing) identifies the person, Tier 3 should not
    fire — even if stable_attributes would also match."""
    stable_known = _full_stable()
    known = [_known_person("<owner-name>", clothing_color="blue", stable=stable_known)]

    person = _primary_person(stable=_full_stable(), clothing_color="blue")
    result = match_person(_vision(person), known, face_recognition=None)
    assert isinstance(result, MatchVerdict)
    # Clothing (1.0) ≥ Tier 3 (1.0); tier order is clothing → stable
    assert result.matched_via == "clothing_color", (
        f"clothing should win over stable, got {result.matched_via}"
    )
    assert result.matched_name == "<owner-name>"


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
