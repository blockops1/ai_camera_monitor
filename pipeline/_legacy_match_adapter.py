"""
_legacy_match_adapter.py — Bridge from infra.vehicle_matcher (legacy 15-dim
scorer) to the modular MatchVerdict | NoMatch shape that
pipeline/orchestrator.py and telegram_formatter/{match,no_match}_telegram.py
consume.

Phase.105 (2026-08-20) — Note asked to wire pipeline/orchestrator.py into
the listener. The orchestrator's match step currently uses
vehicle_matcher.match_signature (4-dim). Production uses
infra.vehicle_matcher.match_vehicle_scored (15-dim, materially better per
scripts/probe_matcher_comparison.py from Phase.103). To preserve
production match quality without regressing to 4 dims, this adapter wraps
the legacy scorer and presents it as a MatchVerdict | NoMatch.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - function arg signature: dict (required) — flattened signature dict
      (color/type/make/model/vehicle_features). Same shape as
      vehicle_matcher.scoring accepts.
    - function arg known_vehicles: list[dict] (required) — list of known
      vehicle dicts from known_vehicles/store.py.
    - function arg n: int (optional, default 3) — for score_top_n_with_legacy

OUTPUTS:
    - return value from match_with_legacy: MatchVerdict | NoMatch
    - return value from score_top_n_with_legacy: list[(kv_id, score, breakdowns)]

PUBLIC API:
    match_with_legacy(signature, known_vehicles) -> MatchVerdict | NoMatch
        Run the legacy 15-dim scorer. Wrap the result in a MatchVerdict
        (on success) or NoMatch (on below_threshold / below_gap /
        no_known_vehicles). Same shape as vehicle_matcher.matcher.match_signature
        so the orchestrator's downstream telegram formatters don't need
        to know which engine produced the verdict.

    score_top_n_with_legacy(signature, known_vehicles, n=3) -> list[tuple]
        Run the legacy 15-dim scorer to get top-N candidates. Returns a
        list of (kv_id, score, breakdowns_dict) tuples (the same shape
        as vehicle_matcher.matcher.score_top_n) so the no-match telegram
        formatter doesn't need to know which engine produced the list.

DOES NOT DO:
    - Implement the scoring engine — delegates to infra.vehicle_matcher
    - Persist anything — pure functions
    - Translate MatchVerdict → legacy tuple — that's the listener's job
      when reading the verdict for STATE updates
    - Decide whether to send a Telegram — orchestrator owns that

WHY HERE:
    Phase.105 plan doc §11.33. The legacy 15-dim scorer
    (infra.vehicle_matcher.match_vehicle_scored) is materially better
    than the modular 4-dim scorer (vehicle_matcher.matcher.match_signature)
    per scripts/probe_matcher_comparison.py. Adopting the modular scorer
    directly would regress match quality. The adapter is a thin shim
    that lets the orchestrator call into the legacy scorer without
    regressing on color normalization, type-group flex, model aliases,
    or negative scoring for mismatches.

CALLED BY:
    - pipeline/orchestrator.py: step 3 (match) and step 4b/4c (no-match)

CALLS INTO:
    - infra.vehicle_matcher: match_vehicle_scored, score_top_n
    - vehicle_matcher: MatchVerdict, NoMatch (for return types only)

RELATED:
    - infra.vehicle_matcher.py (production scoring engine, 15 dims)
    - vehicle_matcher/matcher.py (modular scoring engine, 4 dims, 4-dim path not used in production per Phase.105)
    - scripts/probe_matcher_comparison.py (Phase.103, proves legacy > modular)
"""
from __future__ import annotations

from typing import Any

from infra.vehicle_matcher import score_top_n as _legacy_score_top_n
from vehicle_matcher import MatchVerdict, NoMatch


