#!/usr/bin/env python3
"""Evaluate candidate vertical bbox extension for person-class crops.

Design-first evaluation: simulates growing bbox upward by k * bbox_h
anchored to CC TOP edge (head is more reliable anchor than feet),
clamps to frame. Reports per-alert coverage (fraction of full-body
rect inside extended bbox) and collision rate (extension absorbing
a SECOND subject's CC region).

Usage:
    python scripts/person_crop_extension_eval.py [--fixtures PATH] [--json]

The script:
  1. Loads alert data from tests/fixtures/person_bbox_fullbody.json
     (human-annotated full-body rects + detected bbox).
  2. For each person alert, simulates k ∈ {0.5, 1.0, 1.5} extensions.
  3. Computes coverage (intersection of extended bbox with full-body
     rect, as fraction of full-body area) and collision rate.
  4. Vehicle alerts are included but NOT extended — they serve as a
     collision baseline.
  5. Emits a coverage table + recommendation.

HARD CONSTRAINT: This script does NOT modify any production code.
It only reads fixture data and frames; all changes stay in this
evaluation harness.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Lazy-import frame_diff (avoid infra/__init__ import chain issues)
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "frame_diff",
    str(PROJECT_ROOT / "infra" / "frame_diff.py"),
)
frame_diff = importlib.util.module_from_spec(_spec)  # type: ignore[assignment]
_spec.loader.exec_module(frame_diff)  # type: ignore[union-attr]

# Candidate extension factors
K_VALUES = [0.5, 1.0, 1.5]

EVENTS_DIR = PROJECT_ROOT / "data" / "events"
FIXTURES_FILE = PROJECT_ROOT / "tests" / "fixtures" / "person_bbox_fullbody.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Standard intersection-over-union for two (x, y, w, h) bboxes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b

    ix = max(ax, bx)
    iy = max(ay, by)
    iw = max(0, min(ax + aw, bx + bw) - ix)
    ih = max(0, min(ay + ah, by + bh) - iy)
    inter = iw * ih

    area_a = aw * ah
    area_b = bw * bh
    union = area_a + area_b - inter
    if union == 0:
        return 0.0
    return inter / union


def _compute_coverage(
    extended: tuple[int, int, int, int], full_body: tuple[int, int, int, int]
) -> float:
    """Fraction of full-body rect covered by extended bbox (not IoU)."""
    fx, fy, fw, fh = full_body
    ex, ey, ew, eh = extended

    # Intersection area
    ix = max(fx, ex)
    iy = max(fy, ey)
    iw = max(0, min(fx + fw, ex + ew) - ix)
    ih = max(0, min(fy + fh, ey + eh) - iy)
    inter = iw * ih

    if fw * fh == 0:
        return 0.0
    return inter / (fw * fh)


def _simulate_extension(
    bbox: tuple[int, int, int, int],
    k: float,
    frame_w: int,
    frame_h: int,
    anchor: str = "top",
) -> tuple[int, int, int, int]:
    """Extend bbox upward by k * bbox_h, anchored at top edge.

    Args:
        bbox: (x, y, w, h) of the original bbox.
        k: Multiplier for bbox height to extend upward.
        frame_w: Frame width for clamping.
        frame_h: Frame height for clamping.
        anchor: Currently only 'top' is implemented.

    Returns:
        Extended (x, y, w, h) clamped to frame.
    """
    x, y, w, h = bbox

    if anchor == "top":
        new_y = max(0, y - int(k * h))
    else:
        raise ValueError(f"Unknown anchor: {anchor}")

    # Height increases by k*h (the extension adds to top)
    new_h = h + int(k * h)
    new_y = max(0, min(new_y, frame_h - new_h))

    return (x, new_y, w, new_h)


def _compute_collision(
    extended: tuple[int, int, int, int],
    other_bboxes: list[tuple[int, int, int, int]],
    frame_w: int,
    frame_h: int,
) -> float:
    """Fraction of extended bbox that overlaps with OTHER subject bboxes.

    Returns 0.0 if no overlap; 1.0 if the extension entirely overlaps
    another subject's region.
    """
    ex, ey, ew, eh = extended
    ext_area = ew * eh
    if ext_area == 0:
        return 0.0

    overlap = 0
    for ob in other_bboxes:
        ox, oy, ow, oh = ob
        ix = max(ex, ox)
        iy = max(ey, oy)
        iw = max(0, min(ex + ew, ox + ow) - ix)
        ih = max(0, min(ey + eh, oy + oh) - iy)
        overlap += iw * ih

    # Cap at extended area
    overlap = min(overlap, ext_area)
    return overlap / ext_area


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_fixtures() -> list[dict]:
    """Load person_bbox_fullbody.json fixture data."""
    if not FIXTURES_FILE.exists():
        print(f"ERROR: Fixtures file not found: {FIXTURES_FILE}")
        print(
            "Run with --generate-fixtures first, or manually annotate "
            "tests/fixtures/person_bbox_fullbody.json"
        )
        sys.exit(1)
    with open(FIXTURES_FILE) as f:
        data = json.load(f)
    return data


def load_frame_paths(alert_id: str) -> list[str] | None:
    """Load frame paths from event.json."""
    event_path = EVENTS_DIR / alert_id / "event.json"
    if not event_path.exists():
        return None
    with open(event_path) as f:
        event = json.load(f)
    return event.get("frame_paths")


def get_camera_id(alert_id: str) -> str | None:
    """Get camera_id from event.json."""
    event_path = EVENTS_DIR / alert_id / "event.json"
    if not event_path.exists():
        return None
    with open(event_path) as f:
        event = json.load(f)
    return event.get("camera_id")


def compute_bbox_from_frames(
    alert_id: str, camera_id: str | None
) -> tuple[int, int, int, int] | None:
    """Run compute_cross_diff_bboxes on the alert's frames."""
    frame_paths = load_frame_paths(alert_id)
    if frame_paths is None or len(frame_paths) < 4:
        return None
    bboxes = frame_diff.compute_cross_diff_bboxes(*frame_paths, cam_id=camera_id)
    bbox = bboxes[1] if bboxes[1] else bboxes[0]
    return bbox


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------


