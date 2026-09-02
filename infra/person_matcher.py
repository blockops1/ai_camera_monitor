"""
person_matcher.py — Match a vision-derived person to enrolled identities.

STATUS: provisional (Phase 6B.163; Tier 3 stable-attribute matching added;
    will stabilize after live telemetry)
THREAD SAFETY: thread-safe (pure function over the loaded face_recognition
    cache; identity storage is file-IO bound but the matcher itself is
    stateless)

INPUTS:
    - `vision_result`: dict matching PERSON_SCHEMA_JSON shape (one call
      to Qwen3-VL/Qwen3.6). Reads the primary person's clothing_...[truncated]
    - `face_recognition`: optional dict from infra.face_recognition.recognize_faces()
      to short-circuit when ArcFace has already produced an identification
      during the identify_stage. When provided, takes precedence over
      clothing color match.
    - `known_persons`: list of {name, clothing_upper_color, role}
      (loaded from known_persons/ by infra.faces.list_identities()
      or by the caller). Empty list = match always fails.
    - Constants: CLOTHING_MATCH_THRESHOLD (default 0.40), enforced.

OUTPUTS:
    - MatchVerdict: dataclass with
        matched_name: str | None
        matched_via: "face_recognition" | "clothing_color" | None
        confidence: float (0.0-1.0)
        face_bbox: list[int] | None
      Returned when a known person is identified.
    - NoMatch: dataclass with
        reason: str ("face_visible_no_id", "clothing_no_match", etc.)
        best_candidate_name: str | None
        best_candidate_confidence: float | None
        suppress: bool — True when verified false positive; suppresses
            Telegram alert (Phase 6B.162)
      Returned when no identity is matched.

PUBLIC API:
    match_person(
        vision_result: dict,
        known_persons: list[dict],
        face_recognition: dict | None = None,
    ) -> MatchVerdict | NoMatch
        Return a MatchVerdict if a known identity matches, else NoMatch.

DOES NOT DO:
    - Does NOT call Qwen. (See infra.vision_analyzer.)
    - Does NOT detect or embed faces. (See infra.face_recognition.)
    - Does NOT load or save identity files. (See infra.faces.)
    - Does NOT normalize bbox coords — caller is responsible for
      passing vision_result in the format the prompt emits (pixel
      coords in Qwen's image space).

WHY HERE:
    Single-purpose module extracted from the larger listener pipeline
    (per Phase 6B.106 §11.36 step 6). Designed for forward-compat: §11.36b
    adds bbox-height-ratio as a third matcher signal WITHOUT restructuring
    this module — the function signature accepts additional kwargs.

CALLED BY:
    - listener.person_event_pipeline: person_match_stage
      (built §11.36 step 7)

CALLS INTO:
    - infra.faces: list_identities() to build known_persons list
      (when the matcher is called without an explicit list)

RELATED:
    - infra.person_prompt_template.PERSON_SCHEMA_JSON — the schema
      this matcher consumes
    - infra.face_recognition.recognize_faces — face_recognition dict
      shape this matcher accepts
    - data/known_persons/ — identity storage
    - PLAN.md §11.36 — design plan
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

# Phase 6B.106 — clothing color match threshold (cosine similarity).
# 0.40 mirrors vehicle matcher's pass-1 baseline; tuned per telemetry
# in §11.36b after first week of enrollments.
CLOTHING_MATCH_THRESHOLD = 0.40

# Phase 6B.163 — Tier 3 stable-attribute weighted ensemble. Priority
# order per maintainer 2026-08-29: silhouette > hair > skin_tone > facial_hair >
# age_range > glasses. Weights sum to 1.0; combined_score above
# STABLE_ATTRIBUTES_MATCH_THRESHOLD returns a MatchVerdict.
STABLE_ATTRIBUTES_MATCH_THRESHOLD = 0.65
STABLE_ATTRIBUTES_WEIGHTS: dict[str, float] = {
    "silhouette": 0.30,
    "hair": 0.25,
    "skin_tone": 0.15,
    "facial_hair": 0.10,
    "age_range": 0.10,
    "glasses": 0.10,
}

# Phase 6B.163 — valid enum values per stable attribute are documented
# in infra.person_prompt_template.PERSON_SCHEMA_JSON. Similarity helpers
# here do string-equality + neighbor lookups (HAIR_LENGTH_NEIGHBORS,
# AGE_RANGE_NEIGHBORS), so no separate enum constants are needed.

# Clothing color enum — mirrors the prompt's `clothing_upper.color`
# field. Used for color normalization before matching.
CLOTHING_COLOR_ENUM = frozenset(
    {
        "black",
        "white",
        "gray",
        "silver",
        "red",
        "blue",
        "green",
        "yellow",
        "brown",
        "orange",
        "pink",
        "purple",
        "other",
        "unknown",
    }
)


@dataclass(frozen=True)
class MatchVerdict:
    """A confident match — known identity found."""

    matched_name: str
    matched_via: str  # "face_recognition" | "clothing_color" | "stable_attributes"
    confidence: float
    face_bbox: list[int] | None = None
    # Phase 6B.163 — optional per-attribute breakdown when matched_via
    # == "stable_attributes". Maps attribute name -> 0.0-1.0 score.
    stable_attribute_scores: dict[str, float] | None = None


@dataclass(frozen=True)
class NoMatch:
    """No identity matched the vision result."""

    reason: str  # "face_visible_no_id" | "clothing_no_match" | "no_person_in_frame" | ...
    best_candidate_name: str | None = None
    best_candidate_confidence: float | None = None
    suppress: bool = False  # True when verified false positive — suppresses Telegram (Phase 6B.162)


def _normalize_color(color: str | None) -> str | None:
    """Map free-form color string to the enum, or None if invalid."""
    if color is None:
        return None
    c = color.strip().lower()
    if c in CLOTHING_COLOR_ENUM:
        return c
    # Map common variants to enum. Conservative — only obvious aliases.
    aliases = {
        "navy": "blue",
        "dark blue": "blue",
        "light blue": "blue",
        "maroon": "red",
        "dark red": "red",
        "crimson": "red",
        "olive": "green",
        "dark green": "green",
        "lime": "green",
        "tan": "brown",
        "beige": "brown",
        "lavender": "purple",
        "violet": "purple",
        "gold": "yellow",
        "mustard": "yellow",
        "grey": "gray",
        "neon": "other",
    }
    return aliases.get(c, "other" if c else None)


def _color_similarity(c1: str | None, c2: str | None) -> float:
    """Cosine-equivalent similarity score in [0.0, 1.0].

    Returns 1.0 for exact enum match, 0.7 for related-color alias
    (e.g. "navy" → "blue"), 0.0 for non-comparable (one side None or
    both unknown). Conservative — does not reward unknown-to-unknown.
    """
    if c1 is None or c2 is None:
        return 0.0
    if c1 == c2:
        return 1.0
    # Allow minor variant matches: blue ↔ blue (navy), red ↔ red (maroon).
    related = {
        "red": {"red"},  # already handled above
        "blue": {"blue"},
        "green": {"green"},
        "brown": {"brown"},
        "gray": {"gray", "silver"},  # gray/silver considered close
        "silver": {"gray", "silver"},
    }
    if c1 in related and c2 in related.get(c1, set()):
        return 0.7
    return 0.0


def _extract_primary_person(vision_result: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the primary person from PERSON_SCHEMA_JSON output."""
    persons = vision_result.get("persons") or []
    if not persons:
        return None
    idx = vision_result.get("primary_person_index", 0)
    if not isinstance(idx, int) or idx < 0 or idx >= len(persons):
        idx = 0
    selected = persons[idx]
    if isinstance(selected, dict):
        return cast(dict[str, Any], selected)
    return None


