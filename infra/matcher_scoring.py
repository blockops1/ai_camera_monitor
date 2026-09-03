"""
matcher_scoring.py — Per-dimension scoring engine for the vehicle matcher.

STATUS: stable (extracted from infra/vehicle_matcher.py 2026-08-13 as
    part of the Q1 module-purity split — vehicle_matcher's 5-job
    violation decomposed into matcher_spec + matcher_scoring +
    vehicle_matcher)
THREAD SAFETY: pure functions on dict inputs; the score_vehicle()
    orchestrator threads through a spec dict but is itself stateless
    (callers must serialize if they share mutable state externally —
    production callers in infra.vehicle_matcher don't mutate spec)

INPUTS:
    - function arg sig: dict (Qwen-Vision output normalized into the
      signature schema)
    - function arg kv: dict (one entry from data/vehicles/known_vehicles.json)
    - function arg spec: dict (from infra.matcher_spec.load_spec() —
      the spec keys this module reads: type_groups, color_normalization,
      body_style_flex lists, model_aliases, distinctive_keywords)

OUTPUTS:
    - score_vehicle() -> (total_score: float, breakdown_dict: dict[str, float])
    - DIMENSION_FUNCTIONS — name->callable map used by score_vehicle

PUBLIC API:
    DEFAULT_DIMENSION_WEIGHTS — default per-dimension weights (Phase.29b)
    DIMENSION_FUNCTIONS — name->callable map (22 dimensions + aliases)
    score_vehicle(sig, kv, spec) -> (total_score, breakdown_dict)
    _normalize_color(color, spec) -> str | None — color normalization
    _build_type_to_group(spec) -> dict[str, str] — invert {group: [types]}
    _dim_<name>(sig, kv, spec) -> bool — individual dimension functions
      (exposed for testing; underscore-prefixed but accessible from
      infra.matcher_scoring namespace)

DOES NOT DO:
    - Load the spec — infra.matcher_spec owns DEFAULT_SPEC + load_spec
    - Rank candidates — infra.vehicle_matcher.match_vehicle_scored owns that
    - Decide whether the top candidate clears thresholds — also
      infra.vehicle_matcher.match_vehicle_scored
    - Format Telegram bodies — infra.telegram_formatter owns that

WHY HERE:
    The per-dimension evaluation rules belong in their own module —
    they're the engine core, independent of any orchestrator. Each
    _dim_* function is independently testable (input a sig+kv pair,
    check the bool). score_vehicle is the orchestrator loop that
    applies weights and aggregates the breakdown.

    Splitting the engine from the orchestrator lets us test, say,
    _dim_distinctive_keyword without spinning up the spec loader or
    the matchers' threshold gating logic.

CALLED BY:
    - infra.vehicle_matcher.match_vehicle_scored() — primary caller
    - infra.vehicle_matcher.score_top_n() — also calls score_vehicle
    - infra.matcher_scoring itself (DIMENSION_FUNCTIONS is the
      internal dispatch; lambdas in the map close over _dim_feature_match
      and _dim_bed_cover_match defined here)

CALLS INTO:
    - (no infra deps — pure stdlib)

RELATED:
    - infra.matcher_spec — owns the spec this module reads
    - infra.vehicle_matcher — owns the orchestrator that calls score_vehicle
    - PHASE6B29-PRD-scored-matcher.md — documents every weight threshold
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Type aliases (kept identical to infra.vehicle_matcher for binary compat)
Sig = dict[str, Any]
KnownVehicle = dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers — color normalization + type-group inversion
# ---------------------------------------------------------------------------


def _normalize_color(color: str | None, spec: dict[str, Any]) -> str | None:
    """Apply color normalization table. Returns the canonical color key.

    Matches both sig.color and kv.color against the same normalization
    table so that 'navy' and 'dark_blue' both produce 'blue', etc.
    """
    if not color:
        return None
    norm = (color or "").lower().strip()
    if not norm:
        return None
    # spec is loaded from JSON; cast to typed shape so .items() returns str→list[str]
    norm_map: dict[str, list[str]] = spec.get("color_normalization") or {}
    for canonical, variants in norm_map.items():
        if norm in [v.lower() for v in (variants or [])]:
            return canonical
    return None


def _build_type_to_group(spec: dict[str, Any]) -> dict[str, str]:
    """Invert {group: [types]} into {type: group} for fast lookup."""
    out: dict[str, str] = {}
    for group, types in (spec.get("type_groups") or {}).items():
        for t in types:
            out[t.lower()] = group
    return out


# ---------------------------------------------------------------------------
# Default dimension weights
# ---------------------------------------------------------------------------

# Sum of all "happy path" weights is ~22.5; realistic top scores land
# in the 2-8 range for confident matches.
DEFAULT_DIMENSION_WEIGHTS: dict[str, float] = {
    "color_match":          0.7,
    "color_alt_match":      0.5,
    "type_match":           0.8,
    "type_group_flex":      0.4,   # cross-group match (after 6B.29a split)
    "body_style_flex":      1.0,   # opt-in only (Tesla today)
    "make_match":           2.0,
    "make_in_label":        1.5,
    "model_match":          3.0,
    "model_aliases_match":  3.0,
    "model_in_label":       1.5,
    "wheel_match":          2.0,
    "wheel_arch_match":     1.5,
    "wheel_color_match":    1.0,
    "rear_lights_match":    1.0,
    "roofline_match":       1.0,
    "tailgate_match":       1.0,
    # Phase.48 (2026-08-01) — replaced 6B.46's hitch_match (0.5)
    # with three bigger, more permanent distinguishing features. Note
    # OOB rejected hitch_present as too small and too easily confused
    # (ball vs receiver vs rack). Tint/cab-markers/bed-cover are larger
    # in the frame, more permanent, and less likely to be mismatched.
    "window_tint_match":    1.0,
    "cab_marker_match":     1.0,
    "bed_cover_match":      1.0,
    "body_trim_match":      0.5,
    "distinctive_keyword":  0.5,
    # Phase.75 (2026-08-10) — negative scores for color mismatch.
    # Note 2026-08-10: *"the matcher tries to pick a blue truck,
    # that should be a large negative."* When vision says the vehicle
    # is one color and the candidate is a different color AND there is
    # no colors_alt overlap, actively penalize — don't just skip the
    # color dimension.
    #
    # Phase.84 (2026-08-16) — Note: *"if a blue truck is trying
    # to be matched to a white truck then the color mismatch should be
    # a big penalty."* Bumped -2.0 → -4.0 (5.7x the 0.7 credit of
    # color_match). A wrong-color match loses more than it gains on
    # any single positive dimension (model_match is 3.0, make_match
    # 2.0), so wrong-color comparisons reliably drop below the
    # confidence_threshold instead of squeaking past on incidental
    # stacking. color_alt_mismatch stays at -1.5 because colors_alt is
    # by definition a "this might also match" hint, so partial-credit
    # candidates deserve less penalty than primary-color mismatches.
    "color_mismatch":       -4.0,
    "color_alt_mismatch":   -1.5,
}


# ---------------------------------------------------------------------------
# Dimension functions — 22 evaluators returning bool
# ---------------------------------------------------------------------------


def _dim_color_match(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.color normalized == kv.color."""
    sig_c = _normalize_color(sig.get("color"), spec)
    kv_c = _normalize_color(kv.get("color"), spec)
    return bool(sig_c and kv_c and sig_c == kv_c)