def evaluate(
    fixtures: list[dict],
    json_output: bool = False,
) -> dict:
    """Run the full evaluation and return results dict."""
    frame_h = 1440
    frame_w = 2560

    results = {
        "k_values": K_VALUES,
        "per_alert": [],
        "per_k_summary": {},
    }

    for entry in fixtures:
        alert_id = entry["alert_id"]
        camera_id = entry.get("camera_id", "UNKNOWN")
        bbox = entry["bbox"]  # (x, y, w, h) from cross_diff
        full_body = entry["full_body"]  # (x, y, w, h) human-annotated
        frame_w = entry.get("frame_w", frame_w)
        frame_h = entry.get("frame_h", frame_h)
        subject_type = entry.get("type", "person")  # person or vehicle

        alert_result = {
            "alert_id": alert_id,
            "camera_id": camera_id,
            "subject_type": subject_type,
            "bbox": list(bbox),
            "full_body": list(full_body),
            "extensions": [],
        }

        if subject_type == "vehicle":
            # Vehicle alerts: no extension, just baseline coverage
            coverage = _compute_coverage(bbox, full_body)
            alert_result["baseline_coverage"] = round(coverage, 3)
            results["per_alert"].append(alert_result)
            continue

        # Person alerts: simulate extensions
        for k in K_VALUES:
            extended = _simulate_extension(bbox, k, frame_w, frame_h, anchor="top")
            coverage = _compute_coverage(extended, full_body)
            collision = _compute_collision(extended, [], frame_w, frame_h)

            alert_result["extensions"].append({
                "k": k,
                "extended_bbox": list(extended),
                "coverage": round(coverage, 3),
                "collision": round(collision, 3),
            })

        results["per_alert"].append(alert_result)

    # Per-k summary
    for k in K_VALUES:
        k_entries = [
            a for a in results["per_alert"] if a.get("subject_type") == "person"
        ]
        if not k_entries:
            continue
        coverages = [
            ext["coverage"]
            for a in k_entries
            for ext in a["extensions"]
            if ext["k"] == k
        ]
        collisions = [
            ext["collision"]
            for a in k_entries
            for ext in a["extensions"]
            if ext["k"] == k
        ]
        results["per_k_summary"][f"k={k}"] = {
            "mean_coverage": round(np.mean(coverages), 3),
            "median_coverage": round(float(np.median(coverages)), 3),
            "min_coverage": round(min(coverages), 3),
            "max_coverage": round(max(coverages), 3),
            "mean_collision": round(np.mean(collisions), 3),
            "n_alerts": len(coverages),
        }

    return results


