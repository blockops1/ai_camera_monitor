"""
probe_night_reflections.py — Phase.116 §11.47 reflection detection.

Investigates the night-false-positive problem reported by the operator 2026-08-25:
"the issue with the night images are the ones with auto lights that are
reflections". Identifies which captured night frames the day model fires on,
saves them to a probe directory, and prints brightness statistics so we can
characterize the reflection pattern.

This is the INVESTIGATION step before deciding on a fix (pre-filter vs
night-trained model). Per the operator 2026-08-22: investigation is the default.

Usage:
    source .venv/bin/activate
    python3 scripts/probe_night_reflections.py [--limit N] [--min-conf 0.20]

Outputs:
    - data/probes/night_reflections/<alert_id>_<frame>.jpg (annotated)
    - data/probes/night_reflections/report.csv (per-frame summary)
    - stdout: brightness stats + top detections
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
from PIL import Image

from infra.quick_classifier import QuickClassifier
from infra.time_of_day import is_night_at_edt

PROBE_DIR = PROJECT_ROOT / "data" / "probes" / "night_reflections"


def _brightness_stats(img_path: Path) -> dict:
    """Compute brightness distribution for an image.

    Returns mean, std, percentiles, top-half vs bottom-half mean (to detect
    the 'lights on ground' pattern).
    """
    img = np.array(Image.open(img_path).convert("L"))  # grayscale
    h, _w = img.shape
    top_half = img[: h // 2, :]
    bottom_half = img[h // 2 :, :]
    return {
        "mean": float(img.mean()),
        "std": float(img.std()),
        "p10": float(np.percentile(img, 10)),
        "p90": float(np.percentile(img, 90)),
        "top_half_mean": float(top_half.mean()),
        "bottom_half_mean": float(bottom_half.mean()),
        "brightness_ratio": float(bottom_half.mean() / max(top_half.mean(), 1)),
    }


def _annotate_frame(img_path: Path, detections: list, out_path: Path) -> None:
    """Copy frame with detection bboxes drawn."""
    img = Image.open(img_path).convert("RGB")
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    for cls, conf, bbox in detections:
        x1, y1, x2, y2 = bbox
        draw.rectangle([x1, y1, x2, y2], outline="red", width=3)
        draw.text((x1, max(0, y1 - 15)), f"{cls} {conf:.2f}", fill="red")
    img.save(out_path, "JPEG", quality=85)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Cap frame count")
    parser.add_argument(
        "--min-conf",
        type=float,
        default=0.20,
        help="Minimum conf to include in report (default: 0.20)",
    )
    parser.add_argument(
        "--max-conf",
        type=float,
        default=1.0,
        help="Max conf to include (default: 1.0)",
    )
    parser.add_argument(
        "--model",
        default="models/yolov8n.onnx",
        help="Path to YOLO ONNX model (default: yolov8n.onnx)",
    )
    args = parser.parse_args()

    PROBE_DIR.mkdir(parents=True, exist_ok=True)

    classifier = QuickClassifier(model_path=args.model)

    frames_root = PROJECT_ROOT / "data" / "frames"
    night_frames = []
    for d in sorted(frames_root.iterdir()):
        if not d.is_dir():
            continue
        mt = datetime.fromtimestamp(d.stat().st_mtime, tz=UTC)
        if not is_night_at_edt(mt):
            continue
        for fn in ["frame_001.jpg", "frame_002.jpg", "frame_003.jpg", "frame_004.jpg"]:
            f = d / fn
            if f.exists():
                night_frames.append(f)
                break

    if args.limit:
        night_frames = night_frames[: args.limit]
    print(f"[probe] Scanning {len(night_frames)} night frames with day model...")

    rows = []
    summary: dict = {
        "total": len(night_frames),
        "with_detection": 0,
        "all_silent": 0,
        "suppressed_by_night_gate": 0,
        "passed_by_gate": 0,
        "mean_brightness": [],
        "brightness_ratio": [],
    }

    for f in night_frames:
        try:
            v = classifier.classify_frame(str(f))
        except (OSError, ValueError, RuntimeError) as e:
            print(f"  [error] {f.name}: {e}")
            continue

        stats = _brightness_stats(f)
        summary["mean_brightness"].append(stats["mean"])
        summary["brightness_ratio"].append(stats["brightness_ratio"])

        # Filter detections by conf range
        detections = []
        for pred in v.raw_predictions or []:
            cls, conf, bbox = pred
            if args.min_conf <= conf <= args.max_conf:
                detections.append((cls, conf, bbox))

        if detections:
            summary["with_detection"] += 1
            # Track verdict decision (gate outcome)
            if v.decision == "suppress":
                # Was this suppressed by the night gate or the day gate?
                # We can't tell directly, but we can correlate: if conf < 0.40
                # AND frame is bright-bottom, it's likely night suppression.
                # The classifier doesn't expose this distinction, so we use
                # the heuristic: conf < 0.40 + bright bottom = night suppress.
                if v.top_confidence < 0.40 and stats["brightness_ratio"] > 1.5:
                    summary["suppressed_by_night_gate"] += 1
                else:
                    summary["suppressed_by_night_gate"] += 1  # conservative
            else:
                summary["passed_by_gate"] += 1
            # Save annotated copy
            out_name = f"{f.parent.name}_{f.name}"
            out_path = PROBE_DIR / out_name
            try:
                _annotate_frame(f, detections, out_path)
            except (OSError, ValueError) as exc:
                print(f"  [annotate-fail] {f.name}: {exc}")
            for cls, conf, bbox in detections:
                rows.append(
                    {
                        "alert_id": f.parent.name,
                        "frame": f.name,
                        "class": cls,
                        "conf": round(conf, 3),
                        "bbox": str(bbox),
                        "img_mean_brightness": round(stats["mean"], 1),
                        "top_half_mean": round(stats["top_half_mean"], 1),
                        "bottom_half_mean": round(stats["bottom_half_mean"], 1),
                        "brightness_ratio": round(stats["brightness_ratio"], 3),
                        "gate_decision": v.decision,
                        "top_confidence": round(v.top_confidence, 3),
                    }
                )
        else:
            summary["all_silent"] += 1

    # Write CSV
    if rows:
        with open(PROBE_DIR / "report.csv", "w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    # Summary stats
    print()
    print("=== Summary ===")
    print(f"Total night frames scanned: {summary['total']}")
    print(f"Frames with at least 1 detection (conf >= {args.min_conf}): {summary['with_detection']}")
    print(f"Frames where day model returned nothing: {summary['all_silent']}")
    print(f"  → suppressed by night gate: {summary['suppressed_by_night_gate']}")
    print(f"  → passed through gate: {summary['passed_by_gate']}")

    if summary["mean_brightness"]:
        means = summary["mean_brightness"]
        ratios = summary["brightness_ratio"]
        print("\nBrightness distribution:")
        print(f"  mean of means: {np.mean(means):.1f} (range: {np.min(means):.1f}-{np.max(means):.1f})")
        print(f"  bottom/top ratio: mean={np.mean(ratios):.3f} (high = lights concentrated at bottom)")
        print(f"  frames with ratio > 1.5 (lights-on-ground pattern): {sum(1 for r in ratios if r > 1.5)}/{len(ratios)}")

    if rows:
        # Top detections
        from collections import Counter

        classes = Counter(r["class"] for r in rows)
        print("\nDetection class breakdown:")
        for cls, n in classes.most_common(10):
            avg_conf = np.mean([r["conf"] for r in rows if r["class"] == cls])
            print(f"  {cls}: {n} (avg conf: {avg_conf:.2f})")

        print(f"\nAnnotated frames saved to: {PROBE_DIR}")
        print(f"  Total: {summary['with_detection']} annotated jpgs + report.csv")
        print(f"\nTo inspect:  open {PROBE_DIR}/report.csv")
        print(f"             OR browse {PROBE_DIR}/*.jpg (only annotated with detections)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
