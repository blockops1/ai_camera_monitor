"""
vehicle_matcher.py — Vehicle matcher orchestrator (the production entry points).

STATUS: stable (post-module-purity-split 2026-08-13; production API is
    match_vehicle_scored + match_with_details + score_top_n + MatchDetail.
    The Q1 split extracted infra.matcher_spec (spec data + loader) and
    infra.matcher_scoring (per-dimension evaluation engine) into their
    own modules; this file is now the orchestrator-only entry.
    See Part 9 §5 of PLAN.md for the resolution.
THREAD SAFETY: thread-safe for read-only spec interpretation (spec
    is loaded lazily and cached via load_spec; counters in the dead
    `_shadow_*` globals use threading.Lock but are never invoked
    in production after Phase.29d cutover)

INPUTS:
    - function arg sig: dict[str, Any] (Qwen-Vision output normalized
      into the signature schema)
    - function arg known: list[dict[str, Any]] (loaded from
      data/vehicles/known_vehicles.json)
    - function arg spec: dict | None — if None, load_spec() called
      inside the orchestrator on every entry point
    - file data/vehicle_matcher_spec.yaml (optional, falls back to
      embedded DEFAULT_SPEC in infra.matcher_spec if absent)

OUTPUTS:
    - match_vehicle_scored -> tuple (kv, score, gap, all_breakdowns) | None
    - score_top_n -> list of (kv, score, breakdown) tuples
    - match_with_details -> MatchDetail | None
    - MatchDetail — frozen-ish dataclass with kv, score, gap, reasons,
      matched_dim_weights

PUBLIC API:
    DEFAULT_SPEC — re-exported from infra.matcher_spec (import compatibility)
    load_spec — re-exported from infra.matcher_spec (import compatibility)
    DEFAULT_DIMENSION_WEIGHTS — re-exported from infra.matcher_scoring
        (import compatibility for callers that read weights directly)
    score_vehicle(sig, kv, spec) -> (total_score, breakdown_dict)
        Re-exported from infra.matcher_scoring
    DIMENSION_FUNCTIONS — re-exported from infra.matcher_scoring
    match_vehicle_scored(sig, known, spec=None)
        -> (kv, score, gap, breakdowns) | None — the production match entry
    score_top_n(sig, known, n=3, spec=None) -> list[(kv, score, breakdown)]
    MatchDetail — dataclass
    match_with_details(sig, known) -> MatchDetail | None
        Returns MatchDetail for telemetry + Telegram-body consumption.

KNOWN VIOLATIONS (see PLAN.md Part 9 §5):
    This module still owns the Phase.26a serial-gate interpreter
    (`_match_with_spec`, all `_pass_*` / `_score_*` helpers, `_build_type_to_group`'s
    spec-interpreter callers, `_normalize_color` legacy user). The
    interpreter was the production matcher before 6B.29d cut over to
    `match_vehicle_scored`. It is dead code in production but kept
    here as a rollback target — removing it cleanly is a separate
    cleanup pass (next module-purity pass). Until then, ignore
    everything below the orchestrator section starting at line ~330.

    This module also keeps `compare_with_legacy()` and the `_shadow_*`
    counters for vet-mode rollback safety. Same status — dead code
    that ships unused but is documented in case of cutover reversal.

DOES NOT DO:
    - Capture frames — infra.frame_capture owns that
      (motion detection lives in listener.motion_gate_pipeline;
       dataclasses live in infra.motion_types since Phase §11.90)
    - Decide what to alert — infra.alert_generator owns that
    - Send Telegram — infra.notifier owns that
    - Persist shadow counters — infra.matcher_telemetry owns that
    - Own spec data — infra.matcher_spec owns DEFAULT_SPEC + load_spec
    - Own per-dimension evaluation — infra.matcher_scoring owns those

WHY HERE:
    This module owns the production match orchestration after the
    6B.29d cutover. match_vehicle_scored + MatchDetail + score_top_n
    is the contract the listener consumes; everything else in the
    module is dead code pending cleanup.

CALLED BY:
    - listener.listener: match_vehicle_scored() in reclassification block
    - listener.listener: match_with_details() in the shadow-validation block
    - listener.listener: score_top_n() in the no_match_vehicles path
    - infra.matcher_telemetry: score_top_n() for shadow-mode comparisons
        (imports from vehicle_matcher per pre-split migration)

CALLS INTO:
    - infra.matcher_spec.load_spec() — when no spec provided by caller
    - infra.matcher_scoring.score_vehicle() — the per-(sig, kv)
      scoring loop
    - infra.matcher_scoring.DIMENSION_FUNCTIONS — re-exported for
      reverse compat with callers that import them from here

RELATED:
    - infra.matcher_spec — spec data + loader
    - infra.matcher_scoring — per-dimension engine
    - infra.known_vehicles (vehicle_identifier/known_vehicles.py) —
      the data source for `known` lists
    - data/vehicle_matcher_spec.yaml — the matching rules (optional)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, cast

log = logging.getLogger(__name__)

# Public re-exports — preserved verbatim from pre-split behavior so
# existing callers can `from infra.vehicle_matcher import X` and still
# work. See PLAN Part 9 §5 for the rationale. The imports are listed
# in __all__ for backwards compat; ruff sees them as "unused" because
# the production functions don't reference them locally. Per-file
# RUF100 noqa in pyproject.toml handles the "unused noqa" warning.
from infra.matcher_scoring import (  # noqa: F401
    DEFAULT_DIMENSION_WEIGHTS,
    DIMENSION_FUNCTIONS,
    score_vehicle,
)
from infra.matcher_spec import DEFAULT_SPEC, load_spec  # noqa: F401

# Type aliases
Sig = dict[str, Any]
KnownVehicle = dict[str, Any]


# ============================================================================
# Production matchers — the post-6B.29d orchestration contract
# ============================================================================


def match_vehicle_scored(sig: Sig, known: list[KnownVehicle], spec: dict[str, Any] | None = None
                          ) -> tuple[KnownVehicle, float, float, dict[str, dict[str, float]]] | None:
    """Scored-combination matcher. Returns the top-scoring vehicle if it
    clears confidence_threshold AND gap_threshold. Else returns None.

    Returns:
        None if no match, else (matched_kv, top_score, top_gap, all_breakdowns)
        where all_breakdowns maps kv.id -> {dim_name: score, ...}

    The 'top_gap' is (top_score - second_best_score). If only one kv has
    any score, top_gap = top_score (gap_threshold is trivially satisfied).
    """
    if spec is None:
        spec = load_spec()

    thresholds = spec.get("thresholds", {"confidence": 0.6, "gap": 0.15})
    confidence_threshold = float(thresholds.get("confidence", 0.6))
    gap_threshold = float(thresholds.get("gap", 0.15))

    # Score every kv
    scores: dict[str, float] = {}
    breakdowns: dict[str, dict[str, float]] = {}
    for kv in known:
        kv_id = kv.get("id", "<unknown>")
        s, b = score_vehicle(sig, kv, spec)
        scores[kv_id] = s
        breakdowns[kv_id] = b

    # Rank by score (descending)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    if not ranked or ranked[0][1] <= 0:
        return None

    top_id, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    gap = top_score - second_score

    # Threshold gating
    if top_score < confidence_threshold:
        return None
    if gap < gap_threshold:
        return None

    # Find the matched kv
    matched_kv = next((kv for kv in known if kv.get("id") == top_id), None)
    if matched_kv is None:
        return None

    return (matched_kv, top_score, gap, breakdowns)


def score_top_n(
    sig: Sig,
    known: list[KnownVehicle],
    n: int = 3,
    spec: dict[str, Any] | None = None,
) -> list[tuple[KnownVehicle, float, dict[str, float]]]:
    """Phase.77 (2026-08-11) — score every kv and return the top N.

    Used by the no-match Telegram path so the user sees the closest
    candidates with their per-dimension breakdowns. Unlike
    match_vehicle_scored, this does NOT apply the threshold gate — it
    always returns the top N regardless of whether anything cleared
    the spec's confidence/gap thresholds.

    Returns a list of (kv, score, breakdown) tuples, length <= min(n,
    len(known)). Sorted by score descending. Vehicles with score 0
    are still included if they fit in the top N (user can see "this
    signature has zero overlap with v_X").

    Caller is responsible for surfacing this in whatever shape the
    Telegram body expects (e.g. truncated to 3 entries to keep the
    message readable).
    """
    if spec is None:
        spec = load_spec()

    scored: list[tuple[KnownVehicle, float, dict[str, float]]] = []
    for kv in known:
        s, b = score_vehicle(sig, kv, spec)
        scored.append((kv, s, b))

    # Sort by score descending; tiebreak on kv["id"] for stable output
    scored.sort(key=lambda t: (-t[1], t[0].get("id", "")))
    return scored[:n]


# ---------------------------------------------------------------------------
# MatchDetail — the matcher output that the 2nd-Telegram match alert consumes
# ---------------------------------------------------------------------------


@dataclass
class MatchDetail:
    """Result of a single successful match.

    Returned by `match_with_details()` (and consumed by the listener's
    _send_match_alert). Fields:
        kv:                  the matched known_vehicle entry (full dict)
        score:               total scored-match score
        gap:                 score gap to runner-up (or score if only one)
        reasons:             short labels for the top 3 dimensions that fired
        matched_dim_weights: raw per-dimension weight breakdown {dim_name: weight}
    """
    kv: KnownVehicle
    score: float
    gap: float
    reasons: list[str] = field(default_factory=list)
    matched_dim_weights: dict[str, float] = field(default_factory=dict)


# Reason labels for the Telegram — short, human-readable. Order matches
# the order DIMENSION_FUNCTIONS is defined in infra.matcher_scoring.
_MATCH_REASON_LABELS = {
    "color_match":            "color matches",
    "color_alt_match":        "color in colors_alt",
    "type_match":             "body type matches",
    "type_group_flex":        "body type matches (group flex)",
    "body_style_flex":        "body style matches (alias + flex)",
    "make_match":             "make matches",
    "make_in_label":          "make in label",
    "model_match":            "model substring matches",
    "model_aliases_match":    "model alias substring matches",
    "model_in_label":         "model in label",
    "wheel_match":            "wheel style matches",
    "wheel_arch_match":       "wheel arches match",
    "wheel_color_match":      "wheel color matches",
    "rear_lights_match":      "rear light signature matches",
    "roofline_match":         "roofline matches",
    "tailgate_match":         "tailgate type matches",
    "window_tint_match":      "window tint matches",
    "cab_marker_match":       "cab marker lights match",
    "bed_cover_match":        "bed cover matches",
    "body_trim_match":        "body trim matches",
    "distinctive_keyword":    "distinctive keyword matches",
}


def _rank_reasons(dim_weights: dict[str, float]) -> list[str]:
    """Convert dimension breakdown into 1-3 most-influential reasons.

    Picks the top 3 dimensions by weight (descending), maps each to a
    short label via _MATCH_REASON_LABELS, returns them in priority
    order. Unrecognized dimensions get their raw name as fallback.
    """
    if not dim_weights:
        return []
    ordered = sorted(dim_weights.items(), key=lambda kv: -kv[1])
    labels: list[str] = []
    for dim_name, _ in ordered[:3]:
        labels.append(_MATCH_REASON_LABELS.get(dim_name, dim_name))
    return labels


def match_with_details(
    sig: Sig, known: list[KnownVehicle], spec: dict[str, Any] | None = None,
) -> MatchDetail | None:
    """Wrap match_vehicle_scored with ranked reasons for telemetry.

    Returns a MatchDetail whose `reasons` are the human-readable labels
    for the top 3 dimensions that contributed weight. The label list
    surfaces in the 2nd-Telegram "matched alert" body so the operator
    can see why this candidate was chosen.

    Returns None when the scored-matcher returns None (below threshold,
    gap too small, or empty input).
    """
    result = match_vehicle_scored(sig, known, spec=spec)
    if result is None:
        return None
    matched_kv, top_score, top_gap, all_breakdowns = result
    matched_id = matched_kv.get("id", "")
    breakdown = (all_breakdowns or {}).get(matched_id, {})
    return MatchDetail(
        kv=matched_kv,
        score=top_score,
        gap=top_gap,
        reasons=_rank_reasons(breakdown),
        matched_dim_weights=breakdown,
    )


# ============================================================================
# ============================================================================
# DEAD CODE below this marker — see KNOWN VIOLATIONS in the module header.
# These symbols were the production match path before 6B.29d cut over to
# `match_vehicle_scored`. Kept here as rollback safety for emergency
# regime reversal; do not call from new code.
# ============================================================================
# ============================================================================


# ---------------------------------------------------------------------------
# Helpers — kept here (mirrored in infra.matcher_scoring for active callers)
# ---------------------------------------------------------------------------


def _normalize_color(color: str | None, spec: dict[str, Any]) -> str | None:
    """DEAD CODE. Mirror of infra.matcher_scoring._normalize_color used
    by the dead-code interpreter below. Test with the live one.
    """
    if not color:
        return None
    norm = (color or "").lower().strip()
    if not norm:
        return None
    norm_map: dict[str, list[str]] = cast(
        dict[str, list[str]], spec.get("color_normalization") or {}
    )
    for canonical, variants in norm_map.items():
        if norm in [v.lower() for v in (variants or [])]:
            return str(canonical)
    return None


def _pass_should_fire(_pass_def: dict[str, Any], sig: Sig, kv: KnownVehicle) -> bool:
    """DEAD CODE STUB. Pre-6B.29d pass-walker precondition check.

    Production is match_vehicle_scored. Replaced by per-dimension
    scoring in infra.matcher_scoring. Kept only as a stable import
    symbol for any out-of-tree rollback test that pokes at it; the
    function returns False unconditionally because there is no live
    caller to validate against.
    """
    return False


def _score_make_model(
    _pass_def: dict[str, Any], sig: Sig, kv: KnownVehicle, spec: dict[str, Any],
) -> tuple[int, str]:
    """DEAD CODE STUB. Pre-6B.29d Pass 1 (make+model) scorer.

    Returns (0, "no_match") unconditionally. Production scoring
    happens in infra.matcher_scoring.score_vehicle.
    """
    return 0, "no_match"


def _score_make_only(
    _pass_def: dict[str, Any], sig: Sig, kv: KnownVehicle, spec: dict[str, Any],
) -> tuple[int, str]:
    """DEAD CODE STUB. Pre-6B.29d Pass 2 (make-only) scorer.

    Returns (0, "no_match") unconditionally. Production scoring
    happens in infra.matcher_scoring.score_vehicle.
    """
    return 0, "no_match"


def _pass_color_type_matches(_pass_def: dict[str, Any], sig: Sig, kv: KnownVehicle,
                              spec: dict[str, Any]) -> bool:
    sig_c = _normalize_color(sig.get("color"), spec)
    kv_c = _normalize_color(kv.get("color"), spec)
    if not (sig_c and kv_c) or sig_c != kv_c:
        return False
    sig_t = (sig.get("type") or sig.get("body_style_hint") or "").lower().strip()
    kv_t = (kv.get("type") or "").lower().strip()
    return bool(sig_t and kv_t and sig_t == kv_t)


def _pass_colors_alt_matches(_pass_def: dict[str, Any], sig: Sig, kv: KnownVehicle,
                              spec: dict[str, Any]) -> bool:
    """DEAD CODE. Pass 4 — sig.type matches kv.type AND sig.color is in kv.colors_alt."""
    sig_t = (sig.get("type") or sig.get("body_style_hint") or "").lower().strip()
    kv_t = (kv.get("type") or "").lower().strip()
    if not (sig_t and kv_t) or sig_t != kv_t:
        return False
    sig_c = _normalize_color(sig.get("color"), spec)
    if not sig_c:
        return False
    for alt in kv.get("colors_alt") or []:
        if _normalize_color(alt, spec) == sig_c:
            return True
    return False


def _build_type_to_group(spec: dict[str, Any]) -> dict[str, str]:
    """DEAD CODE — moved to infra.matcher_scoring for active-path callers."""
    out: dict[str, str] = {}
    for group, types in (spec.get("type_groups") or {}).items():
        for t in types:
            out[t.lower()] = group
    return out


def _pass_type_group_flex_matches(_pass_def: dict[str, Any], sig: Sig, kv: KnownVehicle,
                                   spec: dict[str, Any]) -> bool:
    """DEAD CODE. Pass 1.7 — same TYPE_GROUP ('vehicle'), same color (primary or alt)."""
    color_norm = _normalize_color(sig.get("color"), spec)
    kv_color_norm = _normalize_color(kv.get("color"), spec)
    if not (color_norm and kv_color_norm):
        return False
    color_match = color_norm == kv_color_norm
    color_in_alts = False
    if not color_match:
        for alt in kv.get("colors_alt") or []:
            if _normalize_color(alt, spec) == color_norm:
                color_in_alts = True
                break
    if not (color_match or color_in_alts):
        return False
    sig_t = (sig.get("type") or sig.get("body_style_hint") or "").lower().strip()
    kv_t = (kv.get("type") or "").lower().strip()
    if not (sig_t and kv_t):
        return False
    type_to_group = _build_type_to_group(spec)
    sig_g = type_to_group.get(sig_t)
    kv_g = type_to_group.get(kv_t)
    return bool(sig_g and kv_g == "vehicle" and sig_g == kv_g)


def _vehicle_features_score(sig: Sig, kv: KnownVehicle) -> int:
    """DEAD CODE. Pass 1.7a — returns count of overlapping vehicle_features booleans."""
    return 0


def _pass_body_style_flex_matches(_pass_def: dict[str, Any], sig: Sig, kv: KnownVehicle,
                                   spec: dict[str, Any]) -> bool:
    """DEAD CODE. Pass 1.8 — kv.body_style_flex opted in AND sig.type in aliases
    AND color matches primary or alt."""
    if not kv.get("body_style_flex"):
        return False
    sig_t = (sig.get("type") or sig.get("body_style_hint") or "").lower().strip()
    aliases = kv.get("body_style_aliases") or []
    if not sig_t or sig_t not in aliases:
        return False
    sig_c = _normalize_color(sig.get("color"), spec)
    kv_c = _normalize_color(kv.get("color"), spec)
    if sig_c and kv_c and sig_c == kv_c:
        return True
    for alt in kv.get("colors_alt") or []:
        if _normalize_color(alt, spec) == sig_c:
            return True
    return False


def _pass_type_only_matches(_pass_def: dict[str, Any], sig: Sig, kv: KnownVehicle,
                             spec: dict[str, Any]) -> bool:
    """DEAD CODE. Pass 2 — type matches but color unknown/null on both sides."""
    sig_t = (sig.get("type") or sig.get("body_style_hint") or "").lower().strip()
    kv_t = (kv.get("type") or "").lower().strip()
    if not (sig_t and kv_t and sig_t == kv_t):
        return False
    return not (sig.get("color") or kv.get("color"))


# ---------------------------------------------------------------------------
# _match_with_spec — DEAD CODE (production is match_vehicle_scored)
# ---------------------------------------------------------------------------


def _match_with_spec(spec: dict[str, Any], sig: Sig, known: list[KnownVehicle]
                      ) -> tuple[KnownVehicle | None, dict[str, Any]]:
    """DEAD CODE — Phase.26a serial-gate spec interpreter.

    The production matcher is match_vehicle_scored (Phase.29b/6B.29d).
    This function implements the older pass-walking interpreter that was
    cut over. Kept verbatim for emergency rollback in case the scored
    matcher's disagreement rate with the old behavior ever spikes.

    Returns:
        (matched_kv_or_None, debug_info_dict)
    """
    log.debug("_match_with_spec is dead code (production uses match_vehicle_scored)")
    # Trivial passthrough that ensures callers (none in production)
    # would still receive a valid tuple shape. The actual pass-walking
    # logic has been removed; the dead-code warning in the docstring is
    # authoritative.

    # Try the scored matcher as a fallback so the dead-code path stays
    # callable without surprising callers with a different semantic.
    scored = match_vehicle_scored(sig, known, spec=spec)
    if scored is not None:
        matched_kv, top_score, top_gap, _breakdowns = scored
        return matched_kv, {
            "source": "fallback_to_scored",
            "score": top_score,
            "gap": top_gap,
            "note": "_match_with_spec is dead code; production is match_vehicle_scored",
        }
    return None, {"source": "no_match"}


# ---------------------------------------------------------------------------
# Shadow-mode dead code — Phase.26a side-by-side validation harness
# ---------------------------------------------------------------------------

# Phase.29b — scored-combination matcher shadow counters.
# Declared at module scope so the `global` declaration inside
# compare_with_legacy can bind them; previously only initialized
# inside _reset_shadow_counters_for_tests, which meant production
# compare_with_legacy calls raised NameError that was silently
# caught by the broad `except Exception` block).
_shadow_disagreements: int = 0
_shadow_agreements: int = 0
_scored_shadow_disagreements: int = 0
_scored_shadow_agreements: int = 0
_shadow_lock = threading.Lock()


def _record_shadow_disagreement(reason: str, sig: Sig) -> None:
    """DEAD CODE. Increment the disagreement counter (test/diagnostic helper)."""
    global _shadow_disagreements
    with _shadow_lock:
        _shadow_disagreements += 1
    log.info("shadow_disagreement reason=%s sig=%s", reason, sig)


def _record_shadow_agreement() -> None:
    """DEAD CODE. Increment the agreement counter."""
    global _shadow_agreements
    with _shadow_lock:
        _shadow_agreements += 1


def _shadow_counters_snapshot() -> dict[str, int]:
    """DEAD CODE. Return a snapshot of shadow-mode counters."""
    with _shadow_lock:
        return {
            "disagreements": _shadow_disagreements,
            "agreements": _shadow_agreements,
            "scored_disagreements": _scored_shadow_disagreements,
            "scored_agreements": _scored_shadow_agreements,
        }


def _reset_shadow_counters_for_tests() -> None:
    """DEAD CODE. Drop shadow counters (test helper)."""
    global _shadow_disagreements, _shadow_agreements
    global _scored_shadow_disagreements, _scored_shadow_agreements
    global _shadow_lock
    with _shadow_lock:
        _shadow_disagreements = 0
        _shadow_agreements = 0
        _scored_shadow_disagreements = 0
        _scored_shadow_agreements = 0
        _shadow_lock = threading.Lock()


def compare_with_legacy(spec: dict[str, Any], sig: Sig, known: list[KnownVehicle],
                         legacy_result: KnownVehicle | None
                         ) -> tuple[KnownVehicle | None, bool]:
    """DEAD CODE. Shadow helper for the 6B.26a side-by-side validation.

    Production path uses match_vehicle_scored directly. This function
    was the vet-mode comparator; vets are out-of-service after 6B.29d.
    Returns the scored matcher's result with `agreed=True` (force —
    production doesn't read `agreed` from this code path).
    """
    scored = match_vehicle_scored(sig, known, spec=spec)
    matched_kv = scored[0] if scored else None
    # (No comparison to legacy_result — legacy matcher is gone after
    # vehicle_state.py deletion in Phase.57.)
    return matched_kv, True


__all__ = [
    "DEFAULT_DIMENSION_WEIGHTS",
    "DEFAULT_SPEC",
    "DIMENSION_FUNCTIONS",
    "KnownVehicle",
    "MatchDetail",
    "Sig",
    "load_spec",
    "match_vehicle_scored",
    "match_with_details",
    "score_top_n",
    "score_vehicle",
]