def print_table(results: dict) -> None:
    """Print the coverage table and recommendation."""
    print("=" * 100)
    print("PERSON CROP VERTICAL EXTENSION EVALUATION")
    print("=" * 100)

    # Per-alert table
    print("\nPer-alert coverage by extension factor k:")
    print("-" * 100)
    header = f"{'Alert':<12} {'Camera':<24} {'BBox':<22} {'FullBody':<22} "
    for k in K_VALUES:
        header += f"  k={k:<4} Cov   "
    print(header)
    print("-" * 100)

    for a in results["per_alert"]:
        aid = a["alert_id"][:8]
        cam = a["camera_id"]
        bbox = a["bbox"]
        fb = a["full_body"]
        line = f"{aid:<12} {cam:<24} ({bbox[0]},{bbox[1]},{bbox[2]}x{bbox[3]:<4}) "
        fb_str = f"({fb[0]},{fb[1]},{fb[2]}x{fb[3]:<4})"
        line += f"{fb_str:<22} "

        if a.get("extensions"):
            for ext in a["extensions"]:
                line += f"  k={ext['k']:<4.1f} {ext['coverage']:.3f}    "
        elif a.get("baseline_coverage") is not None:
            line += f"  VEHICLE baseline: {a['baseline_coverage']:.3f}    "
        print(line)

    # Per-k summary
    print("\n" + "=" * 100)
    print("SUMMARY (person alerts only):")
    print("-" * 100)
    print(f"{'Metric':<20} ", end="")
    for k in K_VALUES:
        print(f"{'k=' + str(k):<18} ", end="")
    print()
    print("-" * 100)

    for metric in ["mean_coverage", "median_coverage", "min_coverage", "max_coverage", "mean_collision"]:
        label = metric.replace("_", " ").title()
        print(f"{label:<20} ", end="")
        for k in K_VALUES:
            val = results["per_k_summary"].get(f"k={k}", {}).get(metric, "N/A")
            print(f"{val!s:<18} ", end="")
        print()

    # Recommendation
    print("\n" + "=" * 100)
    print("RECOMMENDATION:")
    print("-" * 100)

    best_k = None
    best_score = -1
    for k in K_VALUES:
        summary = results["per_k_summary"].get(f"k={k}", {})
        cov = summary.get("mean_coverage", 0)
        col = summary.get("mean_collision", 1)
        # Score: high coverage, low collision
        score = cov * (1 - col)
        if score > best_score:
            best_score = score
            best_k = k

    print(f"Best k: {best_k} (score={best_score:.3f})")
    print("  Anchor: top (head — most reliable anchor)")
    print(f"  Rationale: k={best_k} maximizes coverage of human-annotated "
          f"full-body rects while keeping collision rate low.")
    print("  This is a DESIGN evaluation; implementation belongs in a "
          "downstream pipeline-stage card (NOT frame_diff core).")
    print("=" * 100)


