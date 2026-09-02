"""
animal_matcher.py — Match detected animals against enrolled registry.

STATUS: provisional (Phase 6B.165 §11.86.2; wider-scope rewrite 2026-08-29)
THREAD SAFETY: thread-safe (pure functions, no shared mutable state)

INPUTS:
  - vision_result: dict matching the wider-scope animal schema produced
        by infra.animal_prompt_template.ANIMAL_SCHEMA_JSON. Required
        keys (Qwen returns null for missing):
            species                (str | null)  free-form species name
            species_confidence     (str | null)  "definite" | "likely" | "unsure"
            body_size              (str | null)  small | medium | large
            body_build             (str | null)  lean | stocky | athletic | compact
            coat_primary_color     (str | null)  CLOTHING_COLOR_ENUM-aligned
            coat_pattern           (str | null)  solid | bi-color | tri-color |
                                              tabby | striped | spotted
            distinctive_features   (list[str] | None)
            face_details           (dict | null) {ear_shape, tail_carriage, mask}
            estimated_age          (str | null)  juvenile | adult | senior
            sex_signal             (str | null)  male | female | neutered
            behavior               (str | None)
            scene_description      (str | None)
            confidence             (float | None) 0..1
  - known_animals: list of dicts read from data/animals/known_animals.json.
        Each entry is a wider-scope enrollment record with at minimum
        a `species` field and a `name` field; richer entries include
        distinctive_features[] and the other stable attributes.

OUTPUTS:
  - return value: AnimalMatchVerdict | AnimalNoMatch dataclass.
        Same shape as infra.person_matcher.MatchVerdict | NoMatch
        (kept for sibling symmetry).
  - The matcher's decisions are logged by the caller, not by this
    module. This module is pure: no I/O, no logging, no env reads.

PUBLIC API:
  match_animal(vision_result, known_animals,
               species_authoritative=True,
               raise_threshold_on_unsure=True) -> AnimalMatchVerdict | AnimalNoMatch
      Score detected animal against enrolled registry. Returns
      AnimalMatchVerdict when the highest-scoring known_animal
      exceeds the threshold (0.55 default, 0.65 when
      species_confidence="unsure"); AnimalNoMatch otherwise.
      Best candidate is always surfaced on NoMatch for audit.

  ANIMAL_MATCH_THRESHOLD          (float, default 0.55)
  ANIMAL_MATCH_THRESHOLD_UNSURE   (float, default 0.65)
  _normalize_species(species: str | None) -> str | None
      Map common species-name variants to canonical buckets
      ("coyote", "coydog", "fox", "wolf", "fisher", etc.). Keeps
      Qwen's free-form output matchable against enrolled names.

DOES NOT DO:
  - HTTP / Qwen calls — owned by infra.vision_analyzer.
  - Loading known_animals from disk — owned by
    known_animals/load_known_animals.py (§11.86.5).
  - Threat classification (concerning vs routine) — owned by
    §11.86.6.
  - Telegram formatting — owned by telegram_formatter/.
  - Validation of vision_result schema fields — owned by
    infra.vision_response.

WHY HERE:
  Phase 6B.165 §11.86.2 (wider-scope revision 2026-08-29). Mirrors
  infra.person_matcher's public API but with no face-rec and no
  clothing path. Stable-attribute weighted ensemble + per-feature
  similarity helpers, designed so two distinct coyotes on the same
  property are distinguishable from Qwen's distinctive_features[].

CALLED BY:
  - listener.animal_event_pipeline (planned §11.86.7)
  - infra.tests.test_animal_matcher_6B165_2

CALLS INTO:
  - infra.person_matcher (reuses _normalize_color + _color_similarity
    only — same enum alignment documented in
    infra.animal_prompt_template)

RELATED:
  - infra.animal_prompt_template.ANIMAL_SCHEMA_JSON (the schema this
    module reads from)
  - infra.person_matcher (sibling; the stable-attribute pattern
    template)
  - data/animals/known_animals.json (the registry this module
    scores against; populated by §11.86.5)
  - PLAN.md §11.86.2 (the design contract)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Re-use the clothing color helpers from person_matcher. The wider-scope
# schema's `coat_primary_color` enum aligns with CLOTHING_COLOR_ENUM
# (verified in infra/animal_prompt_template).
from infra.person_matcher import _color_similarity, _normalize_color

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Default match threshold. Matches PLAN §11.86.2 design (revised 2026-08-29).
# Lower than person pipeline's 0.65 because:
#   - fewer animals are enrolled
#   - wild animals have noisier attributes
#   - we want "unknown" over wrong match
ANIMAL_MATCH_THRESHOLD: float = 0.55

# Raised threshold when Qwen reports species_confidence="unsure". Forces
# the ensemble to compensate with stronger evidence on the OTHER
# attributes (color/markings/size/etc.).
ANIMAL_MATCH_THRESHOLD_UNSURE: float = 0.65


# ---------------------------------------------------------------------------
# Weighted ensemble (must sum to 1.00)
# ---------------------------------------------------------------------------

# Wider-scope schema revision 2026-08-29. Eight feature buckets with
# deliberate weight distribution:
#   - distinctive_features  0.30  (most identifying after species)
#   - coat_primary_color    0.20
#   - body_size             0.15
#   - body_build            0.10
#   - face_details          0.10  (coyote-vs-wolf-vs-fox textbook)
#   - coat_pattern          0.05
#   - estimated_age         0.05
#   - sex_signal            0.05
WEIGHTS: dict[str, float] = {
    "distinctive_features": 0.30,
    "coat_primary_color":   0.20,
    "body_size":            0.15,
    "body_build":           0.10,
    "face_details":         0.10,
    "coat_pattern":         0.05,
    "estimated_age":        0.05,
    "sex_signal":           0.05,
}

# Sanity guard — if these ever don't sum to 1.0 the scoring math is wrong.
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, (
    f"WEIGHTS must sum to 1.00; got {sum(WEIGHTS.values())}"
)


# ---------------------------------------------------------------------------
# Species normalization
# ---------------------------------------------------------------------------

# Maps free-form species strings returned by Qwen into canonical
# buckets. Two goals:
#   1. Match common variants against enrolled species names
#      ("Eastern coyote" → "coyote").
#   2. Preserve hybrid/ambiguous species names so the matcher can
#      match against enrolled coydogs/wolf-dog hybrids etc.
SPECIES_NORMALIZATION: dict[str, str] = {
    # Coyote variants
    "coyote":            "coyote",
    "eastern coyote":    "coyote",
    "western coyote":    "coyote",
    "brush wolf":        "coyote",
    "prairie wolf":      "coyote",
    "american jackal":   "coyote",
    # Coydog / hybrid
    "coydog":            "coydog",
    "coydog hybrid":     "coydog",
    "coyote-dog hybrid": "coydog",
    "coy-wolf hybrid":   "coydog",  # on the property this is the likely call
    # Fox
    "fox":               "fox",
    "red fox":           "fox",
    "gray fox":          "fox",
    "arctic fox":        "fox",
    # Wolf
    "wolf":              "wolf",
    "gray wolf":         "wolf",
    "timber wolf":       "wolf",
    "eastern wolf":      "wolf",
    # Domestic dog (any breed — YOLO gate likely fires these)
    "dog":               "dog",
    "domestic dog":      "dog",
    "house dog":         "dog",
    # Cat
    "cat":               "cat",
    "house cat":         "cat",
    "domestic cat":      "cat",
    "feral cat":         "cat",
    # Bear
    "bear":              "bear",
    "black bear":        "bear",
    "brown bear":        "bear",
    "grizzly":           "bear",
    "grizzly bear":      "bear",
    # Bird (kept loose — bird-species ID is out of scope)
    "bird":              "bird",
    # Horse / sheep / cow (YOLO's other classes)
    "horse":             "horse",
    "sheep":             "sheep",
    "cow":               "cow",
    # Other common North American wildlife maintainer might see
    "fisher":            "fisher",
    "fisher cat":        "fisher",
    "raccoon":           "raccoon",
    "deer":              "deer",
    "white-tailed deer": "deer",
    "coyote":            "coyote",  # noqa: F601 — identity, documents that "coyote" is a canonical form
    "opossum":           "opossum",
    "skunk":             "skunk",
    "striped skunk":     "skunk",
    "wild turkey":       "wild turkey",
    "turkey":            "wild turkey",
    "red fox":           "fox",      # noqa: F601 — canonicalizes the common Qwen variant to "fox"
}


def _normalize_species(species: str | None) -> str | None:
    """Map a free-form species string to its canonical bucket.

    Qwen returns free-form species names ("Eastern coyote", "red fox",
    "coy-wolf hybrid"). Enrolled animals have canonical species names
    ("coyote", "fox", "coydog"). Normalize both sides before
    comparing so the matcher's species hard-filter matches variants.

    Returns None if input is None. Returns the trimmed lowercased
    string when no normalization rule applies (so an unknown species
    like "kangaroo" still works as itself for matching, just won't
    match enrolled "coyote").
    """
    if species is None:
        return None
    s = species.strip().lower()
    if not s:
        return None
    return SPECIES_NORMALIZATION.get(s, s)


# ---------------------------------------------------------------------------
# Return types — mirror person_matcher's MatchVerdict / NoMatch
# ---------------------------------------------------------------------------


@dataclass
class AnimalMatchVerdict:
    """Match found — best candidate exceeds threshold.

    Mirrors infra.person_matcher.MatchVerdict (kept for sibling
    symmetry; downstream emit code can handle both dataclasses
    uniformly).
    """

    matched_name: str
    confidence: float
    matched_via: str  # "stable_attributes" (only path for animals)
    stable_attribute_scores: dict[str, float] = field(default_factory=dict)
    best_candidate_name: str | None = None
    best_candidate_confidence: float | None = None
    species_normalized: str | None = None
    species_confidence: str | None = None


@dataclass
class AnimalNoMatch:
    """No match — best candidate is below threshold (or other reason).

    Mirrors infra.person_matcher.NoMatch. The `reason` field tells
    downstream code + Telegram emit what to do:

    - "no_animal_in_frame"      — Qwen returned empty / no species
    - "no_known_animals"        — registry is empty
    - "species_filter_no_candidates" — Qwen's species has no enrolled match
    - "unknown_species"         — species=null; can't score without species
    - "score_below_threshold"   — best candidate exists but is below
                                   threshold (still surfaced for audit)

    For all NoMatch outcomes where we have a best_candidate, that
    candidate is surfaced so audit logs + future §11.86.5 enrollment
    can use it.
    """

    reason: str
    suppress: bool = False  # True for "no_animal_in_frame" / "unknown_species"
    best_candidate_name: str | None = None
    best_candidate_confidence: float | None = None
    best_candidate_scores: dict[str, float] | None = None
    species_normalized: str | None = None
    species_confidence: str | None = None


# ---------------------------------------------------------------------------
# Body-build similarity (lean | stocky | athletic | compact)
# ---------------------------------------------------------------------------

# Maps build enum values to a coarse "weight class" so neighbors get
# partial credit. A lean coyote and an athletic coyote look similar;
# lean vs stocky is the meaningful axis.
_BODY_BUILD_NEIGHBORS: dict[str, frozenset[str]] = {
    "lean":     frozenset({"lean", "athletic"}),
    "athletic": frozenset({"lean", "athletic", "compact"}),
    "compact":  frozenset({"compact", "athletic"}),
    "stocky":   frozenset({"stocky"}),
}


def _body_build_similarity(b1: str | None, b2: str | None) -> float:
    """Score body_build enum similarity.

    Exact match = 1.0. Neighbor credit (e.g. lean ↔ athletic) = 0.7.
    Otherwise 0.0. None on either side = 0.0 (conservative — don't
    reward unknown-to-unknown).
    """
    if b1 is None or b2 is None:
        return 0.0
    if b1 == b2:
        return 1.0
    if b1 in _BODY_BUILD_NEIGHBORS and b2 in _BODY_BUILD_NEIGHBORS.get(b1, set()):
        return 0.7
    return 0.0


# ---------------------------------------------------------------------------
# Coat pattern similarity (solid | bi-color | tri-color | tabby | striped | spotted)
# ---------------------------------------------------------------------------


def _coat_pattern_similarity(p1: str | None, p2: str | None) -> float:
    """Score coat_pattern enum similarity.

    Exact match = 1.0. None on either side = 0.0. No neighbor credit —
    patterns are distinct (a tabby is not a spotted; a striped is not
    a bi-color).
    """
    if p1 is None or p2 is None:
        return 0.0
    return 1.0 if p1 == p2 else 0.0


# ---------------------------------------------------------------------------
# Distinctive features — set Jaccard
# ---------------------------------------------------------------------------

# Stopwords to ignore when tokenizing feature strings. Helps "blue
# collar" and "the collar" overlap meaningfully.
_FEATURE_STOPWORDS = frozenset({
    "the", "a", "an", "is", "on", "in", "with", "and", "or",
    "of", "to", "no", "yes",
})


def _feature_tokens(s: str) -> frozenset[str]:
    """Lowercase + split a feature string, dropping stopwords.

    Punctuation handling:
      - Interior hyphens are treated as word boundaries so compound
        adjectives split: "white-tipped" → {white, tipped}.
      - Edge punctuation (commas, periods, etc.) is stripped.
      - Apostrophes inside words (e.g. "wolf's") keep the trailing
        "s" — useful for "wolf's paw print" vs "wolf paw print".
    """
    tokens = []
    # Replace hyphens with spaces BEFORE splitting so compounds split.
    for t in s.lower().replace("-", " ").split():
        # Strip edge punctuation (commas, periods, colons, quotes,
        # brackets, exclamation, question marks). No '-' here because
        # we already replaced it above.
        clean = t.strip(".,;:'\"!?()[]")
        if not clean or clean in _FEATURE_STOPWORDS:
            continue
        tokens.append(clean)
    return frozenset(tokens)


def _features_similarity(
    f1: list[str] | None,
    f2: list[str] | None,
) -> float:
    """Score distinctive_features arrays via token-set Jaccard.

    Returns |f1 ∩ f2| / |f1 ∪ f2|, computed on tokenized lowercase
    strings. None or empty on either side = 0.0 (conservative —
    missing features aren't "match by default").

    Examples:
      ["left ear notched", "limp"] vs ["left ear notched", "blue collar"]
        → tokens {left, ear, notched, limp} ∩ {left, ear, notched, blue, collar}
        → {left, ear, notched} / {left, ear, notched, limp, blue, collar}
        → 3/6 = 0.50
    """
    if not f1 or not f2:
        return 0.0
    s1 = set()
    for feature in f1:
        s1 |= _feature_tokens(feature)
    s2 = set()
    for feature in f2:
        s2 |= _feature_tokens(feature)
    if not s1 and not s2:
        return 0.0
    union = s1 | s2
    if not union:
        return 0.0
    intersection = s1 & s2
    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# Face details (nested dict: ear_shape, tail_carriage, mask)
# ---------------------------------------------------------------------------

# Neighbor credit for face_details components — ear_shape and
# tail_carriage can have close variants (pointed ↔ tufted on wild
# canids). mask is exact (yes/no — has a facial mask pattern or not).
_FACE_DETAIL_NEIGHBORS: dict[str, dict[str, frozenset[str]]] = {
    "ear_shape": {
        "pointed": frozenset({"pointed", "tufted"}),
        "tufted":  frozenset({"pointed", "tufted"}),
        "rounded": frozenset({"rounded"}),
        "floppy":  frozenset({"floppy"}),
    },
    "tail_carriage": {
        "high":   frozenset({"high"}),
        "low":    frozenset({"low"}),
        "curled": frozenset({"curled", "level"}),
        "level":  frozenset({"level", "curled"}),
    },
}


def _face_details_similarity(
    fd1: dict[str, Any] | None,
    fd2: dict[str, Any] | None,
) -> float:
    """Score face_details dict similarity across three components.

    Returns the average of component scores (ear_shape, tail_carriage,
    mask). Each component is scored like an enum: exact = 1.0,
    neighbor = 0.7 (where defined), missing = 0.0.

    None or empty on either side = 0.0. The average is taken over
    the components that are present in BOTH dicts (so a half-empty
    face_details doesn't tank the score).
    """
    if not fd1 or not fd2:
        return 0.0

    scores: list[float] = []
    for component in ("ear_shape", "tail_carriage", "mask"):
        v1 = fd1.get(component)
        v2 = fd2.get(component)
        if v1 is None or v2 is None:
            # Both missing is "no info"; one missing means we can't
            # score — neither side gets credit.
            continue
        if v1 == v2:
            scores.append(1.0)
            continue
        neighbors = _FACE_DETAIL_NEIGHBORS.get(component, {})
        if v1 in neighbors and v2 in neighbors.get(v1, set()):
            scores.append(0.7)
        # else: 0.0 — don't append, it would dilute the average.

    if not scores:
        return 0.0
    return sum(scores) / len(scores)


# ---------------------------------------------------------------------------
# Estimated age / sex_signal — exact enum match
# ---------------------------------------------------------------------------


def _enum_exact(v1: str | None, v2: str | None) -> float:
    """Exact-match helper for closed enums (estimated_age, sex_signal).

    None on either side = 0.0 (conservative). Match = 1.0. No
    neighbor credit — these are categorical.
    """
    if v1 is None or v2 is None:
        return 0.0
    return 1.0 if v1 == v2 else 0.0


# ---------------------------------------------------------------------------
# Body size — trinary with neighbor credit (kept from §11.86.2 first-draft)
# ---------------------------------------------------------------------------

_SIZE_BUCKETS = ("small", "medium", "large")
_SIZE_NEIGHBORS = {
    "small":  frozenset({"small", "medium"}),
    "medium": frozenset({"small", "medium", "large"}),
    "large":  frozenset({"medium", "large"}),
}


def _body_size_similarity(s1: str | None, s2: str | None) -> float:
    """Score body_size trinary similarity.

    Exact match = 1.0. Adjacent buckets (small↔medium, medium↔large)
    = 0.5. Non-adjacent = 0.0. None on either side = 0.0.
    """
    if s1 is None or s2 is None:
        return 0.0
    if s1 == s2:
        return 1.0
    if s1 in _SIZE_NEIGHBORS and s2 in _SIZE_NEIGHBORS.get(s1, set()):
        return 0.5
    return 0.0


# ---------------------------------------------------------------------------
# Score one known_animal entry against vision_result
# ---------------------------------------------------------------------------


def _score_known(
    vision_result: dict[str, Any],
    known: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    """Compute the weighted-ensemble score for one known entry.

    Returns (total_score, per_bucket_scores). The total is in [0, 1]
    IF every bucket had a value. With missing values the total can
    fall below 1.0 — that's the desired conservative behavior.

    IMPORTANT: this function does NOT apply the species hard filter.
    The caller does that. This function assumes the species already
    matches (after normalization).
    """
    # Align detected vs enrolled attrs. Enrolled may use `markings` as
    # a string (legacy from the first-draft schema) or
    # `distinctive_features` as an array (current wider-scope schema).
    # Support both during the §11.86.5 transition.

    detected_features = (
        vision_result.get("distinctive_features")
        or []
    )
    known_features = (
        known.get("distinctive_features")
        or known.get("markings")  # legacy field
        or []
    )
    # If `markings` is a string (legacy), wrap it for the array helper.
    if isinstance(known_features, str):
        known_features = [known_features]

    detected_face = vision_result.get("face_details") or {}
    known_face = known.get("face_details") or {}

    per_bucket: dict[str, float] = {
        "distinctive_features": _features_similarity(
            detected_features, known_features
        ),
        "coat_primary_color": _color_similarity(
            _normalize_color(vision_result.get("coat_primary_color")),
            _normalize_color(known.get("coat_primary_color")),
        ),
        "body_size": _body_size_similarity(
            vision_result.get("body_size"),
            known.get("body_size"),
        ),
        "body_build": _body_build_similarity(
            vision_result.get("body_build"),
            known.get("body_build"),
        ),
        "face_details": _face_details_similarity(
            detected_face, known_face
        ),
        "coat_pattern": _coat_pattern_similarity(
            vision_result.get("coat_pattern"),
            known.get("coat_pattern"),
        ),
        "estimated_age": _enum_exact(
            vision_result.get("estimated_age"),
            known.get("estimated_age"),
        ),
        "sex_signal": _enum_exact(
            vision_result.get("sex_signal"),
            known.get("sex_signal"),
        ),
    }

    total = sum(WEIGHTS[k] * per_bucket[k] for k in WEIGHTS)
    return total, per_bucket


# ---------------------------------------------------------------------------
# Public API — match_animal
# ---------------------------------------------------------------------------


def match_animal(
    vision_result: dict[str, Any],
    known_animals: list[dict[str, Any]],
    species_authoritative: bool = True,
    raise_threshold_on_unsure: bool = True,
) -> AnimalMatchVerdict | AnimalNoMatch:
    """Match detected animal against enrolled registry.

    Parameters
    ----------
    vision_result : dict
        Wider-scope schema dict (see module docstring). Required:
        `species` (or the function suppresses with
        "no_animal_in_frame").
    known_animals : list[dict]
        Enrolled animals. Each entry is a registry record. Required:
        `species`, `name` (or `id`).
    species_authoritative : bool, default True
        When True (production default), species hard-filters
        candidates: only enrolled animals with the same normalized
        species can score. When False, all enrolled animals score
        regardless of species (escape hatch for testing the matcher
        before the species filter is trusted).
    raise_threshold_on_unsure : bool, default True
        When True (production default), threshold is raised from
        0.55 to 0.65 if Qwen reports species_confidence="unsure".

    Returns
    -------
    AnimalMatchVerdict if the best known_animal exceeds the
    threshold; AnimalNoMatch otherwise. NoMatch always carries the
    best candidate (when one exists) for audit + future enrollment.

    Decision tree:
      1. vision_result has no species → NoMatch("no_animal_in_frame", suppress=True)
      2. known_animals empty → NoMatch("no_known_animals")
      3. After species filter, no candidates → NoMatch("species_filter_no_candidates")
      4. Qwen says species_confidence="unsure" AND no candidate scores
         above the raised threshold → NoMatch("score_below_threshold")
      5. Best candidate above threshold → AnimalMatchVerdict
      6. Else → NoMatch("score_below_threshold") with best_candidate
    """
    # Gate 1 — no species at all.
    species_raw = vision_result.get("species")
    species_norm = _normalize_species(species_raw)
    if species_norm is None:
        return AnimalNoMatch(
            reason="no_animal_in_frame",
            suppress=True,
            best_candidate_name=None,
            best_candidate_confidence=None,
            best_candidate_scores=None,
            species_normalized=None,
            species_confidence=vision_result.get("species_confidence"),
        )

    # Gate 2 — empty registry.
    if not known_animals:
        return AnimalNoMatch(
            reason="no_known_animals",
            suppress=False,
            best_candidate_name=None,
            best_candidate_confidence=None,
            best_candidate_scores=None,
            species_normalized=species_norm,
            species_confidence=vision_result.get("species_confidence"),
        )

    # Gate 3 — species hard filter.
    if species_authoritative:
        candidates = [
            k for k in known_animals
            if _normalize_species(k.get("species")) == species_norm
        ]
        if not candidates:
            return AnimalNoMatch(
                reason="species_filter_no_candidates",
                suppress=False,
                best_candidate_name=None,
                best_candidate_confidence=None,
                best_candidate_scores=None,
                species_normalized=species_norm,
                species_confidence=vision_result.get("species_confidence"),
            )
    else:
        candidates = list(known_animals)

    # Score every candidate. Take the highest.
    best_score = -1.0
    best_name: str | None = None
    best_scores: dict[str, float] | None = None
    for k in candidates:
        score, per_bucket = _score_known(vision_result, k)
        if score > best_score:
            best_score = score
            best_name = k.get("name") or k.get("id")
            best_scores = per_bucket

    # Threshold (may be raised on unsure).
    species_confidence = vision_result.get("species_confidence")
    threshold = ANIMAL_MATCH_THRESHOLD
    if (
        raise_threshold_on_unsure
        and species_confidence == "unsure"
    ):
        threshold = ANIMAL_MATCH_THRESHOLD_UNSURE

    if best_name is None or best_score < threshold:
        return AnimalNoMatch(
            reason="score_below_threshold",
            suppress=False,
            best_candidate_name=best_name,
            best_candidate_confidence=(
                None if best_name is None else round(best_score, 4)
            ),
            best_candidate_scores=best_scores,
            species_normalized=species_norm,
            species_confidence=species_confidence,
        )

    # Match found.
    return AnimalMatchVerdict(
        matched_name=best_name,
        confidence=round(best_score, 4),
        matched_via="stable_attributes",
        stable_attribute_scores=best_scores or {},
        best_candidate_name=best_name,
        best_candidate_confidence=round(best_score, 4),
        species_normalized=species_norm,
        species_confidence=species_confidence,
    )


__all__ = [
    "ANIMAL_MATCH_THRESHOLD",
    "ANIMAL_MATCH_THRESHOLD_UNSURE",
    "SPECIES_NORMALIZATION",
    "WEIGHTS",
    "AnimalMatchVerdict",
    "AnimalNoMatch",
    "_body_build_similarity",
    "_body_size_similarity",
    "_coat_pattern_similarity",
    "_enum_exact",
    "_face_details_similarity",
    "_feature_tokens",
    "_features_similarity",
    "_normalize_species",
    "match_animal",
]