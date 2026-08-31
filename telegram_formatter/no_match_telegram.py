"""No-Match Telegram body.

The second Telegram sent when the matcher says NO.

Contains:
  - Header: camera name + the reason (below_threshold / below_gap /
    no_known_vehicles)
  - Top-3 candidates with per-dimension breakdowns
  - The threshold values used

Pure function. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from vehicle_matcher import NoMatch  # 6B.90 package-form (per farm-surveillance-workflow skill)


@dataclass(frozen=True)
class NoMatchTelegramInput:
    """Inputs for the No-Match Telegram body.

    Attributes:
        camera_name:      e.g. "<CAMERA_LABEL>"
        captured_at_iso:  ISO-8601 timestamp string
        no_match:         The NoMatch verdict from the matcher.
        top_n_breakdowns: List of (kv_id, score, breakdowns_dict) tuples
                          from score_top_n(). The body shows the top-N
                          (default 3) candidates with their per-dimension
                          scores so the operator can see what the matcher
                          considered.
        match_threshold:  The confidence threshold used.
        gap_threshold:    The gap threshold used.
        alert_id:         Optional alert identifier.
    """
    camera_name: str
    captured_at_iso: str
    no_match: NoMatch
    top_n_breakdowns: list[tuple[str, float, dict[str, float]]]
    match_threshold: float
    gap_threshold: float
    alert_id: str | None = None


def _format_reason(no_match: NoMatch) -> str:
    """Human-readable reason text."""
    if no_match.reason == "below_threshold":
        return "Top score below confidence threshold"
    if no_match.reason == "below_gap":
        return "Top score too close to runner-up (gap too small)"
    if no_match.reason == "no_known_vehicles":
        return "No known vehicles to match against"
    return f"Unknown reason: {no_match.reason}"


def _format_top_n(top_n: list[tuple[str, float, dict[str, float]]]) -> str:
    """Format the top-N candidates with per-dimension breakdowns."""
    if not top_n:
        return "  (no candidates)"

    blocks: list[str] = []
    for rank, (kv_id, score, breakdowns) in enumerate(top_n, start=1):
        block_lines: list[str] = []
        block_lines.append(f"  #{rank} {kv_id}  (score: {score:.2f})")
        # Sort breakdowns by score descending so strongest dimensions
        # appear first.
        sorted_dims = sorted(
            breakdowns.items(), key=lambda kv: kv[1], reverse=True,
        )
        for dim, dim_score in sorted_dims:
            block_lines.append(f"      {dim}: {dim_score:.2f}")
        blocks.append("\n".join(block_lines))
    return "\n\n".join(blocks)


def build_no_match_telegram_body(input: NoMatchTelegramInput) -> str:
    """Build the No-Match Telegram body.

    Args:
        input: NoMatchTelegramInput with the no-match verdict and
               top-N candidates.

    Returns:
        The Telegram body as a string. No I/O.

    Layout:
        <header>            ← ❌ No match — <camera>
        <blank>
        ❌ No match — <reason>
        <blank>
        Top candidates:
          #1 <kv_id> (score: <score>)
             <dim>: <score>
             ...
          #2 ...
        <blank>
        Thresholds: confidence≥..., gap≥...
        <blank>
        <captured_at>                    (omitted if empty)

    Phase.114 (2026-08-25): Removed [alert_id] prefix from header
    and removed the captured_at_iso from the header (was interrupting
    the word-flow). The captured_at webhook time is now a footer
    line at the end of the body — event time, not send time, per
    Note correction.
    """
    nm = input.no_match
    lines: list[str] = []

    # Header.
    lines.append(f"❌ No match — {input.camera_name}")

    lines.append("")
    lines.append(f"Reason: {_format_reason(nm)}")

    lines.append("")
    lines.append("Top candidates:")
    lines.append(_format_top_n(input.top_n_breakdowns))

    lines.append("")
    lines.append(
        f"Thresholds: confidence≥{input.match_threshold:.2f}, "
        f"gap≥{input.gap_threshold:.2f}"
    )

    # Footer — webhook event time (Phase.114).
    if input.captured_at_iso:
        lines.append("")
        lines.append(input.captured_at_iso)

    return "\n".join(lines)
