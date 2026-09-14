"""no_match_telegram.py — Build TG#3 Telegram message body when vehicle match is rejected.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - reason: str (required) -- reason the match was rejected
    - top_candidates: list of (kv_id, score) tuples (required) -- top-N candidates
    - match_threshold: float (required) -- score threshold used for matching
    - gap_threshold: float (required) -- gap threshold used for matching
    - captured_at: str (required) -- ISO-8601 timestamp of capture

OUTPUTS:
    - return str: formatted body string for the no-match alert

PUBLIC API:
    build_no_match_alert_body(reason, top_candidates, match_threshold, gap_threshold, captured_at) -> str
        Build the no-match vehicle details block for TG#3 (reason, top
        candidates with per-dim scores, thresholds).

DOES NOT DO:
    - Compute matching scores (caller passes pre-computed top_candidates)
    - Send Telegram messages

CALLED BY:
    - telegram_formatter.match_alert.build_match_message (no-match branch)

RELATED:
    - vehicle_matcher.match.score_top_n (producer of top_candidates)
"""

from __future__ import annotations


def build_no_match_alert_body(
    reason: str,
    top_candidates: list[tuple[str, float]],
    match_threshold: float,
    gap_threshold: float,
    captured_at: str,
) -> str:
    """Build the no-match body for a TG#3 rejected alert.

    Slimmed v1 layout:

        Reason: <reason>

        Top candidates:
          #1 <kv_id>: <score>
          #2 <kv_id>: <score>

        Thresholds:
          Match: <match_threshold>
          Gap: <gap_threshold>

    Args:
        reason: Human-readable reason for rejection.
        top_candidates: List of (kv_id, score) tuples, sorted descending.
        match_threshold: Score threshold used for the match decision.
        gap_threshold: Gap threshold used for the match decision.
        captured_at: ISO-8601 timestamp of the capture event.

    Returns:
        Formatted body string.
    """
    lines: list[str] = [f"Reason: {reason}"]
    lines.append("")

    if top_candidates:
        lines.append("Top candidates:")
        for i, (kid, score) in enumerate(top_candidates[:3], 1):
            score_str = f"{score:.1f}"
            lines.append(f"  #{i} {kid}: {score_str}")
        lines.append("")

    lines.append("Thresholds:")
    lines.append(f"  Match: {match_threshold:.1f}")
    lines.append(f"  Gap: {gap_threshold:.1f}")

    return "\n".join(lines)
