"""

This is the probe for §11.26 Regression B (multi-crop vision). It tests
whether sending 3 crops in a single API call (instead of 3 sequential
calls, one per crop) gives a useful answer — without changing any
production code. The output is the raw VisionResult, so the user can
decide whether the description matches what's actually in the images.

Usage:
    .venv/bin/python scripts/probe_multi_crop_vision.py <alert_id> [camera_name]

If camera_name is omitted the script uses the alert folder name heuristic
(looks for the first frame's metadata) — actually just takes it from CLI.

Output:
    - stdout: per-crop description and consolidated signature
    - stdout: latency, prompt hash
    - data/frames/<alert_id>/multi_crop_vision_response.json: raw response
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path("<install-path>/ai_camera_monitor")
sys.path.insert(0, str(PROJECT_ROOT))

from vehicle_identifier.vision_client import (
    DEFAULT_VISION_URL,
    call_vision,
    is_vision_error,
)


def _resolve_crops(alert_id: str) -> list[Path]:
    crops_dir = PROJECT_ROOT / "data" / "frames" / alert_id / "crops"
    if not crops_dir.is_dir():
        sys.exit(f"no crops dir at {crops_dir}")
    crops = sorted(crops_dir.glob("*_crop_*.jpg"))
    if not crops:
        sys.exit(f"no crops in {crops_dir}")
    return crops


def _read_motion_json(alert_id: str) -> dict:
    motion_path = PROJECT_ROOT / "data" / "frames" / alert_id / "motion.json"
    if not motion_path.is_file():
        return {}
    with motion_path.open() as f:
        body: dict = json.load(f)
        return body


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("alert_id", help="UUID of the alert (folder under data/frames/)")
    p.add_argument("camera_name", help="Human-readable camera label")
    p.add_argument(
        "--captured-at",
        default="2026-08-19T16:34:35.000+0000",
        help="ISO timestamp for the prompt",
    )
    p.add_argument(
        "--event-hint",
        default="",
        help="Optional event_hint_block to append to the prompt",
    )
    p.add_argument(
        "--api-url",
        default=DEFAULT_VISION_URL,
        help=f"Vision API URL (default {DEFAULT_VISION_URL})",
    )
    args = p.parse_args()

    crops = _resolve_crops(args.alert_id)
    motion = _read_motion_json(args.alert_id)

    print(f"== probe_multi_crop_vision: alert={args.alert_id} ==")
    print(f"camera_name: {args.camera_name}")
    print(f"captured_at: {args.captured_at}")
    print(f"crops to send ({len(crops)}):")
    primary = motion.get("primary_moving_object", {})
    bbox_per_frame = primary.get("bbox_per_frame", [])
    for i, c in enumerate(crops):
        # area ranking matches crop_0 = largest
        bbox = bbox_per_frame[i] if i < len(bbox_per_frame) else None
        area = bbox[2] * bbox[3] if bbox else 0
        size = c.stat().st_size
        print(f"  [{i}] {c.name}  bbox={bbox}  area={area}px  file={size}B")
    print()

    print(f"== calling call_vision(image_paths=[{len(crops)} crops], prompt=production) ==")
    print(f"api_url: {args.api_url}")
    print()

    result = call_vision(
        image_paths=[str(c) for c in crops],
        camera_name=args.camera_name,
        captured_at=args.captured_at,
        api_url=args.api_url,
        event_hint_block=args.event_hint,
    )

    if is_vision_error(result):
        # runtime narrowing: is_vision_error returns True for VisionError or err-dict
        err_kind = getattr(result, "kind", "unknown")
        err_msg = getattr(result, "message", str(result))
        err_elapsed = getattr(result, "elapsed_ms", 0.0)
        print(f"!! vision error: kind={err_kind}")
        print(f"   message: {err_msg}")
        print(f"   elapsed_ms: {err_elapsed}")
        return 1

    # After the is_vision_error branch, result is VisionResult (or a non-error dict).
    content = getattr(result, "content", result if isinstance(result, dict) else None)
    raw_text = getattr(result, "raw_text", None)
    elapsed = getattr(result, "elapsed_ms", 0.0)

    print(f"== response (elapsed_ms={elapsed:.1f}) ==")
    print(json.dumps(content, indent=2))
    print()

    # Persist the response alongside the crops for later review.
    out_path = PROJECT_ROOT / "data" / "frames" / args.alert_id / "multi_crop_vision_response.json"
    with out_path.open("w") as f:
        json.dump(
            {
                "alert_id": args.alert_id,
                "camera_name": args.camera_name,
                "captured_at": args.captured_at,
                "crops_sent": [str(c) for c in crops],
                "elapsed_ms": elapsed,
                "content": content,
                "raw_text": raw_text,
            },
            f,
            indent=2,
        )
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
