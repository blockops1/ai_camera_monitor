"""
probe_quick_classifier.py — Phase.NEW (planned) verification probe.

Loads the QuickClassifier (YOLOv8n ONNX, COCO-trained, CoreML-accelerated)
and runs it against a sample of frames from the existing data/frames/<alert_id>/
corpus. The goal: confirm the gate fires correctly on real frames and measure
end-to-end latency.

Per Note's "investigate before asking" preference (2026-08-22) and the
"probing → scripts/probe_*.py" preference (2026-08-19), this probe:
  1. Imports the prod module (no parallel logic)
  2. Runs it against existing real data
  3. Reports latency stats + verdict distribution
  4. Does NOT touch the listener or any production state

Output: per-frame verdict + decision (suppress / pass_with_hint / pass),
plus aggregate stats. If a future phase wants to verify the gate against
labeled FP/TP data, that's a follow-up probe.

Run:
    source .venv/bin/activate
    python3 scripts/probe_quick_classifier.py [--frames N] [--alert-dir DIR]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

from infra.quick_classifier import QuickClassifier


def _pick_recent_frames(frames_root: Path, count: int) -> list[tuple[Path, str]]:
    """Pick the most recent `count` frame files, paired with their alert_id.

    Returns list of (frame_path, alert_id) sorted newest-first.
    """
    # Sort alert dirs by mtime desc
    alert_dirs = sorted(
        [d for d in frames_root.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    frames: list[tuple[Path, str]] = []
    for d in alert_dirs:
        for f in sorted(d.glob("frame_*.jpg")):
            frames.append((f, d.name))
            if len(frames) >= count:
                return frames
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--frames", type=int, default=30,
        help="Number of frames to test (default 30).",
    )
    parser.add_argument(
        "--alert-dir", default="data/frames",
        help="Alert frames directory (default data/frames).",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.40,
        help="Confidence threshold below which gate suppresses (default 0.40).",
    )
    args = parser.parse_args()

    frames_root = _root / args.alert_dir
    if not frames_root.is_dir():
        print(f"ERROR: {frames_root} does not exist")
        return 1

    frames = _pick_recent_frames(frames_root, args.frames)
    if not frames:
        print(f"ERROR: no frames found in {frames_root}")
        return 1

    print("=== Quick classifier probe ===")
    print("Loading YOLOv8n ONNX (COCO-trained, CoreML EP if available)...")
    t0 = time.perf_counter()
    qc = QuickClassifier(confidence_threshold=args.threshold)
    load_ms = (time.perf_counter() - t0) * 1000
    print(f"Model loaded in {load_ms:.1f} ms\n")

    print(f"Running inference on {len(frames)} frames (threshold={args.threshold})...")
    print()

    verdicts = []
    inference_times = []
    decisions = {"suppress": 0, "pass_with_hint": 0, "pass": 0}
    class_counter: dict[str, int] = {}

    for frame_path, alert_id in frames:
        t0 = time.perf_counter()
        v = qc.classify_frame(str(frame_path))
        dt_ms = (time.perf_counter() - t0) * 1000
        inference_times.append(dt_ms)

        verdicts.append((alert_id, frame_path.name, v))
        decisions[v.decision] += 1
        class_counter[v.top_class] = class_counter.get(v.top_class, 0) + 1

        # Per-frame print
        decision_marker = {
            "suppress": "🚫 SUPPRESS",
            "pass_with_hint": "✅ PASS+HINT",
            "pass": "⚪ PASS",
        }[v.decision]
        print(
            f"  {decision_marker:14s} {alert_id[:8]} {frame_path.name} "
            f"→ {v.top_class} ({v.top_confidence:.2f}) "
            f"in {dt_ms:5.1f} ms"
        )

    print()
    print("=== Summary ===")
    print(f"  Total frames:        {len(verdicts)}")
    print(f"  Model load time:     {load_ms:.1f} ms (one-time)")
    print("  Inference latency:")
    print(f"    mean:              {sum(inference_times) / len(inference_times):.1f} ms")
    print(f"    min:               {min(inference_times):.1f} ms")
    print(f"    max:               {max(inference_times):.1f} ms")
    print(f"    p95:               {sorted(inference_times)[int(len(inference_times) * 0.95)]:.1f} ms")
    print("  Decisions:")
    print(f"    suppress:          {decisions['suppress']}  ({decisions['suppress']/len(verdicts)*100:.0f}%)")
    print(f"    pass_with_hint:    {decisions['pass_with_hint']}  ({decisions['pass_with_hint']/len(verdicts)*100:.0f}%)")
    print(f"    pass (uncertain):  {decisions['pass']}  ({decisions['pass']/len(verdicts)*100:.0f}%)")
    print("  Top classes detected:")
    for cls, count in sorted(class_counter.items(), key=lambda x: -x[1])[:8]:
        print(f"    {cls:20s} {count}")

    # Spot-check: if all are 'suppress', that's a sign threshold is wrong OR
    # the corpus is all-noise. Print a hint.
    if decisions['suppress'] == len(verdicts):
        print()
        print("  ⚠️  WARNING: every frame was suppressed.")
        print("      Likely: real-vehicle/person alerts in this sample, threshold too high,")
        print("      or the model doesn't see the targets at this distance/angle.")
        print("      Try --threshold 0.20 to see what the model is detecting.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
