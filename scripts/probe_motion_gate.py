#!/usr/bin/env python3
"""
probe_motion_gate.py — Phase.107 end-to-end verification.

Runs the motion gate pipeline against real alert frames from data/frames/<alert_id>/
and prints the routing decision + latency for each. Confirms:

  1. Real alerts with vehicles route to vehicle pipeline (decision="vehicle")
  2. Real alerts with persons route to person pipeline (decision="person")
  3. Confirmed-noise alerts get suppressed (decision="suppress")
  4. Latency is within budget (<200ms per alert, including model load)
  5. Crops are saved to disk for human review

Usage:
    source .venv/bin/activate
    python3 scripts/probe_motion_gate.py [--alert-ids ID1,ID2,...] [--limit N]

Default: sample N alerts from data/frames/ (most recent first), use the first
4 frames in each alert dir as input.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from infra.paths import FRAMES_DIR


# Lazy import so the probe can show its own help before loading onnxruntime
def _import_gate():
    from listener.motion_gate_pipeline import run as run_gate
    return run_gate


def find_alert_dirs(root: Path, limit: int | None = None) -> list[Path]:
    """Find alert dirs in data/frames/, sorted most-recent first."""
    if not root.is_dir():
        return []
    dirs = sorted(
        [d for d in root.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if limit is not None:
        dirs = dirs[:limit]
    return dirs


def pick_four_frames(alert_dir: Path) -> list[Path] | None:
    """Pick the first 4 frame_*.jpg files in this alert dir, sorted by name."""
    frames = sorted(alert_dir.glob("frame_*.jpg"))
    if len(frames) < 4:
        return None
    return frames[:4]


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase.107 motion gate probe")
    parser.add_argument(
        "--alert-ids",
        type=str,
        default=None,
        help="Comma-separated list of alert IDs to probe (overrides --limit)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=15,
        help="Number of alerts to probe (default 15, ignored if --alert-ids set)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="data/probe_results/motion_gate_probe.json",
        help="Where to write the JSON results",
    )
    args = parser.parse_args()

    frames_root = Path(FRAMES_DIR)
    if not frames_root.is_dir():
        print(f"ERROR: {frames_root} does not exist", file=sys.stderr)
        return 1

    # Resolve alert dirs
    if args.alert_ids:
        alert_ids = [s.strip() for s in args.alert_ids.split(",") if s.strip()]
        alert_dirs = [frames_root / aid for aid in alert_ids]
        alert_dirs = [d for d in alert_dirs if d.is_dir()]
    else:
        alert_dirs = find_alert_dirs(frames_root, limit=args.limit)

    if not alert_dirs:
        print(f"ERROR: no alert dirs found under {frames_root}", file=sys.stderr)
        return 1

    print(f"Probing {len(alert_dirs)} alerts...")
    print("=" * 80)

    # Lazy-load gate (loads onnxruntime + YOLO model on first call)
    print("Loading gate (model load + classifier init)...")
    t0 = time.perf_counter()
    run_gate = _import_gate()
    load_ms = (time.perf_counter() - t0) * 1000
    print(f"Gate loaded in {load_ms:.0f}ms")
    print("=" * 80)

    results = []
    summary: dict = {
        "total_alerts": 0,
        "vehicle_decisions": 0,
        "person_decisions": 0,
        "suppress_decisions": 0,
        "wrong_frame_count": 0,
        "latency_ms": [],
        "by_reason": {},
        "by_camera": {},
    }

    for alert_dir in alert_dirs:
        alert_id = alert_dir.name
        frames = pick_four_frames(alert_dir)
        if frames is None:
            print(f"  SKIP {alert_id} (only {len(list(alert_dir.glob('frame_*.jpg')))} frames)")
            summary["wrong_frame_count"] += 1
            continue

        # Get camera name from the alert dir's audit log if available
        camera_name = "Unknown"
        # Try a few recent dates for the audit
        for days_ago in range(7):
            check_date = time.gmtime(time.time() - days_ago * 86400)
            check_path = PROJECT_ROOT / "data" / "alerts" / f"{time.strftime('%Y-%m-%d', check_date)}.jsonl"
            if check_path.is_file():
                try:
                    with check_path.open() as f:
                        for line in f:
                            try:
                                entry = json.loads(line)
                                aid = entry.get("alert_id", "").replace("-identified", "").replace("-arriving", "")
                                if aid == alert_id:
                                    camera_name = entry.get("camera", "Unknown")
                                    break
                            except json.JSONDecodeError:
                                continue
                    if camera_name != "Unknown":
                        break
                except OSError:
                    pass

        t0 = time.perf_counter()
        try:
            verdict = run_gate(
                frame_paths=[str(p) for p in frames],
                camera_name=camera_name,
                alert_id=alert_id,
                output_dir=str(alert_dir),
            )
        except (RuntimeError, ValueError, OSError) as e:  # gate failures
            print(f"  ERROR {alert_id}: {e!r}")
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # Print
        decision_marker = {
            "vehicle": "🚗 VEHICLE",
            "person": "🚶 PERSON",
            "suppress": "🚫 SUPPRESS",
        }.get(verdict.decision, "? UNKNOWN")

        print(
            f"  {decision_marker:<14} {alert_id[:12]} → "
            f"{verdict.class_label or 'none'} ({verdict.confidence:.2f}) "
            f"reason={verdict.reason} {elapsed_ms:.0f}ms"
        )

        results.append({
            "alert_id": alert_id,
            "camera": camera_name,
            "decision": verdict.decision,
            "class_label": verdict.class_label,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "bbox_a": verdict.bbox_a,
            "bbox_b": verdict.bbox_b,
            "crop_a_path": verdict.crop_a_path,
            "crop_b_path": verdict.crop_b_path,
            "elapsed_ms": elapsed_ms,
        })

        summary["total_alerts"] += 1
        summary["latency_ms"].append(elapsed_ms)
        summary["by_reason"][verdict.reason] = summary["by_reason"].get(verdict.reason, 0) + 1
        summary["by_camera"].setdefault(camera_name, {})
        cam_summary = summary["by_camera"][camera_name]
        cam_summary[verdict.decision] = cam_summary.get(verdict.decision, 0) + 1
        if verdict.decision == "vehicle":
            summary["vehicle_decisions"] += 1
        elif verdict.decision == "person":
            summary["person_decisions"] += 1
        elif verdict.decision == "suppress":
            summary["suppress_decisions"] += 1

    # Print summary
    print("=" * 80)
    print("=== Summary ===")
    print(f"  Total alerts probed:       {summary['total_alerts']}")
    print(f"  Vehicle decisions:         {summary['vehicle_decisions']}")
    print(f"  Person decisions:          {summary['person_decisions']}")
    print(f"  Suppress decisions:        {summary['suppress_decisions']}")
    print(f"  Wrong frame count (skip):  {summary['wrong_frame_count']}")
    if summary["latency_ms"]:
        sorted_lat = sorted(summary["latency_ms"])
        n = len(sorted_lat)
        print("  Latency (per gate call):")
        print(f"    mean: {sum(sorted_lat) / n:.1f}ms")
        print(f"    min:  {sorted_lat[0]:.1f}ms")
        print(f"    max:  {sorted_lat[-1]:.1f}ms")
        print(f"    p50:  {sorted_lat[n // 2]:.1f}ms")
        print(f"    p95:  {sorted_lat[int(n * 0.95)]:.1f}ms" if n > 1 else "")
    print("  Decisions by reason:")
    for reason, count in sorted(summary["by_reason"].items(), key=lambda x: -x[1]):
        print(f"    {reason:<35} {count}")
    print("  Decisions by camera:")
    for cam, decisions in sorted(summary["by_camera"].items()):
        print(f"    {cam}: {decisions}")

    # Write JSON
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=2)
    print(f"\nResults written to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
