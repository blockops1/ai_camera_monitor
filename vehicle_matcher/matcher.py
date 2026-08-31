"""Matcher — signature + known vehicles → match verdict.

Pure functions. No I/O. No vision. No Telegram.

The matcher scores the signature against every known vehicle, sorts
by score, and decides whether the top candidate clears both the
confidence threshold AND the gap threshold (so ambiguous matches
get rejected).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .scoring import (
    ScoringSpec,
    score_signature_against_known,
)


@dataclass(frozen=True)
class MatchVerdict:
    """A successful match.

    Attributes:
        known_vehicle: The matched known_vehicle dict.
        score: The total weighted score.
        gap: score - second_best_score. Required >= spec.gap_threshold.
        breakdowns: {dim_name: score} for every scored dimension.
        rank: The matcher's confidence rank (0-indexed). 0 = best.
        all_scores: Sorted list of (kv_id, score) for transparency.
    """
    known_vehicle: dict[str, Any]
    score: float
    gap: float
    breakdowns: dict[str, float]
    rank: int
    all_scores: list[tuple]  # List of (kv_id, score)


@dataclass(frozen=True)
class NoMatch:
    """A failed match.

    Attributes:
        reason: "below_threshold" | "below_gap" | "no_known_vehicles"
        top_candidates: Sorted list of (kv_id, score) for the top-N
            candidates. Always present (even on no_known_vehicles — empty).
    """
    reason: str
    top_candidates: list[tuple] = field(default_factory=list)


def _score_against_all(
    signature: dict[str, Any],
    known_vehicles: list[dict[str, Any]],
    spec: ScoringSpec,
) -> list[tuple]:
    """Score signature against every known vehicle.

    Returns: Sorted list of (kv_id, score, breakdowns) descending.
    """
    scored = []
    for kv in known_vehicles:
        total, breakdowns = score_signature_against_known(
            signature, kv, spec=spec,
        )
        scored.append((kv.get("id", "?"), total, breakdowns))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def match_signature(
    signature: dict[str, Any],
    known_vehicles: list[dict[str, Any]],
    spec: ScoringSpec | None = None,
) -> Any:
    """Score signature and return either MatchVerdict or NoMatch.

    Args:
        signature: The signature dict (output of extract_signature).
        known_vehicles: List of known vehicle dicts from the store.
        spec: ScoringSpec. None = defaults.

    Returns:
        MatchVerdict if confidence >= spec.confidence_threshold
            AND gap >= spec.gap_threshold.
        NoMatch otherwise.

    Pure function.
    """
    if spec is None:
        spec = ScoringSpec()
    _spec: ScoringSpec = spec

    if not known_vehicles:
        return NoMatch(
            reason="no_known_vehicles",
            top_candidates=[],
        )

    scored = _score_against_all(signature, known_vehicles, _spec)

    if not scored:
        return NoMatch(
            reason="no_known_vehicles",
            top_candidates=[],
        )

    top_kv_id, top_score, top_breakdowns = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    gap = top_score - second_score

    # Build all_scores (kv_id, score) pairs for transparency.
    all_scores = [(kv_id, s) for kv_id, s, _ in scored]

    # Threshold checks.
    if top_score < _spec.confidence_threshold:
        return NoMatch(
            reason="below_threshold",
            top_candidates=all_scores,
        )
    if gap < _spec.gap_threshold:
        return NoMatch(
            reason="below_gap",
            top_candidates=all_scores,
        )

    # Find the actual known_vehicle dict for the winner.
    winner = next(
        (kv for kv in known_vehicles if kv.get("id") == top_kv_id),
        None,
    )
    if winner is None:
        return NoMatch(
            reason="below_threshold",
            top_candidates=all_scores,
        )

    return MatchVerdict(
        known_vehicle=winner,
        score=top_score,
        gap=gap,
        breakdowns=top_breakdowns,
        rank=0,
        all_scores=all_scores,
    )


def score_top_n(
    signature: dict[str, Any],
    known_vehicles: list[dict[str, Any]],
    n: int = 3,
    spec: ScoringSpec | None = None,
) -> list[tuple]:
    """Return the top-N candidates by score, regardless of threshold.

    Used by the no-match Telegram to show the user what the matcher
    considered, with per-dimension breakdowns.

    Args:
        signature: The signature dict.
        known_vehicles: List of known vehicles.
        n: How many top candidates to return.
        spec: ScoringSpec. None = defaults.

    Returns:
        List of (kv_id, score, breakdowns_dict) tuples, sorted
        descending. Length <= min(n, len(known_vehicles)).
        Empty list if no known vehicles.

    Pure function.
    """
    if spec is None:
        spec = ScoringSpec()
    if not known_vehicles:
        return []
    scored = _score_against_all(signature, known_vehicles, spec)
    return [(kv_id, s, bd) for kv_id, s, bd in scored[:n]]
