"""
extract_night_training_candidates.py — Phase.116 §11.47 labeling pipeline.

Identifies night surveillance frames that are good candidates for manual
labeling (Roboflow), copies them to a staging directory, and writes a
manifest CSV. Goal: produce a labeled corpus of ~100-200 real IR night
frames so we can fine-tune yolov8n specifically for our cameras.

Run:
    .venv/bin/python scripts/extract_night_training_candidates.py --days 7

Filtering strategy (matches what we measured as FP-prone):
- Only night frames (is_night_at_edt)
- Per-camera dedup (don't drown in one camera's noise)
- Include both "suppressed" and "passed" frames so the model sees the full distribution
- Optional: prioritize frames with existing crops (those had motion detection candidates)

Output:
- data/training_corpus/yolov8n_night/candidates/<alert_id>_<frame_idx>.jpg
- data/training_corpus/yolov8n_night/candidates/manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from infra.time_of_day import is_night_at_edt


def _load_camera_map() -> dict[str, str]:
    """Build alert_id -> camera mapping from data/alerts/*.jsonl."""
    alert_to_camera: dict[str, str] = {}
    alerts_dir = _ROOT / "data" / "alerts"
    if not alerts_dir.is_dir():
        return alert_to_camera
    import json
    for jsonl in sorted(alerts_dir.glob("*.jsonl")):
        try:
            with open(jsonl) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        aid = e.get("alert_id", "").split("-identified")[0]  # strip suffix
                        cam = e.get("camera")
                        if aid and cam:
                            alert_to_camera[aid] = cam
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError:
            continue
    return alert_to_camera


def find_night_frames(days: int, max_per_camera: int = 50) -> list[dict]:
    """Find night frames in data/frames/ from the past N days."""
    cutoff = datetime.now(tz=UTC).timestamp() - (days * 86400)
    frames_dir = _ROOT / "data" / "frames"
    if not frames_dir.is_dir():
        return []

    camera_map = _load_camera_map()
    print(f"[extract] Loaded camera names for {len(camera_map)} alerts")

    by_camera: dict[str, list[dict]] = defaultdict(list)
    for alert_dir in frames_dir.iterdir():
        if not alert_dir.is_dir():
            continue
        # mtime of dir ~ when alert fired
        mt = alert_dir.stat().st_mtime
        if mt < cutoff:
            continue
        utc = datetime.fromtimestamp(mt, tz=UTC)
        if not is_night_at_edt(utc):
            continue
        camera = camera_map.get(alert_dir.name, "unknown")
        for fn in ("frame_001.jpg", "frame_002.jpg", "frame_003.jpg", "frame_004.jpg"):
            f = alert_dir / fn
            if f.exists():
                by_camera[camera].append({
                    "path": f,
                    "alert_id": alert_dir.name,
                    "frame_idx": fn,
                    "camera": camera,
                    "mtime": utc.isoformat(),
                    "has_crops": any(alert_dir.glob("*_crop*.jpg")),
                })

    # Take up to max_per_camera per camera
    result = []
    for camera, frames in by_camera.items():
        # Sort by has_crops (True first), then by recency
        frames.sort(key=lambda x: (not x["has_crops"], x["mtime"]), reverse=True)
        result.extend(frames[:max_per_camera])

    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="How many days back to scan")
    ap.add_argument("--max-per-camera", type=int, default=50)
    ap.add_argument("--output-dir", type=Path,
                    default=_ROOT / "data" / "training_corpus" / "yolov8n_night" / "candidates")
    args = ap.parse_args()

    print(f"[extract] Scanning data/frames/ for night frames from past {args.days} days...")
    frames = find_night_frames(args.days, args.max_per_camera)
    print(f"[extract] Found {len(frames)} candidate frames")

    if not frames:
        print("[extract] No candidates found. Aborting.")
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = []
    for entry in frames:
        src: Path = entry["path"]
        # Filename: <alert_id>_<frame_idx>.jpg — keeps traceability back to source alert
        dst = args.output_dir / f"{entry['alert_id']}_{entry['frame_idx']}"
        if not dst.exists():
            shutil.copy2(src, dst)
        manifest_rows.append({
            "filename": dst.name,
            "alert_id": entry["alert_id"],
            "frame_idx": entry["frame_idx"],
            "camera": entry["camera"],
            "mtime": entry["mtime"],
            "has_crops": entry["has_crops"],
            "source_path": str(src),
        })

    # Write manifest
    manifest_path = args.output_dir / "manifest.csv"
    with open(manifest_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()))
        writer.writeheader()
        writer.writerows(manifest_rows)

    print(f"[extract] Copied {len(manifest_rows)} frames to {args.output_dir}")
    print(f"[extract] Manifest: {manifest_path}")
    print()
    # Per-camera breakdown
    from collections import Counter
    cam_counts = Counter(r["camera"] for r in manifest_rows)
    print("[extract] Per-camera breakdown:")
    for cam, n in cam_counts.most_common():
        print(f"  {cam}: {n} frames")
    print()
    print("Next steps:")
    print("  1. Open Roboflow (https://roboflow.com) — create a free project 'farm-night-vehicles'")
    print("  2. Upload all images from the candidates/ directory")
    print(f"     ({args.output_dir})")
    print("  3. Label each frame with bounding boxes for real vehicles/people/animals")
    print("     (ignore noise — reflections, trees, edge artifacts)")
    print("  4. Export as YOLOv8 format (zip)")
    print("  5. Drop the zip into data/training_corpus/yolov8n_night/labeled/")
    print("  6. Run scripts/train_yolov8n_night.py --labeled-only --epochs 50")
    return 0


if __name__ == "__main__":
    sys.exit(main())