def match_with_legacy(
    signature: dict[str, Any],
    known_vehicles: list[dict[str, Any]],
    confidence_threshold: float | None = None,
    gap_threshold: float | None = None,
) -> MatchVerdict | NoMatch:
    """Run the legacy 15-dim scorer. Wrap result in MatchVerdict | NoMatch.

    Args:
        signature: The flattened signature dict from identifier.
        known_vehicles: List of known vehicle dicts.
        confidence_threshold: Optional override for the confidence threshold.
            None = read from infra.matcher_spec.load_spec() (default 0.6).
            Used by orchestrator's PipelineConfig to allow per-run tuning.
        gap_threshold: Optional override for the gap threshold.
            None = read from infra.matcher_spec.load_spec() (default 0.15).
            Used by orchestrator's PipelineConfig to allow per-run tuning.

    Returns:
        MatchVerdict if the legacy scorer returns a tuple (cleared both
            confidence_threshold AND gap_threshold).
        NoMatch otherwise. The reason field is set to one of:
            "no_known_vehicles" — known_vehicles list was empty.
            "below_threshold"  — top_score < confidence_threshold.
            "below_gap"        — top_score - second_score < gap_threshold.

    Note: the legacy scorer's match_vehicle_scored returns None for both
    "below_threshold" and "below_gap" cases. We can't distinguish them
    from the None return. To recover the distinction, we call
    score_top_n to get all_scores and then compare top to threshold.
    """
    if not known_vehicles:
        return NoMatch(
            reason="no_known_vehicles",
            top_candidates=[],
        )

    # Resolve thresholds: caller override > spec default.
    from infra.matcher_spec import load_spec
    spec = load_spec()
    spec_thresholds = spec.get("thresholds", {"confidence": 0.6, "gap": 0.15})
    if confidence_threshold is None:
        confidence_threshold = float(spec_thresholds.get("confidence", 0.6))
    if gap_threshold is None:
        gap_threshold = float(spec_thresholds.get("gap", 0.15))

    # Use score_top_n to get all_scores; we need them for MatchVerdict
    # and to distinguish "below_threshold" vs "below_gap" on NoMatch.
    all_scored = _legacy_score_top_n(
        sig=signature,
        known=known_vehicles,
        n=len(known_vehicles),
    )

    if not all_scored:
        return NoMatch(
            reason="no_known_vehicles",
            top_candidates=[],
        )

    # Build all_scores in the modular shape: list of (kv_id, score).
    all_scores = [(kv.get("id", "?"), score) for kv, score, _ in all_scored]

    top_kv, top_score, top_breakdowns = all_scored[0]
    second_score = all_scored[1][1] if len(all_scored) > 1 else 0.0
    gap = top_score - second_score

    if top_score < confidence_threshold:
        return NoMatch(
            reason="below_threshold",
            top_candidates=all_scores,
        )
    if gap < gap_threshold:
        return NoMatch(
            reason="below_gap",
            top_candidates=all_scores,
        )

    return MatchVerdict(
        known_vehicle=top_kv,
        score=top_score,
        gap=gap,
        breakdowns=top_breakdowns,
        rank=0,
        all_scores=all_scores,
    )


def score_top_n_with_legacy(
    signature: dict[str, Any],
    known_vehicles: list[dict[str, Any]],
    n: int = 3,
) -> list[tuple[str, float, dict[str, float]]]:
    """Run the legacy 15-dim scorer for top-N candidates.

    Returns:
        List of (kv_id, score, breakdowns_dict) tuples, sorted
        descending by score. Length <= min(n, len(known_vehicles)).
        Empty list if no known vehicles.

    Note: this is for the no-match Telegram path, where we want to show
    the user the closest candidates with their per-dimension breakdowns
    regardless of whether anything cleared the threshold.
    """
    if not known_vehicles:
        return []
    scored = _legacy_score_top_n(
        sig=signature,
        known=known_vehicles,
        n=n,
    )
    # Legacy shape is (kv, score, breakdowns). Reshape to (kv_id, score, breakdowns).
    return [(kv.get("id", "?"), score, bd) for kv, score, bd in scored]