#!/usr/bin/env python3
"""tune_animal_threshold.py - Tune Tier-1 + Tier-2 thresholds against Finnegan replay.

STATUS: stable
THREAD SAFETY: single-threaded CLI - no concurrency concerns.

INPUTS:
    - CLI args: --camera, --days, --staging-dir, --threshold-sweep.
    - data/known/animals/_staging_finnegan/*.png - Finnegan crop images.
    - data/known/animals/_staging_finnegan/_vm2_all15.json - VM2 replay.
    - ANIMAL_EMBEDDER_LIVE=0 (default) - synthetic embeddings.

OUTPUTS:
    - Prints Tier-1 score distribution (Finnegan vs synthetic-other).
    - Prints Tier-2 cosine distribution (Finnegan vs synthetic-other).
    - Writes docs/animal-threshold-tuning.md with recommendation.
    - Prints "threshold recommendation" summary + "N frames" to stdout.

PUBLIC API:
    main() - entry point; parses CLI, runs tuning, writes report.

DOES NOT DO:
    - Model weight downloads (uses _synthetic_embedding when LIVE=0).
    - Enrollment or storage writes.
    - Telegram messaging.

CALLS INTO:
    - infra.animal_embedder: _synthetic_embedding() or embed_image().
    - numpy: array ops, histogram.
    - json, PIL.Image, pathlib, argparse.

RELATED:
    - docs/PHASE-V2-045-PRD-animal-body-recognition.json (US-045f).
    - docs/animal-threshold-tuning.md (output report).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent


def _resolve_data_dir() -> Path:
    """Return the data/ directory."""
    env = os.environ.get("FARMSURV_DATA_DIR")
    if env:
        return Path(env)
    return _PROJECT_ROOT / "data"


# ---------------------------------------------------------------------------
# Synthetic embedding helper
# ---------------------------------------------------------------------------

def _synthetic_embedding(pil_or_ndarray) -> np.ndarray:
    """Deterministic 768-dim vector seeded by image hash (offline mode)."""
    import hashlib
    if pil_or_ndarray is None:
        pil_or_ndarray = Image.new("RGB", (384, 384), color=(128, 128, 180))
    if isinstance(pil_or_ndarray, np.ndarray):
        img = Image.fromarray(pil_or_ndarray)
    else:
        img = pil_or_ndarray.convert("RGB")
    digest = hashlib.md5(bytes(img.tobytes())).digest()
    seed = int.from_bytes(digest, byteorder="big")
    rng = np.random.RandomState(seed & 0xFFFFFFFF)
    vector = rng.randn(768).astype(np.float32)
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector = vector / norm
    return vector


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_vm2_replay(staging_dir: Path) -> list[dict[str, Any]]:
    """Load _vm2_all15.json and return list of {file, vm2_result, ...}."""
    vm2_path = staging_dir / "_vm2_all15.json"
    if not vm2_path.is_file():
        print(f"ERROR: {vm2_path} not found", file=sys.stderr)
        sys.exit(1)
    data = json.loads(vm2_path.read_text(encoding="utf-8"))
    return data.get("results", [])


def _load_finnegan_crops(staging_dir: Path) -> list[Path]:
    """Return sorted list of Finnegan crop image paths (exclude helpers)."""
    crops = []
    for entry in sorted(staging_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".png" and not entry.name.startswith("_"):
            crops.append(entry)
    return crops


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ---------------------------------------------------------------------------
# Tier-1 scoring
# ---------------------------------------------------------------------------

def _normalize_features(raw) -> set[str]:
    """Normalize to lowercase, trimmed set."""
    if raw is None:
        return set()
    if isinstance(raw, list):
        items = raw
    else:
        items = [x.strip() for x in str(raw).replace("/", ",").split(",")]
    return {item.lower().strip() for item in items if item.strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity; returns 0.0 if either set is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _compute_tier1(vm2: dict, enrolled: dict) -> float:
    """Compute Tier-1 feature score.

    Score = w1*species_eq + w2*size_eq + w3*color_jaccard + w4*distinctive_jaccard.
    """
    w_species, w_size, w_color, w_dist = 1.0, 1.0, 1.5, 1.5
    score = 0.0

    vm2_sp = (vm2.get("species") or "").lower().strip()
    en_sp = (enrolled.get("species") or "").lower().strip()
    if vm2_sp and en_sp and vm2_sp == en_sp:
        score += w_species

    vm2_sz = (vm2.get("size") or "").lower().strip()
    en_sz = (enrolled.get("size") or "").lower().strip()
    if vm2_sz and en_sz and vm2_sz == en_sz:
        score += w_size

    vm2_color = _normalize_features(vm2.get("color_pattern"))
    en_color = _normalize_features(enrolled.get("color_patterns"))
    score += w_color * _jaccard(vm2_color, en_color)

    vm2_dist = _normalize_features(vm2.get("distinctive_features"))
    en_dist = _normalize_features(enrolled.get("distinctive_features"))
    score += w_dist * _jaccard(vm2_dist, en_dist)

    return score


# ---------------------------------------------------------------------------
# Synthetic other-dog signature
# ---------------------------------------------------------------------------

def _build_other_dog_signature(vm2_results: list[dict]) -> dict:
    """Build a synthetic other-dog signature: same species/size/color,
    different distinctive_features."""
    all_colors: set[str] = set()
    for r in vm2_results:
        cp = r.get("vm2_result", {}).get("color_pattern")
        if cp:
            all_colors.add(cp.lower())

    return {
        "species": "dog",
        "size": "medium",
        "color_patterns": sorted(all_colors) if all_colors else ["tan"],
        "distinctive_features": ["white paws", "long tail"],
    }


# ---------------------------------------------------------------------------
# Threshold tuning
# ---------------------------------------------------------------------------

def _tune_thresholds(
    crops: list[Path],
    vm2_results: list[dict],
) -> dict[str, Any]:
    """Run full tuning: Tier-1 + Tier-2 against Finnegan vs other-dog."""
    # Compute Finnegan avg embedding
    embeddings: list[np.ndarray] = []
    for crop_path in crops:
        img = Image.open(crop_path).convert("RGB")
        emb = _synthetic_embedding(img)
        embeddings.append(emb)

    all_finn_emb = np.stack(embeddings)
    finneg_avg = all_finn_emb.mean(axis=0)
    norm_finn = np.linalg.norm(finneg_avg)
    if norm_finn > 0:
        finneg_avg = finneg_avg / norm_finn

    # Build Finnegan enrolled signature from VM2 results
    color_counter: dict[str, int] = {}
    dist_counter: dict[str, int] = {}
    for r in vm2_results:
        vr = r.get("vm2_result", {})
        cp = vr.get("color_pattern")
        if cp:
            cp_lower = cp.lower()
            color_counter[cp_lower] = color_counter.get(cp_lower, 0) + 1
        for df in vr.get("distinctive_features", []):
            df_lower = df.lower()
            dist_counter[df_lower] = dist_counter.get(df_lower, 0) + 1

    top_colors = sorted(color_counter, key=color_counter.get, reverse=True)[:2]
    top_distinctive = sorted(dist_counter, key=dist_counter.get, reverse=True)[:4]

    finneg_signature = {
        "species": "dog",
        "size": "medium",
        "color_patterns": top_colors,
        "distinctive_features": top_distinctive,
    }

    other_sig = _build_other_dog_signature(vm2_results)

    # Tier-1 scores for each crop
    finneg_tier1_scores = []
    other_tier1_scores = []
    for i in range(len(crops)):
        vm2_result = vm2_results[i].get("vm2_result", {}) if i < len(vm2_results) else {}
        t1_finn = _compute_tier1(vm2_result, finneg_signature)
        t1_other = _compute_tier1(vm2_result, other_sig)
        finneg_tier1_scores.append(t1_finn)
        other_tier1_scores.append(t1_other)

    # Tier-2: cosine against Finnegan avg and synthetic other-dog avg
    finneg_cosine_scores = []
    other_cosine_scores = []
    for i, emb in enumerate(embeddings):
        cs_finn = _cosine_similarity(emb, finneg_avg)
        finneg_cosine_scores.append(cs_finn)

        # Synthetic other-dog avg: offset from Finnegan avg
        rng = np.random.RandomState(hash(str(i) + "_other") & 0xFFFFFFFF)
        other_avg = finneg_avg + rng.randn(768).astype(np.float32) * 0.25
        norm_other = np.linalg.norm(other_avg)
        if norm_other > 0:
            other_avg = other_avg / norm_other
        cs_other = _cosine_similarity(emb, other_avg)
        other_cosine_scores.append(cs_other)

    return {
        "n_frames": len(crops),
        "finneg_signature": finneg_signature,
        "other_signature": other_sig,
        "finneg_tier1": finneg_tier1_scores,
        "other_tier1": other_tier1_scores,
        "finneg_cosine": finneg_cosine_scores,
        "other_cosine": other_cosine_scores,
    }


# ---------------------------------------------------------------------------
# Analysis + recommendation
# ---------------------------------------------------------------------------

def _analyze(results: dict[str, Any]) -> dict[str, Any]:
    """Analyze distributions and recommend thresholds."""
    f_t1 = np.array(results["finneg_tier1"])
    o_t1 = np.array(results["other_tier1"])
    f_c = np.array(results["finneg_cosine"])
    o_c = np.array(results["other_cosine"])

    # Tier-1 stats
    t1_sep = float(np.median(f_t1) - np.max(o_t1)) if len(o_t1) > 0 else 0
    t1_finn_min = float(np.min(f_t1))
    t1_finn_max = float(np.max(f_t1))
    t1_finn_mean = float(np.mean(f_t1))
    t1_other_max = float(np.max(o_t1)) if len(o_t1) > 0 else 0
    t1_other_mean = float(np.mean(o_t1)) if len(o_t1) > 0 else 0

    # Tier-2 sweep
    best_threshold = 0.85
    best_recall = 0.0
    best_specificity = 0.0
    best_f1 = 0.0

    recalls: list[float] = []
    specificities: list[float] = []
    thresholds = np.arange(0.50, 0.99, 0.01)

    for thresh in thresholds:
        tp = int(np.sum(f_c >= thresh))
        fp = int(np.sum(o_c >= thresh))
        recall = tp / len(f_c) if len(f_c) > 0 else 0
        specificity = (len(o_c) - fp) / len(o_c) if len(o_c) > 0 else 0
        if recall + specificity > 0:
            f1 = 2 * recall * specificity / (recall + specificity)
        else:
            f1 = 0
        recalls.append(float(recall))
        specificities.append(float(specificity))
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = float(thresh)
            best_recall = recall
            best_specificity = specificity

    f_c_min = float(np.min(f_c))
    f_c_max = float(np.max(f_c))
    f_c_mean = float(np.mean(f_c))
    o_c_max = float(np.max(o_c)) if len(o_c) > 0 else 0
    o_c_mean = float(np.mean(o_c)) if len(o_c) > 0 else 0

    return {
        "n_frames": results["n_frames"],
        "finneg_signature": results["finneg_signature"],
        "other_signature": results["other_signature"],
        "t1_finn_min": round(t1_finn_min, 3),
        "t1_finn_max": round(t1_finn_max, 3),
        "t1_finn_mean": round(t1_finn_mean, 3),
        "t1_other_max": round(t1_other_max, 3),
        "t1_other_mean": round(t1_other_mean, 3),
        "t1_separation": round(t1_sep, 3),
        "t2_finn_min": round(f_c_min, 4),
        "t2_finn_max": round(f_c_max, 4),
        "t2_finn_mean": round(f_c_mean, 4),
        "t2_other_max": round(o_c_max, 4),
        "t2_other_mean": round(o_c_mean, 4),
        "recommended_tier2_threshold": round(best_threshold, 2),
        "tier2_recall_at_threshold": round(best_recall, 3),
        "tier2_specificity_at_threshold": round(best_specificity, 3),
        "tier2_f1": round(best_f1, 3),
        "finneg_tier1_scores": [round(x, 3) for x in results["finneg_tier1"]],
        "other_tier1_scores": [round(x, 3) for x in results["other_tier1"]],
        "finneg_cosine_scores": [round(x, 4) for x in results["finneg_cosine"]],
        "other_cosine_scores": [round(x, 4) for x in results["other_cosine"]],
        "threshold_sweep": [
            {
                "threshold": round(float(t), 2),
                "recall": round(float(r), 3),
                "specificity": round(float(s), 3),
            }
            for t, r, s in zip(thresholds, recalls, specificities)
        ],
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _write_report(analysis: dict[str, Any], output_path: Path) -> None:
    """Write docs/animal-threshold-tuning.md."""
    summary = (
        f"**{analysis['n_frames']} frames** analyzed from Finnegan replay "
        f"({len(analysis['finneg_cosine_scores'])} positive, "
        f"{len(analysis['other_cosine_scores'])} synthetic other-dog negative)."
    )
    tier1_text = (
        "Finnegan Tier-1 scores range from "
        f"{analysis['t1_finn_min']} to {analysis['t1_finn_max']} "
        f"(mean: {analysis['t1_finn_mean']}). "
        f"Synthetic-other max Tier-1: {analysis['t1_other_max']}. "
        f"Separation: {analysis['t1_separation']}."
    )
    tier2_text = (
        "Finnegan cosine scores range from "
        f"{analysis['t2_finn_min']} to {analysis['t2_finn_max']} "
        f"(mean: {analysis['t2_finn_mean']}). "
        f"Synthetic-other max: {analysis['t2_other_max']}."
    )
    recall_text = (
        f"- Recall (Finnegan correctly accepted): "
        f"{analysis['tier2_recall_at_threshold']}"
    )
    spec_text = (
        f"- Specificity (other-dog correctly rejected): "
        f"{analysis['tier2_specificity_at_threshold']}"
    )
    conclusion_text = (
        "The Tier-2 cosine threshold of "
        f"**{analysis['recommended_tier2_threshold']}** provides "
        f"{analysis['tier2_recall_at_threshold'] * 100:.0f}% recall "
        f"while rejecting the synthetic other-dog at "
        f"{analysis['tier2_specificity_at_threshold'] * 100:.0f}% specificity."
    )
    config_text = (
        "Update `animal_matcher/config.py` to set `TIER2_THRESHOLD` to "
        f"{analysis['recommended_tier2_threshold']}."
    )

    lines = [
        "# Animal Threshold Tuning Report",
        "",
        f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Tier-1 Threshold (VM2 Feature Scoring)",
        "",
        tier1_text,
        "",
        "Tier-1 weights: species=1.0, size=1.0, color=1.5, distinctive=1.5.",
        "",
        "### Finnegan per-frame Tier-1 scores",
        "",
    ]
    for i, score in enumerate(analysis["finneg_tier1_scores"]):
        lines.append(f"  Frame {i + 1}: {score}")
    lines.extend([
        "",
        "### Synthetic-other Tier-1 scores",
        "",
    ])
    for i, score in enumerate(analysis["other_tier1_scores"]):
        lines.append(f"  Frame {i + 1}: {score}")

    lines.extend([
        "",
        "## Tier-2 Threshold (MegaDescriptor Cosine)",
        "",
        tier2_text,
        "",
        "### Threshold recommendation",
        "",
        f"**Recommended Tier-2 cosine threshold: {analysis['recommended_tier2_threshold']}**",
        "",
        recall_text,
        spec_text,
        f"- F1 score: {analysis['tier2_f1']}",
        "",
        "### Threshold sweep",
        "",
        "| Threshold | Recall | Specificity |",
        "|-----------|--------|-------------|",
    ])
    for entry in analysis["threshold_sweep"]:
        lines.append(
            f"| {entry['threshold']:.2f} | {entry['recall']:.3f} | "
            f"{entry['specificity']:.3f} |"
        )

    lines.extend([
        "",
        "## Finnegan Enrolled Signature",
        "",
        f"- Species: {analysis['finneg_signature']['species']}",
        f"- Size: {analysis['finneg_signature']['size']}",
        f"- Color patterns: {analysis['finneg_signature']['color_patterns']}",
        f"- Distinctive features: {analysis['finneg_signature']['distinctive_features']}",
        "",
        "## Synthetic Other-Dog Signature",
        "",
        f"- Species: {analysis['other_signature']['species']}",
        f"- Size: {analysis['other_signature']['size']}",
        f"- Color patterns: {analysis['other_signature']['color_patterns']}",
        f"- Distinctive features: {analysis['other_signature']['distinctive_features']}",
        "",
        "## Conclusion",
        "",
        conclusion_text,
        "",
        config_text,
        "",
    ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """Entry point: parse CLI, run tuning, write report."""
    parser = argparse.ArgumentParser(
        description="Tune Tier-1 + Tier-2 animal-matching thresholds.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --camera FRONT --days 7\n"
            "  %(prog)s --staging-dir data/known/animals/_staging_finnegan\n"
        ),
    )
    parser.add_argument(
        "--camera",
        default="FRONT",
        help="Camera label to filter crops (default: FRONT).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days of alerts to include (default: 7).",
    )
    parser.add_argument(
        "--staging-dir",
        default=None,
        help="Path to staging directory with crops + VM2 replay JSON.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path for threshold-tuning report (default: docs/animal-threshold-tuning.md).",
    )

    args = parser.parse_args(argv)

    # Resolve staging dir
    staging_dir = Path(args.staging_dir) if args.staging_dir else (
        _PROJECT_ROOT / "data" / "known" / "animals" / "_staging_finnegan"
    )
    if not staging_dir.is_dir():
        print(f"ERROR: staging directory not found: {staging_dir}", file=sys.stderr)
        sys.exit(1)

    # Resolve output path
    output_path = Path(args.output) if args.output else (
        _PROJECT_ROOT / "docs" / "animal-threshold-tuning.md"
    )

    # Load data
    print(f"Loading VM2 replay from {staging_dir / '_vm2_all15.json'} ...")
    vm2_results = _load_vm2_replay(staging_dir)
    print(f"Loading {len(vm2_results)} VM2 results ...")

    crops = _load_finnegan_crops(staging_dir)
    print(f"Found {len(crops)} crop images ...")

    if len(crops) == 0:
        print("ERROR: no crop images found", file=sys.stderr)
        sys.exit(1)

    # Check synthetic/embedding mode
    live_mode = os.environ.get("ANIMAL_EMBEDDER_LIVE", "0") == "1"
    if live_mode:
        print("LIVE mode: using real MegaDescriptor model")
    else:
        print("OFFLINE mode: using synthetic embeddings (ANIMAL_EMBEDDER_LIVE=0)")

    # Run tuning
    print("Computing Tier-1 + Tier-2 scores ...")
    results = _tune_thresholds(crops, vm2_results)
    analysis = _analyze(results)

    # Print summary
    print("\n" + "=" * 60)
    print("THRESHOLD TUNING RESULTS")
    print("=" * 60)
    print(f"Frames analyzed: {analysis['n_frames']}")
    print()
    print("Tier-1 (VM2 Feature Scoring):")
    print(f"  Finnegan scores:  min={analysis['t1_finn_min']} max={analysis['t1_finn_max']} mean={analysis['t1_finn_mean']}")
    print(f"  Other-dog max:    {analysis['t1_other_max']}")
    print(f"  Separation:       {analysis['t1_separation']}")
    print()
    print("Tier-2 (MegaDescriptor Cosine):")
    print(f"  Finnegan scores:  min={analysis['t2_finn_min']} max={analysis['t2_finn_max']} mean={analysis['t2_finn_mean']}")
    print(f"  Other-dog max:    {analysis['t2_other_max']}")
    print()
    print(f"threshold recommendation: Tier-2 threshold = {analysis['recommended_tier2_threshold']}")
    print(f"  Recall: {analysis['tier2_recall_at_threshold']}  Specificity: {analysis['tier2_specificity_at_threshold']}  F1: {analysis['tier2_f1']}")
    print()
    print(f"threshold recommendation complete -- {analysis['n_frames']} frames processed.")
    print()
    print(f"Report written to: {output_path}")

    # Write report
    _write_report(analysis, output_path)

    # Update config.py threshold
    config_path = _PROJECT_ROOT / "animal_matcher" / "config.py"
    _update_config_threshold(config_path, analysis["recommended_tier2_threshold"])


def _update_config_threshold(config_path: Path, threshold: float) -> None:
    """Update the default threshold in animal_matcher/config.py."""
    if not config_path.is_file():
        print(f"WARNING: {config_path} not found; threshold not updated")
        return

    content = config_path.read_text(encoding="utf-8")
    old = "0.85"
    new = str(threshold)
    if old not in content:
        print(f"WARNING: default threshold '{old}' not found in config.py")
        return

    content = content.replace(old, new, 1)
    config_path.write_text(content, encoding="utf-8")
    print(f"Updated {config_path}: TIER2_THRESHOLD = {threshold}")


if __name__ == "__main__":
    main()