def _extract_face_recognition_result(
    face_recognition: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Pick the best-confidence face result from recognize_faces() output."""
    if not face_recognition:
        return None
    faces = face_recognition.get("faces") or []
    if not faces:
        return None
    # Filter to known faces
    known_faces = [f for f in faces if f.get("is_known")]
    if not known_faces:
        return None
    # Pick highest confidence
    best = max(known_faces, key=lambda f: f.get("confidence") or 0.0)
    return cast(dict[str, Any], best)


def _match_by_face_recognition(
    face_recognition: dict | None,
) -> MatchVerdict | None:
    """If recognize_faces() produced an identification, use it directly.

    Confidence threshold enforced here (MATCH_THRESHOLD lives in
    infra.face_recognition; we re-check to defend against the case
    where the caller passes a face_recognition dict with weak match).
    """
    if not face_recognition:
        return None
    best = _extract_face_recognition_result(face_recognition)
    if best is None:
        return None
    confidence = best.get("confidence")
    name = best.get("identified_name")
    if not name or confidence is None:
        return None
    # Re-enforce threshold (face_recognition already does, but defense
    # in depth)
    from infra.face_recognition import MATCH_THRESHOLD

    if confidence < MATCH_THRESHOLD:
        return None
    return MatchVerdict(
        matched_name=name,
        matched_via="face_recognition",
        confidence=confidence,
        face_bbox=best.get("bbox"),
    )


def _match_by_clothing(
    person: dict,
    known_persons: list[dict],
) -> MatchVerdict | NoMatch:
    """Match by clothing upper color.

    Walks known_persons, scores each by color similarity to the
    detected primary person's clothing_upper.color. Returns the
    highest-confidence match above CLOTHING_MATCH_THRESHOLD, else NoMatch.
    """
    upper = person.get("clothing_upper") or {}
    detected_color = _normalize_color(upper.get("color"))
    if not detected_color or detected_color in ("unknown", "other"):
        return NoMatch(
            reason="clothing_unknown",
            best_candidate_name=None,
            best_candidate_confidence=None,
        )
    best_name = None
    best_conf = 0.0
    for known in known_persons:
        known_color = _normalize_color(known.get("clothing_upper_color"))
        if not known_color or known_color in ("unknown", "other"):
            continue
        sim = _color_similarity(detected_color, known_color)
        if sim > best_conf:
            best_conf = sim
            best_name = known.get("name")
    if best_conf >= CLOTHING_MATCH_THRESHOLD and best_name:
        return MatchVerdict(
            matched_name=best_name,
            matched_via="clothing_color",
            confidence=best_conf,
            face_bbox=None,
        )
    return NoMatch(
        reason="clothing_no_match",
        best_candidate_name=best_name,
        best_candidate_confidence=best_conf if best_name else None,
    )


# ---------------------------------------------------------------------------
# Phase 6B.163 — Tier 3 stable-attribute matching (weighted ensemble).
#
# Falls back to a weighted combination of 6 categorical visual
# attributes when face recognition (Tier 1) and clothing color (Tier 2)
# both fail to identify the person. Each attribute returns a 0.0-1.0
# similarity; weighted sum crosses STABLE_ATTRIBUTES_MATCH_THRESHOLD
# (0.65) to confirm a match.
# ---------------------------------------------------------------------------


def _attr_similarity(detected: str | None, known: str | None) -> float:
    """Generic categorical similarity for a single stable attribute.

    Returns 1.0 for exact match, 0.0 for any mismatch or when either
    side is None (unknown-to-unknown is NOT rewarded; we cannot tell).
    Some attributes (age_range, hair_color, hair_length) get a partial
    score for nearby values to handle Qwen3.6's coarse categorization.
    """
    if detected is None or known is None:
        return 0.0
    d = detected.strip().lower()
    k = known.strip().lower()
    if d == k:
        return 1.0
    return 0.0


def _attr_similarity_with_neighbors(
    detected: str | None,
    known: str | None,
    neighbors: dict[str, frozenset[str]],
) -> float:
    """Categorical similarity with neighbor-credit for related values.

    1.0 for exact match, 0.5 for neighbors (e.g. "young_adult" near
    "middle_aged"), 0.0 otherwise. Used for ordered categories like
    age_range and hair_length where coarse buckets overlap.
    """
    if detected is None or known is None:
        return 0.0
    d = detected.strip().lower()
    k = known.strip().lower()
    if d == k:
        return 1.0
    # Check bidirectional neighbors
    if k in neighbors.get(d, frozenset()):
        return 0.5
    if d in neighbors.get(k, frozenset()):
        return 0.5
    return 0.0


# Neighbors for ordered categories — coarse partial credit
AGE_RANGE_NEIGHBORS: dict[str, frozenset[str]] = {
    "child": frozenset({"young_adult"}),
    "young_adult": frozenset({"child", "middle_aged"}),
    "middle_aged": frozenset({"young_adult", "senior"}),
    "senior": frozenset({"middle_aged"}),
}
HAIR_LENGTH_NEIGHBORS: dict[str, frozenset[str]] = {
    "bald": frozenset({"shaved"}),
    "shaved": frozenset({"bald", "short"}),
    "short": frozenset({"shaved", "medium"}),
    "medium": frozenset({"short", "long"}),
    "long": frozenset({"medium"}),
}


def _extract_stable_attributes(person: dict[str, Any]) -> dict[str, Any]:
    """Pull the stable-attribute block from a primary-person dict.

    Returns a flat dict with keys: silhouette.build, silhouette.height,
    skin_tone, age_range, hair.color, hair.length, hair.style,
    facial_hair, glasses. Missing fields default to None.
    """
    out: dict[str, Any] = {}
    sil = person.get("silhouette") or {}
    if isinstance(sil, dict):
        out["silhouette.build"] = sil.get("build")
        out["silhouette.height"] = sil.get("height")
    else:
        out["silhouette.build"] = None
        out["silhouette.height"] = None
    out["skin_tone"] = person.get("skin_tone")
    out["age_range"] = person.get("age_range")
    hair = person.get("hair") or {}
    if isinstance(hair, dict):
        out["hair.color"] = hair.get("color")
        out["hair.length"] = hair.get("length")
        out["hair.style"] = hair.get("style")
    else:
        out["hair.color"] = None
        out["hair.length"] = None
        out["hair.style"] = None
    out["facial_hair"] = person.get("facial_hair")
    out["glasses"] = person.get("glasses")
    return out


def _score_stable_attributes(
    detected_attrs: dict[str, Any],
    known_attrs: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    """Compute weighted Tier 3 similarity score.

    Args:
        detected_attrs: dict with stable attribute values. Two accepted
            shapes:
              - flat (from _extract_stable_attributes): keys are
                "silhouette.build", "silhouette.height", "hair.color",
                etc.
              - nested (raw schema): keys are "silhouette": {...},
                "hair": {...}, etc. — same shape as the saved
                stable_attributes block in identities JSON.
            Missing fields default to None.
        known_attrs: same accepted shapes as detected_attrs.

    Returns:
        (combined_score, per_attribute_scores) — combined_score is the
        weighted sum (0.0-1.0); per_attribute_scores maps each weight
        bucket name (e.g. "silhouette", "hair") to its similarity.
        None / missing values contribute 0.0 to their bucket.
    """
    detected_flat = _flatten_stable_attrs(detected_attrs)
    known_flat = _flatten_stable_attrs(known_attrs)

    bucket_scores: dict[str, float] = {}

    # Silhouette: average of build + height (each 0.0/0.5/1.0).
    build = _attr_similarity(
        detected_flat.get("silhouette.build"), known_flat.get("silhouette.build")
    )
    height = _attr_similarity(
        detected_flat.get("silhouette.height"), known_flat.get("silhouette.height")
    )
    bucket_scores["silhouette"] = (build + height) / 2.0

    # Hair: average of color + length (with neighbor credit) + style.
    hcolor = _attr_similarity(detected_flat.get("hair.color"), known_flat.get("hair.color"))
    hlength = _attr_similarity_with_neighbors(
        detected_flat.get("hair.length"),
        known_flat.get("hair.length"),
        HAIR_LENGTH_NEIGHBORS,
    )
    hstyle = _attr_similarity(detected_flat.get("hair.style"), known_flat.get("hair.style"))
    bucket_scores["hair"] = (hcolor + hlength + hstyle) / 3.0

    # Skin tone: exact match only (no neighbor credit for skin tone).
    bucket_scores["skin_tone"] = _attr_similarity(
        detected_flat.get("skin_tone"),
        known_flat.get("skin_tone"),
    )

    # Facial hair: exact match.
    bucket_scores["facial_hair"] = _attr_similarity(
        detected_flat.get("facial_hair"),
        known_flat.get("facial_hair"),
    )

    # Age range: neighbor credit (young_adult ~ middle_aged).
    bucket_scores["age_range"] = _attr_similarity_with_neighbors(
        detected_flat.get("age_range"),
        known_flat.get("age_range"),
        AGE_RANGE_NEIGHBORS,
    )

    # Glasses: exact match.
    bucket_scores["glasses"] = _attr_similarity(
        detected_flat.get("glasses"),
        known_flat.get("glasses"),
    )

    # Weighted sum
    combined = 0.0
    for bucket, weight in STABLE_ATTRIBUTES_WEIGHTS.items():
        combined += weight * bucket_scores.get(bucket, 0.0)
    return combined, bucket_scores


def _flatten_stable_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Flatten a stable-attributes dict to the leaf-key shape used by
    _score_stable_attributes. Accepts either nested or already-flat.

    Nested input:
        {"silhouette": {"build": "average", "height": "tall"},
         "hair": {"color": "brown", "length": "short", "style": "straight"},
         "skin_tone": "light", "age_range": "middle_aged",
         "facial_hair": "stubble", "glasses": "none"}

    Flat output:
        {"silhouette.build": "average", "silhouette.height": "tall",
         "hair.color": "brown", "hair.length": "short", "hair.style": "straight",
         "skin_tone": "light", "age_range": "middle_aged",
         "facial_hair": "stubble", "glasses": "none"}
    """
    if not isinstance(attrs, dict):
        return {}
    # If it's already flat (has dot-keys), return as-is.
    if any("." in k for k in attrs):
        return attrs
    out: dict[str, Any] = {}
    sil = attrs.get("silhouette")
    if isinstance(sil, dict):
        out["silhouette.build"] = sil.get("build")
        out["silhouette.height"] = sil.get("height")
    else:
        out["silhouette.build"] = None
        out["silhouette.height"] = None
    out["skin_tone"] = attrs.get("skin_tone")
    out["age_range"] = attrs.get("age_range")
    hair = attrs.get("hair")
    if isinstance(hair, dict):
        out["hair.color"] = hair.get("color")
        out["hair.length"] = hair.get("length")
        out["hair.style"] = hair.get("style")
    else:
        out["hair.color"] = None
        out["hair.length"] = None
        out["hair.style"] = None
    out["facial_hair"] = attrs.get("facial_hair")
    out["glasses"] = attrs.get("glasses")
    return out


def _match_by_stable_attributes(
    person: dict,
    known_persons: list[dict],
) -> MatchVerdict | NoMatch:
    """Phase 6B.163 — Tier 3 weighted-ensemble stable-attribute match.

    Walks known_persons (each may carry a `stable_attributes` block).
    Skips known persons with no stable_attributes block (backward-compat
    with identities enrolled before 6B.163). Returns the highest-scoring
    candidate above STABLE_ATTRIBUTES_MATCH_THRESHOLD (0.65), else
    NoMatch.

    Returns a MatchVerdict with matched_via="stable_attributes" and the
    per-attribute score breakdown in `stable_attribute_scores` for
    audit / debugging.
    """
    detected = _extract_stable_attributes(person)
    best_name = None
    best_score = 0.0
    best_breakdown: dict[str, float] = {}
    for known in known_persons:
        known_stable = known.get("stable_attributes")
        if not isinstance(known_stable, dict):
            continue
        # _score_stable_attributes accepts either nested (raw saved
        # block) or flat dict via _flatten_stable_attrs.
        score, breakdown = _score_stable_attributes(detected, known_stable)
        if score > best_score:
            best_score = score
            best_name = known.get("name")
            best_breakdown = breakdown
    if best_name and best_score >= STABLE_ATTRIBUTES_MATCH_THRESHOLD:
        return MatchVerdict(
            matched_name=best_name,
            matched_via="stable_attributes",
            confidence=best_score,
            face_bbox=None,
            stable_attribute_scores=best_breakdown,
        )
    return NoMatch(
        reason="stable_attributes_no_match",
        best_candidate_name=best_name,
        best_candidate_confidence=best_score if best_name else None,
    )


def match_person(
    vision_result: dict,
    known_persons: list[dict],
    face_recognition: dict | None = None,
) -> MatchVerdict | NoMatch:
    """Match a vision-derived person against enrolled identities.

    Args:
        vision_result: dict matching PERSON_SCHEMA_JSON shape. Reads
            persons[primary_person_index], clothing_upper.color,
            face_visible, face_bbox.
        known_persons: list of {name, clothing_upper_color, role}.
            Empty list = always NoMatch.
        face_recognition: optional dict from
            infra.face_recognition.recognize_faces(). When non-None
            and contains a known face, takes precedence over clothing
            color match.

    Returns:
        MatchVerdict: a known identity is matched (via face recognition
            or clothing color above threshold).
        NoMatch: no identity matches, with the reason logged.
    """
    person = _extract_primary_person(vision_result)
    if person is None:
        return NoMatch(reason="no_person_in_frame", suppress=True)

    # Path 1: face recognition result (already computed during identify
    # stage by the pipeline; passed through here to avoid re-running).
    face_match = _match_by_face_recognition(face_recognition)
    if face_match is not None:
        return face_match

    if not known_persons:
        return NoMatch(reason="no_known_persons")

    # Path 2: clothing color match. Per maintainer 2026-08-22, when face is
    # visible but ArcFace failed to identify, fall through to clothing
    # rather than treating it as a hard miss. The body surfaces both
    # signals ("Face: visible but not identified — matched by clothing").
    clothing_result = _match_by_clothing(person, known_persons)
    if isinstance(clothing_result, MatchVerdict):
        return clothing_result

    # Path 3 (Phase 6B.163): weighted-ensemble stable-attribute match.
    # Fires only when face (Path 1) + clothing (Path 2) both fail.
    # Skipped silently if no known person has a stable_attributes block
    # (backward-compat with identities enrolled before 6B.163).
    stable_result = _match_by_stable_attributes(person, known_persons)
    if isinstance(stable_result, MatchVerdict):
        return stable_result

    # No tier matched — return the most informative non-match.
    # Prefer clothing result's metadata (best_candidate_name) over
    # stable's, since clothing is the higher-confidence tier when it
    # gets a candidate.
    if clothing_result.best_candidate_name:
        return clothing_result
    return stable_result


__all__ = [
    "AGE_RANGE_NEIGHBORS",
    "CLOTHING_COLOR_ENUM",
    "CLOTHING_MATCH_THRESHOLD",
    "HAIR_LENGTH_NEIGHBORS",
    "STABLE_ATTRIBUTES_MATCH_THRESHOLD",
    "STABLE_ATTRIBUTES_WEIGHTS",
    "MatchVerdict",
    "NoMatch",
    "_attr_similarity",
    "_attr_similarity_with_neighbors",
    "_color_similarity",
    "_extract_stable_attributes",
    "_flatten_stable_attrs",
    "_normalize_color",
    "_score_stable_attributes",
    "match_person",
]