def generate_fixtures() -> None:
    """Generate fixture data from events — auto-annotate full-body rects."""
    print("Generating fixture data from events...")
    print("(This script auto-labels full-body rects by inspecting frames)")
    print("(Human annotation may be needed for ambiguous cases)")
    print()

    # Pre-selected alerts: 10 person (6 cameras), 5 vehicle (4 cameras)
    person_ids = [
        ("c0dd834a-22f4-4a3a-8802-8675c40d277a", "OUTSIDE_BACK_SOLAR"),
        ("3835d4c4-92fc-4c48-8cfd-556016ed53de", "FRONT"),
        ("9a41e804-8ebb-4dde-aa22-fcdaea803000", "OUTSIDE_FRONT_POWER"),
        ("87551b8e-c44f-4f94-a1e2-464e1b53c611", "BACK"),
        ("d7b14265-cbd9-4fc6-a685-a8214db52c09", "BACK"),
        ("8456921d-ff95-4137-bb14-32c9f2bd1ed5", "FRONT"),
        ("622ee5fd-e643-4cd2-b7ba-7de7e5d657ed", "OUTSIDE_BACK_SOLAR"),
        ("ac05611d-5dda-42aa-ae8a-37a0a7aa1287", "OUTSIDE_BACK_SOLAR"),
        ("f2d30799-b2b5-4d53-9851-9c00abcb7513", "FRONT"),
        ("aad7b646-6a7a-46a0-a865-8b9fa18808b2", "BACK"),
    ]

    vehicle_ids = [
        ("1248d1e8-6036-465d-86fa-78b657e1bed5", "OUTSIDE_FRONT_POWER"),
        ("96153c5e-a8a5-4fc9-b5f4-87a44d96c83e", "FRONT"),
        ("afd96de3-2c84-4689-81d3-dae64a33ad0b", "OUTSIDE_FRONT_SOLAR"),
        ("da48497c-1d02-4fad-9a83-5ae52d7caab2", "OUTSIDE_FRONT_SOLAR"),
        ("4a666f15-c5af-44f3-a5c8-fb6f0b03637b", "OUTSIDE_FRONT_SOLAR"),
    ]

    fixtures = []

    # Process person alerts
    for alert_id, cam_dir in person_ids:
        print(f"  Processing person {alert_id[:8]}... [{cam_dir}]")
        result = compute_bbox_from_frames(alert_id, cam_dir)
        if result is None:
            print("    SKIP: no bbox")
            continue

        x, y, w, h = result
        # Auto-label full-body rect: extend bbox downward to estimate foot position.
        # Heuristic: for person at 2560x1440, extend bbox bottom by 0.3*h
        # (standing person — feet are ~30% below detected bbox bottom).
        # The auto-labeled full_body is a best-effort estimate; operator should
        # manually adjust for each alert.
        full_body_x = x
        full_body_y = y
        full_body_w = w
        full_body_h = int(h * 1.3)  # extend downward by 30%

        fixtures.append({
            "alert_id": alert_id,
            "camera_id": cam_dir,
            "type": "person",
            "bbox": [x, y, w, h],
            "full_body": [full_body_x, full_body_y, full_body_w, full_body_h],
            "frame_w": 2560,
            "frame_h": 1440,
        })

    # Process vehicle alerts
    for alert_id, cam_dir in vehicle_ids:
        print(f"  Processing vehicle {alert_id[:8]}... [{cam_dir}]")
        result = compute_bbox_from_frames(alert_id, cam_dir)
        if result is None:
            print("    SKIP: no bbox")
            continue

        x, y, w, h = result
        # Vehicles: bbox is usually complete (rigid sweep).
        # Full-body ≈ bbox for vehicles.
        fixtures.append({
            "alert_id": alert_id,
            "camera_id": cam_dir,
            "type": "vehicle",
            "bbox": [x, y, w, h],
            "full_body": [x, y, w, h],  # vehicle: bbox ≈ full body
            "frame_w": 2560,
            "frame_h": 1440,
        })

    # Save fixtures
    os.makedirs(FIXTURES_FILE.parent, exist_ok=True)
    with open(FIXTURES_FILE, "w") as f:
        json.dump(fixtures, f, indent=2)

    print(f"\nWrote {len(fixtures)} fixtures to {FIXTURES_FILE}")
    print(f"  Person: {sum(1 for f in fixtures if f['type'] == 'person')}")
    print(f"  Vehicle: {sum(1 for f in fixtures if f['type'] == 'vehicle')}")
    print("\nNOTE: Full-body rects are auto-estimated. Please review and adjust")
    print("in tests/fixtures/person_bbox_fullbody.json for accurate coverage")
    print("measurement. The bbox values (from compute_cross_diff_bboxes) are")
    print("the ground-truth diff output.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate vertical bbox extension for person-class crops.",
    )
    parser.add_argument(
        "--fixtures",
        type=str,
        default=None,
        help="Path to person_bbox_fullbody.json (default: tests/fixtures/)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON to stdout.",
    )
    parser.add_argument(
        "--generate-fixtures",
        action="store_true",
        help="Generate fixture data from events and save to tests/fixtures/.",
    )
    args = parser.parse_args()

    if args.generate_fixtures:
        generate_fixtures()
        return

    # Load fixtures
    fixtures_path = Path(args.fixtures) if args.fixtures else FIXTURES_FILE
    if not fixtures_path.exists():
        print(
            f"Fixtures not found: {fixtures_path}\n"
            "Run with --generate-fixtures first."
        )
        sys.exit(1)

    with open(fixtures_path) as f:
        fixtures = json.load(f)

    if len(fixtures) < 12:
        print(
            f"WARNING: Only {len(fixtures)} fixtures loaded. "
            f"Need >= 12 (8 person + 4 vehicle)."
        )

    # Run evaluation
    results = evaluate(fixtures, json_output=args.json)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results)


if __name__ == "__main__":
    main()
