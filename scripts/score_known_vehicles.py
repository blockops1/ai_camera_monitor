#!/usr/bin/env python
"""score_known_vehicles.py — pre-flight dry-run for vehicle enrollment.

Runs the listener's actual matcher (vehicle_matcher.matcher.match_signature)
against the loaded known_vehicles.json store with a set of candidate
sightings, and prints a top-N ranked list per candidate.

USE THIS BEFORE COMMITTING A NEW ENTRY to data/vehicles/known_vehicles.json.

The vehicle-enrollment skill recommends verifying via synthetic webhook
to /alert (~30s, vision + cascade + match). That path catches whether
the new vehicle was matched. It does NOT catch whether it was matched
*correctly* versus a dominant existing entry (e.g. a white F-350 with
match_priority="make_model_then_color_type" outscoring a new blue
Silverado enrollment).

This script catches that class of bug in <100ms with no I/O beyond
the on-disk store load.

Usage:
    cd ~/farm-surveillance-refactor
    .venv/bin/python scripts/score_known_vehicles.py                 # all canonical candidates
    .venv/bin/python scripts/score_known_vehicles.py --candidate-id v_jeremiah_blue
                                                            # narrow to one entry
    .venv/bin/python scripts/score_known_vehicles.py --top-n 5      # more depth

Exit codes:
    0 — every candidate's expected v_id is at rank 1 with gap > 0.5
    1 — at least one candidate's expected v_id is NOT at rank 1 OR gap < 0.5
    2 — at least one candidate returned NoMatch (sighting too vague)
    3 — store or matcher import failed (block commit)

The non-zero exit codes are intentional: this script is wired into
the pre-commit working-tree audit (see AGENTS.md Step 6.7).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root on sys.path so `known_vehicles` and `vehicle_matcher` resolve.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from known_vehicles.store import load_known_vehicles
from vehicle_matcher.matcher import match_signature


# Canonical candidate sightings — one per enrolled vehicle, derived from
# the entry's own color/type/make/model. If you add a new entry and the
# new candidate isn't here, the script won't verify it.
def _canonical_candidates(vehicles: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for v in vehicles:
        out.append((
            v["id"],
            {
                "color": v.get("color"),
                "type": v.get("type"),
                "make": v.get("make"),
                "model": v.get("model"),
                "vehicle_features": v.get("vehicle_features", {}) or {},
            },
        ))
    return out


def _print_table(
    label: str,
    expected_vid: str,
    result,
    top_n: int = 3,
) -> bool:
    """Return True if expected_vid is at rank 1 with gap > 0.5."""
    print(f"\n=== {label} (expected v_id={expected_vid}) ===")
    if not hasattr(result, "all_scores"):
        # NoMatch case
        print(f"  NoMatch: reason={getattr(result, 'reason', 'unknown')}")
        return False

    scores = result.all_scores
    for i, (vid, s) in enumerate(scores[:top_n]):
        next_s = scores[i + 1][1] if i + 1 < len(scores) else 0.0
        gap = s - next_s
        marker = "✓" if vid == expected_vid else " "
        print(f"  {marker} rank {i + 1}. {vid:30} score={s:6.2f}  gap_to_next={gap:5.2f}")

    # Check whether the expected v_id is at rank 1.
    rank1_vid = scores[0][0] if scores else None
    rank1_score = scores[0][1] if scores else 0.0
    rank2_score = scores[1][1] if len(scores) > 1 else 0.0
    gap = rank1_score - rank2_score
    return rank1_vid == expected_vid and gap > 0.5


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score known vehicles against canonical candidate sightings (pre-flight dry-run for enrollment).",
    )
    parser.add_argument(
        "--candidate-id",
        help="Only run the candidate matching this v_id (e.g. v_jeremiah_blue)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=3,
        help="Number of top-N matches to print per candidate (default 3)",
    )
    args = parser.parse_args()

    # Load the store. Will raise FileNotFoundError if missing.
    try:
        vehicles = load_known_vehicles()
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"FATAL: load_known_vehicles() failed: {exc}", file=sys.stderr)
        return 3
    print(f"Loaded {len(vehicles)} known vehicles.")

    candidates = _canonical_candidates(vehicles)
    if args.candidate_id:
        candidates = [(vid, sig) for vid, sig in candidates if vid == args.candidate_id]
        if not candidates:
            print(f"No candidate matches --candidate-id={args.candidate_id}",
                  file=sys.stderr)
            return 3

    rank1_failures: list[tuple[str, str, float, float]] = []
    nomatch_failures: list[str] = []

    for expected_vid, signature in candidates:
        # Skip candidates with no usable fields — they were never enrolled
        # with enough info to test against.
        if not signature.get("color") or not signature.get("type"):
            print(f"\n=== {expected_vid} ===")
            print("  SKIP: entry lacks color or type; cannot build test sighting.")
            continue
        result = match_signature(signature, vehicles)
        healthy = _print_table(
            label=f"Candidate sighting for {expected_vid}",
            expected_vid=expected_vid,
            result=result,
            top_n=args.top_n,
        )
        if not hasattr(result, "all_scores"):
            nomatch_failures.append(expected_vid)
        elif not healthy:
            scores = result.all_scores
            rank1_vid = scores[0][0]
            rank1_score = scores[0][1]
            rank2_score = scores[1][1] if len(scores) > 1 else 0.0
            gap = rank1_score - rank2_score
            rank1_failures.append((expected_vid, rank1_vid, rank1_score, gap))

    # Summary.
    print("\n" + "=" * 60)
    print(f"Summary: {len(candidates)} candidates tested.")
    print(f"  rank-1 with gap > 0.5:    {len(candidates) - len(rank1_failures) - len(nomatch_failures)}")
    print(f"  rank-1 but low gap:       {len(rank1_failures)}")
    print(f"  NoMatch (sighting vague): {len(nomatch_failures)}")

    if rank1_failures:
        print("\nDominance collisions / low gaps (NOT healthy):")
        for expected, actual, score, gap in rank1_failures:
            print(f"  expected={expected}  actual_winner={actual}  "
                  f"score={score:.2f}  gap={gap:.2f}")

    if nomatch_failures:
        print("\nNoMatch results (sighting shape too vague to enroll against):")
        for vid in nomatch_failures:
            print(f"  {vid}")

    # Exit code: 1 if any rank-1 mismatch, 2 if any NoMatch, 0 otherwise.
    if rank1_failures:
        return 1
    if nomatch_failures:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())