def _dim_color_alt_match(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.color normalized is in kv.colors_alt."""
    sig_c = _normalize_color(sig.get("color"), spec)
    if not sig_c:
        return False
    for alt in kv.get("colors_alt") or []:
        if _normalize_color(alt, spec) == sig_c:
            return True
    return False


def _dim_color_mismatch(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """Phase.75: returns True when sig.color is known, kv.color is
    known, AND they DON'T normalize equal (a true mismatch). The
    negative weight in DEFAULT_DIMENSION_WEIGHTS turns this into a
    penalty. If either color is missing/unknown, returns False — we
    don't penalize for missing data.

    NOTE: This is independent of colors_alt. If sig.color is in
    kv.colors_alt (color_alt_match fires), this dim STILL fires if
    kv.color differs from sig.color — the alt-match credit covers
    that, but the primary color still doesn't match. In practice
    this would double-count, so this dim is gated to fire only when
    color_alt_match does NOT fire (see the guard at the bottom).
    """
    sig_c = _normalize_color(sig.get("color"), spec)
    kv_c = _normalize_color(kv.get("color"), spec)
    if not sig_c or not kv_c:
        return False
    if sig_c == kv_c:
        return False
    # If sig color matches one of kv's alts, color_alt_match will
    # credit the +0.5. Don't also penalize the primary-color mismatch
    # — having declared "I'm flexible to include gray" is enough.
    for alt in kv.get("colors_alt") or []:
        if _normalize_color(alt, spec) == sig_c:
            return False
    return True


def _dim_color_alt_mismatch(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """Phase.75: returns True when:
      - sig.color is known
      - kv has at least one colors_alt
      - kv.color does NOT match sig.color (otherwise color_match fires
        and we shouldn't double-penalize via colors_alt)
      - AND none of those alts normalize to sig_c

    The negative weight in DEFAULT_DIMENSION_WEIGHTS turns this into a
    penalty. If kv.color already matches sig.color, returns False —
    color_match's credit (+0.7) covers it, no need to also penalize the
    alts for not matching the primary color.
    """
    sig_c = _normalize_color(sig.get("color"), spec)
    kv_c = _normalize_color(kv.get("color"), spec)
    if not sig_c or not kv_c:
        return False
    if sig_c == kv_c:
        return False  # primary color matches → don't double-penalize
    alts = kv.get("colors_alt") or []
    if not alts:
        return False
    for alt in alts:
        if _normalize_color(alt, spec) == sig_c:
            return False  # sig matches an alt → not a mismatch
    # sig is known, kv.color differs, kv has alts, no alt matches → mismatch
    return True


def _dim_type_match(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.type == kv.type (exact)."""
    sig_t = (sig.get("type") or sig.get("body_style_hint") or "").lower().strip()
    kv_t = (kv.get("type") or "").lower().strip()
    return bool(sig_t and kv_t and sig_t == kv_t)


def _dim_type_group_flex(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.type and kv.type are in the same type_group (vehicle group only)."""
    sig_t = (sig.get("type") or sig.get("body_style_hint") or "").lower().strip()
    kv_t = (kv.get("type") or "").lower().strip()
    if not sig_t or not kv_t:
        return False
    t2g = _build_type_to_group(spec)
    sig_g = t2g.get(sig_t)
    kv_g = t2g.get(kv_t)
    # Cross-group flex only applies within the "vehicle" supergroup
    # (pickup, truck, van, suv — but suv is split out post-6B.29a, so
    # type_group_flex fires between pickup/truck/van only).
    return bool(sig_g and kv_g and sig_g == kv_g == "vehicle" and sig_t != kv_t)


def _dim_body_style_flex(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.type is in kv.body_style_aliases AND kv.body_style_flex: true."""
    if not kv.get("body_style_flex"):
        return False
    sig_t = (sig.get("type") or sig.get("body_style_hint") or "").lower().strip()
    if not sig_t:
        return False
    aliases = kv.get("body_style_aliases") or []
    if sig_t not in aliases:
        return False
    # Color must also match (primary or alt)
    sig_c = _normalize_color(sig.get("color"), spec)
    kv_c = _normalize_color(kv.get("color"), spec)
    if sig_c and kv_c and sig_c == kv_c:
        return True
    for alt in kv.get("colors_alt") or []:
        if _normalize_color(alt, spec) == sig_c:
            return True
    return False


def _dim_make_match(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.make.lower() == kv.make.lower()."""
    sig_m = (sig.get("make") or "").lower().strip()
    kv_m = (kv.get("make") or "").lower().strip()
    return bool(sig_m and kv_m and sig_m == kv_m)


def _dim_make_in_label(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.make.lower() in kv.label.lower()."""
    sig_m = (sig.get("make") or "").lower().strip()
    if not sig_m:
        return False
    label = (kv.get("label") or "").lower()
    return sig_m in label


def _dim_model_match(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.model bidirectional substring of kv.model (after normalization)."""
    sig_m = (sig.get("model") or "").lower().strip()
    kv_m = (kv.get("model") or "").lower().strip()
    if not sig_m or not kv_m:
        return False
    sig_n = sig_m.replace("-", "").replace(" ", "")
    kv_n = kv_m.replace("-", "").replace(" ", "")
    return sig_n in kv_n or kv_n in sig_n


def _dim_model_aliases_match(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.model bidirectional substring of any kv.model_aliases entry."""
    sig_m = (sig.get("model") or "").lower().strip()
    if not sig_m:
        return False
    sig_n = sig_m.replace("-", "").replace(" ", "")
    for alias in kv.get("model_aliases") or []:
        a = (alias or "").lower().strip()
        a_n = a.replace("-", "").replace(" ", "")
        if a_n and (a_n in sig_n or sig_n in a_n):
            return True
    return False


def _dim_model_in_label(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """sig.model in kv.label."""
    sig_m = (sig.get("model") or "").lower().strip()
    if not sig_m:
        return False
    label_n = (kv.get("label") or "").lower().replace("-", "").replace(" ", "")
    sig_n = sig_m.replace("-", "").replace(" ", "")
    return sig_n in label_n


def _dim_feature_match(sig: Sig, kv: KnownVehicle, feature_key: str) -> bool:
    """sig.<feature_key> == kv.vehicle_features[feature_key].

    Phase.47 (2026-08-01) — read from FLAT sig first (post-6B.46
    refactor of `_signature_for_vehicle`), fall back to nested
    `sig.vehicle_features` for backward compat with older sigs or
    direct callers.

    Phase.84 (2026-08-16) — absence-evidence fallacy fix (d99a38e6).
    When both sig and kv have falsy/default values (False, None, "",
    "none"), this dimension does NOT fire. Sharing a default is not
    signal — it's the absence of evidence on both sides. Before 6B.84,
    cab_marker_lights=False + bed_cover=none on sig and kv returned
    True (matching False == False), giving every comparison a bogus
    +1.0 +1.0 = +2.0 credit. Now: positive presence required on at
    least one side AND values match.
    """
    # Flat top-level (post-6B.46): sig["wheel_style"] = ...
    sig_v_flat = sig.get(feature_key)
    if sig_v_flat is not None:
        kv_vf = kv.get("vehicle_features") or {}
        kv_v = kv_vf.get(feature_key)
        if sig_v_flat is None or kv_v is None:
            return False
        if not _feature_present(sig_v_flat) or not _feature_present(kv_v):
            return False
        return str(sig_v_flat).lower() == str(kv_v).lower()

    # Nested fallback: sig["vehicle_features"]["wheel_style"] = ...
    sig_vf = sig.get("vehicle_features") or {}
    kv_vf = kv.get("vehicle_features") or {}
    if not isinstance(sig_vf, dict) or not isinstance(kv_vf, dict):
        return False
    sig_v = sig_vf.get(feature_key)
    kv_v = kv_vf.get(feature_key)
    if sig_v is None or kv_v is None:
        return False
    if not _feature_present(sig_v) or not _feature_present(kv_v):
        return False
    return str(sig_v).lower() == str(kv_v).lower()


def _feature_present(val: object) -> bool:
    """Phase.84 (2026-08-16) — does this value count as positive
    evidence for a feature dimension?

    Returns False for falsy defaults that Qwen emits when it doesn't
    see something:
        - None (missing entirely)
        - False (boolean field absent)
        - "" (empty string)
        - "none" / "null" / "false" (string equivalents of absent)

    Returns True for any other value. "5-spoke", "black steel",
    "exposed", True, "chrome" all qualify.
    """
    if val is None:
        return False
    if val is False:  # explicit False, not just falsy
        return False
    s = str(val).strip().lower()
    if not s:
        return False
    return s not in {"none", "null", "false", "unknown"}


# Phase.48 — bed_cover alias handling. Qwen says "topper" or "camper_shell"
# for the same high-profile fiberglass shell; both should match a kv with
# either value.
_BED_COVER_ALIASES = {"topper": "camper_shell", "camper_shell": "camper_shell"}


def _normalize_bed_cover(val: object) -> str | None:
    """Normalize bed_cover values to canonical form.

    Phase.84 (2026-08-16) — also returns None for absence signals
    ('none', 'null', 'false', '', empty). Without this, sig.bed_cover
    ='none' would normalize to 'none' and match kv.bed_cover='none',
    firing bed_cover_match with +1.0 against a known vehicle that
    also says 'none' — the d99a38e6 absence-evidence pattern.
    """
    if val is None:
        return None
    if val is False:
        return None
    s = str(val).strip().lower()
    if not s or s in {"none", "null", "false", "unknown"}:
        return None
    return _BED_COVER_ALIASES.get(s, s)


def _dim_bed_cover_match(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """bed_cover match with topper == camper_shell alias.

    Phase.84 (2026-08-16) — gates on _feature_present so both
    sides having 'none' / 'null' / False returns False (absence-
    evidence, not signal). Legacy 6B.48 alias handling preserved.
    """
    sig_v = _normalize_bed_cover(sig.get("bed_cover"))
    if sig_v is None:
        # Fall back to nested sig.vehicle_features (6B.47 backward compat)
        sig_vf = sig.get("vehicle_features") or {}
        sig_v = _normalize_bed_cover(sig_vf.get("bed_cover") if isinstance(sig_vf, dict) else None)
    if sig_v is None:
        return False
    kv_vf = kv.get("vehicle_features") or {}
    kv_v = _normalize_bed_cover(kv_vf.get("bed_cover") if isinstance(kv_vf, dict) else None)
    if kv_v is None:
        return False
    return sig_v == kv_v


_STRIP_CHARS = ".,;()[]{}" + chr(34) + chr(39)


def _dim_distinctive_keyword(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]) -> bool:
    """At least one distinctive_features keyword matches sig evidence.

    Heuristic: split each distinctive_features string into keywords
    (words >= 4 chars). If any keyword also appears in sig.vehicle_features
    values OR sig.notes OR sig.scene_description, return True.
    """
    sig_text_parts: list[Any] = []
    sig_vf = sig.get("vehicle_features") or {}
    if isinstance(sig_vf, dict):
        sig_text_parts.extend(str(v) for v in sig_vf.values() if v)
    if sig.get("notes"):
        sig_text_parts.append(str(sig["notes"]))
    if sig.get("scene_description"):
        sig_text_parts.append(str(sig["scene_description"]))
    sig_text = " ".join(sig_text_parts).lower()

    if not sig_text:
        return False

    for feature in kv.get("distinctive_features") or []:
        if not isinstance(feature, str):
            continue
        # Extract content words >= 4 chars (skip "the", "and", etc.)
        words = [w.lower().strip(_STRIP_CHARS) for w in feature.split()]
        words = [w for w in words if len(w) >= 4]
        for w in words:
            if w in sig_text:
                return True
    return False


# ---------------------------------------------------------------------------
# Dispatch table — dimension name -> callable
# ---------------------------------------------------------------------------


# Map dimension name -> callable. Each takes (sig, kv, spec) and returns bool.
DIMENSION_FUNCTIONS: dict[str, Any] = {
    "color_match":         _dim_color_match,
    "color_alt_match":     _dim_color_alt_match,
    # Phase.75 (2026-08-10) — negative-color dimensions
    "color_mismatch":      _dim_color_mismatch,
    "color_alt_mismatch":  _dim_color_alt_mismatch,
    "type_match":          _dim_type_match,
    "type_group_flex":     _dim_type_group_flex,
    "body_style_flex":     _dim_body_style_flex,
    "make_match":          _dim_make_match,
    "make_in_label":       _dim_make_in_label,
    "model_match":         _dim_model_match,
    "model_aliases_match": _dim_model_aliases_match,
    "model_in_label":      _dim_model_in_label,
    "wheel_match":         lambda s, k, spec: _dim_feature_match(s, k, "wheel_style"),
    "wheel_arch_match":    lambda s, k, spec: _dim_feature_match(s, k, "wheel_arch"),
    "wheel_color_match":   lambda s, k, spec: _dim_feature_match(s, k, "wheel_color"),
    "rear_lights_match":   lambda s, k, spec: _dim_feature_match(s, k, "rear_lights_signature"),
    "roofline_match":      lambda s, k, spec: _dim_feature_match(s, k, "roofline_style"),
    "tailgate_match":      lambda s, k, spec: _dim_feature_match(s, k, "tailgate_type"),
    # Phase.48 — replacing 6B.46's hitch_match.
    "window_tint_match":   lambda s, k, spec: _dim_feature_match(s, k, "window_tint"),
    "cab_marker_match":    lambda s, k, spec: _dim_feature_match(s, k, "cab_marker_lights"),
    # bed_cover uses a custom matcher that treats topper == camper_shell
    # (Qwen uses these interchangeably per PRD).
    "bed_cover_match":     lambda s, k, spec: _dim_bed_cover_match(s, k, spec),
    "body_trim_match":     lambda s, k, spec: _dim_feature_match(s, k, "body_trim"),
    "distinctive_keyword": _dim_distinctive_keyword,
}


# ---------------------------------------------------------------------------
# Orchestrator — score one (sig, kv) pair
# ---------------------------------------------------------------------------


def score_vehicle(sig: Sig, kv: KnownVehicle, spec: dict[str, Any]
                  ) -> tuple[float, dict[str, float]]:
    """Score one (sig, kv) pair across every dimension.

    Returns (total_score, breakdown_dict). The breakdown_dict maps each
    dimension name to its score contribution (weight if matched, 0 if not).

    Weights come from spec.get("dimension_weights", DEFAULT_DIMENSION_WEIGHTS).
    """
    weights = spec.get("dimension_weights", DEFAULT_DIMENSION_WEIGHTS)
    breakdown: dict[str, float] = {}
    total = 0.0
    for dim_name, dim_fn in DIMENSION_FUNCTIONS.items():
        weight = weights.get(dim_name, 0.0)
        # Phase.75 (2026-08-10) — changed from `if weight <= 0` to
        # `if weight == 0` so negative-weight dimensions (color_mismatch,
        # color_alt_mismatch) are evaluated and contribute a penalty.
        # Weight 0 means "dimension not configured" or "explicitly
        # disabled in spec" — skip. Negative means "evaluate and
        # subtract on match."
        if weight == 0:
            continue
        try:
            matched = bool(dim_fn(sig, kv, spec))
        except Exception as exc:
            # Per-dimension failures are expected during rollout (some
            # dimensions reference optional fields that may be missing);
            # keep these quiet to avoid log spam. The match continues
            # with that dimension treated as a non-match (weight 0).
            log.debug("dim_eval_failed dim=%s err=%r", dim_name, exc)
            matched = False
        if matched:
            breakdown[dim_name] = weight
            total += weight

    return total, breakdown


__all__ = [
    "DEFAULT_DIMENSION_WEIGHTS",
    "DIMENSION_FUNCTIONS",
    "KnownVehicle",
    "Sig",
    "score_vehicle",
]
