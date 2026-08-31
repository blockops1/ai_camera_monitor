"""
Probe Phase.81 (PLAN §11.14) — enriched CAM5 lead motion Telegram.

Renders the lead motion Telegram body using the SAME helpers that
`_send_motion_alert` uses, then prints:
  1. The enriched body, line by line.
  2. A synthetic MotionResult + 6 synthetic frames.
  3. The annotated frames (with green bbox outlines) written to a
     scratch directory.
  4. Stats on the annotated JPEGs (green-pixel counts, file sizes).

This is the probe-first check for §11.14.6 step 8. It exercises all
three new additions (detector metadata, Qwen confidence, bbox
annotation) end-to-end WITHOUT needing a real CAM5 event or the listener
to be running. If this renders correctly, the production listener will
produce the same body for a real motion event.

Usage:
    .venv/bin/python scripts/probe_enriched_alert.py

Output:
    - stdout: the rendered Telegram body (header + metadata + vehicle line)
    - stdout: stats (green-pixel counts per annotated frame)
    - <scratch>/: 6 synthetic JPEG originals + 6 annotated JPEG copies
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# Repo root → import infra + listener
sys.path.insert(0, str(Path(__file__).parent.parent))

import cv2
import numpy as np

from infra.motion_detector import MotionResult, MovingObject
from telegram_formatter.vehicle_alert import (
    annotate_frame_bboxes,
    format_detector_metadata_lines,
    format_motion_alert_vehicle_line,
    format_qwen_confidence_line,
    render_qwen_dict_lines,
)

# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------

def _make_synthetic_frame(
    width: int = 2560,
    height: int = 1920,
    color_bgr: tuple = (40, 40, 40),
) -> np.ndarray:
    """Solid-color BGR frame as numpy uint8 array."""
    return np.full((height, width, 3), color_bgr, dtype=np.uint8)


def _make_synthetic_motion_result() -> MotionResult:
    """Realistic MotionResult mimicking the 09:22:42 ET alert from
    logs/listener.log on 2026-08-16 (total_motion_px=33704,
    reference_method=pairwise)."""
    primary = MovingObject(
        bbox_per_frame=[
            (0, 0, 0, 0),  # absent
            (300, 100, 200, 200),
            (300, 100, 200, 200),
            (500, 200, 200, 200),
            (500, 200, 200, 200),
            (700, 300, 200, 200),
        ],
        center_per_frame=[(0, 0)] + [(400, 200)] * 5,
        area_per_frame=[0] + [40000] * 5,
        trajectory=["absent", "UM1", "UM1", "UM1", "UM1", "LM2"],
        avg_area=40000,
        frames_seen=5,
        total_motion_pixels=33704,
        position_change_max=287,
        best_crop_path=None,
        crop_paths=[],
    )
    return MotionResult(
        moving_objects=[primary],
        primary_moving_object=primary,
        best_crop_path=None,
        crop_paths=[],
        no_motion_detected=False,
        reference_method="pairwise",
        total_motion_pixels=33704,
        elapsed_ms=23,
    )


def _make_synthetic_vision_result() -> dict:
    """vision_result dict mimicking Qwen's response on a "noisy"
    CAM5 event — confidence is present (low, since CAM5 is far), but
    structured fields are blank. This is the realistic case the operator
    wants enriched.
    """
    return {
        "confidence": 0.32,
        "vehicles": [
            {
                # All Qwen fields empty — matches the user's screenshot case
                "color": "",
                "body_style_hint": "",
                "make": "",
                "model": "",
                "vehicle_features": {},
                "description": "",
                "frame_positions": [
                    "absent",
                    "UM1",
                    "UM1",
                    "UM1",
                    "UM1",
                    "LM2",
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Body construction — mirror the section of _send_motion_alert that builds
# the lead motion alert body, but using module-level helpers instead of
# relying on the nested-function call.
# ---------------------------------------------------------------------------

def build_enriched_body(
    camera_name: str,
    captured_at: str,
    detector_trajectory: list,
    motion_result: MotionResult,
    vision_result: dict,
    vehicles: list,
) -> list:
    """Reproduce the exact body that `_send_motion_alert` builds.

    This is the same code path as lines 2948-2989 of listener.py, minus
    the photo send. Returns a list of body lines.
    """
    lines = [f"🚗 <b>Vehicle motion at {camera_name}</b>"]
    if captured_at:
        lines.append(f"   {captured_at}")
    # Phase.81 — Qwen confidence line.
    lines.append(format_qwen_confidence_line(vision_result))
    if detector_trajectory:
        lines.append(
            f"   detector trajectory: "
            f"{' → '.join(str(x) for x in detector_trajectory)}"
        )
    # Phase.81 — detector metadata section.
    lines.extend(format_detector_metadata_lines(motion_result))
    for idx, v in enumerate(vehicles, start=1):
        lines.append(format_motion_alert_vehicle_line(idx, v, vision_result))
        lines.extend(render_qwen_dict_lines(v, indent=6))
        fps = v.get("frame_positions") or []
        if fps:
            lines.append(
                f"      frame trajectory: {' → '.join(str(x) for x in fps)}"
            )
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe Phase.81 enriched CAM5 lead motion Telegram."
    )
    parser.add_argument(
        "--scratch-dir",
        default=None,
        help="Directory to write synthetic + annotated frames (default: tempdir).",
    )
    args = parser.parse_args()

    scratch = Path(args.scratch_dir) if args.scratch_dir else Path(tempfile.mkdtemp(prefix="probe_enriched_"))
    scratch.mkdir(parents=True, exist_ok=True)
    print(f"=== scratch dir: {scratch} ===")

    # 1. Generate 6 synthetic JPEG frames.
    print("\n=== generating 6 synthetic frames (2560x1920 solid-color) ===")
    frame_paths = []
    for i in range(6):
        fp = scratch / f"frame{i}.jpg"
        cv2.imwrite(str(fp), _make_synthetic_frame(), [cv2.IMWRITE_JPEG_QUALITY, 90])
        frame_paths.append(str(fp))
        print(f"  wrote {fp.name} ({os.path.getsize(fp)} bytes)")

    # 2. Build synthetic MotionResult + vision_result.
    motion_result = _make_synthetic_motion_result()
    vision_result = _make_synthetic_vision_result()
    vehicles = vision_result["vehicles"]
    primary_obj = motion_result.primary_moving_object
    if primary_obj is None:
        raise RuntimeError("synthetic MotionResult.primary_moving_object is None")
    detector_trajectory = primary_obj.trajectory

    # 3. Render the enriched Telegram body.
    print("\n=== enriched Telegram body ===\n")
    body_lines = build_enriched_body(
        camera_name="CAM5",
        captured_at="2026-08-16T09:22:42.000-04:00",
        detector_trajectory=detector_trajectory,
        motion_result=motion_result,
        vision_result=vision_result,
        vehicles=vehicles,
    )
    for line in body_lines:
        print(line)

    print(f"\n=== body stats: {len(body_lines)} lines, "
          f"{sum(len(l) for l in body_lines)} chars ===")

    # 4. Annotate the 6 frames via the helper.
    print("\n=== annotating frames (detector bboxes → green outlines) ===")
    primary = motion_result.primary_moving_object
    annotated_paths = annotate_frame_bboxes(frame_paths, primary)

    print("\n=== annotated frame stats ===")
    print(f"  {'frame':<10} {'orig_KB':>8} {'ann_KB':>8} {'green_px':>10}")
    for i, (orig, ann) in enumerate(zip(frame_paths, annotated_paths)):
        orig_kb = os.path.getsize(orig) // 1024
        ann_kb = os.path.getsize(ann) // 1024
        # Count pure green pixels in the annotated file.
        ann_img = cv2.imread(ann)
        if ann_img is None:
            green_px = -1
        else:
            mask = (
                (ann_img[:, :, 0] == 0)
                & (ann_img[:, :, 1] == 255)
                & (ann_img[:, :, 2] == 0)
            )
            green_px = int(mask.sum())
        same = "✓" if orig != ann else "→ fallback (orig)"
        print(f"  {i:<10} {orig_kb:>8} {ann_kb:>8} {green_px:>10}  {same}")

    print(f"\n=== annotated JPEGs at {scratch}/annotated_frame*.jpg ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())