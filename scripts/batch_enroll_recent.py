#!/usr/bin/env python3
"""batch_enroll_recent.py — Batch-enroll persons from recent frame crops.

STATUS: alpha — US-106a
THREAD SAFETY: single-threaded CLI — no concurrency concerns.

INPUTS:
    - data/frames/<cam>/<uuid>/crop_*.png — person crops from recent alerts.
    - --since <iso> — only consider crops newer than this timestamp.
    - --max-final N — max picks to enroll (default 8).
    - --dry-run — print picks without writing.

OUTPUTS:
    - Prints selected persons and exits 0.
    - Without --dry-run: writes to data/known/people/known_people.json + embeddings.npz.

PUBLIC API:
    main() — entry point; scans frame dirs, dedupes, embeds, picks diverse set.

DOES NOT DO:
    - Vision-verify crops (separate card US-106b).
    - Handle non-person crops (relies on crop naming convention).

USAGE:
    python scripts/batch_enroll_recent.py --since 2026-10-12T00:00:00
    python scripts/batch_enroll_recent.py --since 2h --dry-run
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRAMES_DIR: Path = Path(__file__).resolve().parents[1] / "data" / "frames"
KNOWN_PEOPLE_JSON: Path = Path(__file__).resolve().parents[1] / "data" / "known" / "people" / "known_people.json"
EMBEDDINGS_NPZ: Path = Path(__file__).resolve().parents[1] / "data" / "known" / "people" / "embeddings.npz"


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _find_crops_since(since: datetime.datetime) -> dict[str, list[Path]]:
    """Find all crop_*.png files under data/frames/<cam>/<uuid>/ newer than since.

    Returns:
        dict mapping alert_id -> list of crop paths for that alert.
        One crop per alert (deduped).
    """
    if not FRAMES_DIR.exists():
        return {}

    alerts: dict[str, list[Path]] = {}
    for cam_dir in sorted(FRAMES_DIR.iterdir()):
        if not cam_dir.is_dir():
            continue
        for alert_dir in sorted(cam_dir.iterdir()):
            if not alert_dir.is_dir():
                continue
            alert_id = alert_dir.name
            # Check mtime of the alert dir
            if alert_dir.stat().st_mtime < since.timestamp():
                continue
            crops = sorted(
                p for p in alert_dir.iterdir()
                if p.name.startswith("crop_") and p.suffix.lower() in (".png", ".jpg")
            )
            if crops:
                # Pick the first crop per alert (one-per-alert dedup)
                # In production, we'd pick the best crop; here just take first
                alerts[alert_id] = [crops[0]]

    return alerts


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def _embed_batch(paths: list[str]) -> np.ndarray:
    """Embed a batch of crops; shape (N, 512)."""
    from infra.person_embedder import embed_person_batch
    return embed_person_batch(paths)


# ---------------------------------------------------------------------------
# Dedup + Diversity
# ---------------------------------------------------------------------------


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors."""
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _pairwise_sim_matrix(embeddings: np.ndarray) -> np.ndarray:
    """Compute NxN pairwise cosine similarity matrix."""
    n = embeddings.shape[0]
    sim = np.zeros((n, n))
    for i in range(n):
        for j in range(i + 1, n):
            s = _cosine_sim(embeddings[i], embeddings[j])
            sim[i, j] = s
            sim[j, i] = s
    return sim


def _outlier_filter(
    sim_matrix: np.ndarray, embeddings: np.ndarray
) -> list[int]:
    """Remove outliers: keep top-5 by mean similarity, then drop below-threshold.

    Strategy: compute mean pairwise sim for each embedding.
    Top-5 by mean sim are kept as the candidate set.
    """
    n = sim_matrix.shape[0]
    mean_sims = sim_matrix.mean(axis=1)
    # Sort by mean similarity descending
    ranked = np.argsort(-mean_sims)
    top5 = ranked[:5]
    # Filter: keep only those with mean sim > 0.3 (loose threshold)
    candidates = [int(i) for i in top5 if mean_sims[i] > 0.3]
    return candidates if candidates else list(range(min(3, n)))


def _farthest_first_pick(
    candidates: list[int],
    sim_matrix: np.ndarray,
    k: int,
) -> list[int]:
    """Farthest-first traversal: pick k diverse embeddings from candidates.

    Greedy: each pick maximizes the minimum distance to previously-picked.
    """
    if len(candidates) <= k:
        return list(candidates)

    picked = [candidates[0]]
    remaining = list(candidates[1:])

    while len(picked) < k and remaining:
        # Find the remaining embedding farthest from all picked
        best_idx = 0
        best_min_dist = -1.0
        for ri, c in enumerate(remaining):
            min_dist = min(1.0 - sim_matrix[c, p] for p in picked)
            if min_dist > best_min_dist:
                best_min_dist = min_dist
                best_idx = ri
        picked.append(candidates[remaining[best_idx]])
        remaining.pop(best_idx)

    return picked


# ---------------------------------------------------------------------------
# Per-cam central pick + diversity
# ---------------------------------------------------------------------------


def _pick_from_crops(
    alerts: dict[str, list[Path]], max_final: int = 8
) -> tuple[list[Path], list[dict]]:
    """Find diverse person picks from recent crops.

    1. Embed all crops (one per alert).
    2. Pairwise cosine sim.
    3. Outlier filter (top-5 by mean sim).
    4. Per-cam central pick: for each cam with crops, pick the alert closest
       to the cam's mean embedding.
    5. Farthest-first diversity to max_final picks.

    Returns:
        (selected_paths, picks_info) where picks_info is a list of dicts with
        alert_id, cam, sim_mean, etc.
    """
    if not alerts:
        return [], []

    # Collect all paths and alert metadata
    entries: list[dict[str, Any]] = []
    for alert_id, crops in alerts.items():
        # Derive cam from parent dir name
        cam = crops[0].parent.parent.name
        entries.append({
            "alert_id": alert_id,
            "cam": cam,
            "crop_path": str(crops[0]),
        })

    paths = [e["crop_path"] for e in entries]
    embeddings = _embed_batch(paths)

    if embeddings.shape[0] <= 1:
        return paths, entries

    # Pairwise similarity
    sim_matrix = _pairwise_sim_matrix(embeddings)

    # Outlier filter
    candidate_indices = _outlier_filter(sim_matrix, embeddings)

    # Per-cam central pick: for each cam, pick the candidate closest to
    # the cam's mean embedding
    cam_groups: dict[str, list[int]] = {}
    for idx in candidate_indices:
        cam = entries[idx]["cam"]
        cam_groups.setdefault(cam, []).append(idx)

    # For each cam, compute mean embedding and pick closest candidate
    cam_picks: dict[str, int] = {}
    for cam, indices in cam_groups.items():
        cam_mean = embeddings[indices].mean(axis=0)
        best_idx = min(indices, key=lambda i: 1.0 - _cosine_sim(embeddings[i], cam_mean))
        cam_picks[cam] = best_idx

    # Farthest-first diversity across all candidates
    diversity_picks = _farthest_first_pick(candidate_indices, sim_matrix, max_final)

    # Ensure cam picks are represented
    cam_pick_set = set(cam_picks.values())
    final_picks = list(diversity_picks)
    for cp in cam_pick_set:
        if cp not in final_picks:
            final_picks.append(cp)

    selected_paths = [entries[i]["crop_path"] for i in final_picks]
    picks_info = [
        {
            "alert_id": entries[i]["alert_id"],
            "cam": entries[i]["cam"],
            "crop_path": entries[i]["crop_path"],
            "mean_sim": float(sim_matrix[i].mean()),
        }
        for i in final_picks
    ]

    return selected_paths, picks_info


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


def _enroll_selected(paths: list[str], dry_run: bool) -> None:
    """Enroll the selected crops as persons."""
    if not paths:
        print("No crops to enroll.")
        return

    for path in paths:
        # Derive a person_id from the crop path
        # Use the alert_id (parent dir name) as part of the id
        alert_id = Path(path).parent.name
        cam = Path(path).parent.parent.name
        person_id = f"{cam.lower()}__{alert_id}"

        try:
            from infra.farm_api.identities import enroll_known_person
        except ImportError:
            print("ERROR: infra.farm_api.identities.enroll_known_person not available", file=sys.stderr)
            sys.exit(1)

        try:
            entry = enroll_known_person(
                name=f"Person from {cam} alert {alert_id}",
                person_id=person_id,
                identity_markers=[cam.lower(), alert_id],
                image_paths=[path],
            )
            print(f"Enrolled: {entry['label']} (id={entry['id']})")
        except ValueError as exc:
            print(f"SKIP {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Batch-enroll persons from recent frame crops.",
    )
    parser.add_argument(
        "--since",
        default="24h",
        help="ISO timestamp or relative duration (e.g. '2h', '24h', '7d'). Default 24h ago.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print picks without writing.",
    )
    parser.add_argument(
        "--max-final",
        type=int,
        default=8,
        help="Max picks to enroll (default 8).",
    )
    args = parser.parse_args(argv)

    # Parse since
    now = datetime.datetime.now(datetime.UTC)
    since = _parse_since(args.since, now)

    print(f"Scanning for crops since {since.isoformat()} ...")

    # Find crops
    alerts = _find_crops_since(since)
    if not alerts:
        print("No crops found.")
        return

    print(f"Found {len(alerts)} alerts with crops.")

    # Pick diverse set
    selected_paths, picks_info = _pick_from_crops(alerts, max_final=args.max_final)

    print(f"\nPicks ({len(selected_paths)}):")
    for info in picks_info:
        print(f"  {info['cam']} {info['alert_id']} (mean_sim={info['mean_sim']:.3f}) -> {info['crop_path']}")

    if args.dry_run:
        print("\n[Dry run — not writing]")
        return

    # Enroll
    print("\nEnrolling ...")
    str_paths = [str(p) for p in selected_paths]
    _enroll_selected(str_paths, dry_run=False)


def _parse_since(since_str: str, now: datetime.datetime) -> datetime.datetime:
    """Parse a since-string: ISO timestamp or relative duration like '2h', '7d'."""
    # Try ISO first
    try:
        return datetime.datetime.fromisoformat(since_str)
    except ValueError:
        pass

    # Relative duration
    if since_str.endswith("h"):
        hours = int(since_str[:-1])
        return now - datetime.timedelta(hours=hours)
    elif since_str.endswith("d"):
        days = int(since_str[:-1])
        return now - datetime.timedelta(days=days)
    elif since_str.endswith("m"):
        minutes = int(since_str[:-1])
        return now - datetime.timedelta(minutes=minutes)

    raise ValueError(f"Cannot parse --since value: {since_str}")


if __name__ == "__main__":
    main()
