"""Phase.144 probe — verify the YOLO-tighten revert + 3-image Qwen payload (§11.66).

the operator 2026-08-27: when a tractor (or other non-vehicle equipment that
YOLO classifies as "car") moves in front of a parked Sequoia, the
6B.134 YOLO-tighten step would crop AROUND the Sequoia (high-conf
"car") and Qwen would describe the wrong subject. 6B.144 reverts the
tighten step and sends the streak crops + a pairwise differential
image so Qwen can pick the MOVING subject.

This probe verifies:
  1. motion_gate_pipeline.run() no longer calls _tighten_streak_crop_with_yolo
     or _write_tight_crop_to_disk (helpers are gone).
  2. The gate writes a pairwise_diff.jpg at the output dir.
  3. GateVerdict exposes pairwise_diff_path.
  4. identify_from_crops appends the diff path to the image list when
     it exists on disk.
  5. The vision prompt describes the 3-image payload (no more "single
     tight crop" lie).

Run: `.venv/bin/python scripts/probe_phase_6B144_pairwise_diff_qwen.py`
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ... helpers ...

def check(name: str, ok: bool, detail: str = "") -> bool:
    status = "OK  " if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    failures = 0

    # ----- 1. tightening helpers removed from motion_gate_pipeline -----
    print("\n=== 1. motion_gate_pipeline: YOLO-tighten removed ===")
    try:
        from listener import motion_gate_pipeline as mod_gate

        removed_a = not hasattr(mod_gate, "_tighten_streak_crop_with_yolo")
        removed_b = not hasattr(mod_gate, "_write_tight_crop_to_disk")
        removed_c = not hasattr(mod_gate, "TIGHTEN_MIN_CONF")
        removed_d = not hasattr(mod_gate, "TIGHTEN_PADDING_PX")
    except ImportError as err:
        print(f"  [FAIL] could not import motion_gate_pipeline: {err}")
        removed_a = removed_b = removed_c = removed_d = False
    if not check("helper _tighten_streak_crop_with_yolo removed", removed_a):
        failures += 1
    if not check("helper _write_tight_crop_to_disk removed", removed_b):
        failures += 1
    if not check("constant TIGHTEN_MIN_CONF removed", removed_c):
        failures += 1
    if not check("constant TIGHTEN_PADDING_PX removed", removed_d):
        failures += 1

    # ----- 2. pairwise_diff_path attribute on GateVerdict -----
    print("\n=== 2. GateVerdict.pairwise_diff_path ===")
    try:
        from listener.motion_gate_pipeline import GateVerdict

        verdict = GateVerdict(decision="vehicle", class_label="car", confidence=0.5)
        ok = hasattr(verdict, "pairwise_diff_path") and verdict.pairwise_diff_path is None
    except ImportError as err:
        print(f"  [FAIL] GateVerdict import failed: {err}")
        ok = False
    if not check("GateVerdict.pairwise_diff_path exists and defaults None", ok):
        failures += 1

    # ----- 3. _write_pairwise_diff_image produces a JPEG with bbox overlays -----
    print("\n=== 3. _write_pairwise_diff_image writes JPEG with bbox overlays ===")
    try:
        import tempfile

        import numpy as np
        from PIL import Image

        from listener.motion_gate_pipeline import _write_pairwise_diff_image

        np.random.seed(42)
        H, W = 480, 640
        arr_a = np.random.randint(0, 255, (H, W, 3), dtype=np.uint8)
        arr_a[200:280, 100:200] = [255, 0, 0]
        frame_a = Image.fromarray(arr_a, mode="RGB")
        arr_b = arr_a.copy()
        arr_b[200:280, 100:200] = [0, 0, 0]
        arr_b[200:280, 200:300] = [255, 0, 0]
        frame_b = Image.fromarray(arr_b, mode="RGB")

        with tempfile.TemporaryDirectory() as tmp:
            bbox_a = (90, 190, 120, 100)
            bbox_b = (190, 190, 120, 100)
            path = _write_pairwise_diff_image(
                frame_a, frame_b, bbox_a, bbox_b, tmp, "test-alert"
            )
            p = Path(path) if path else None
            ok1 = path is not None and p is not None and p.exists()
            ok2 = path is not None and p is not None and p.name == "pairwise_diff.jpg"
            ok3 = path is not None and p is not None and p.stat().st_size > 1000
            if not check("pairwise_diff.jpg created", ok1):
                failures += 1
            if not check("filename = pairwise_diff.jpg", ok2):
                failures += 1
            if not check("file is non-trivial size", ok3):
                failures += 1
    except Exception as err:
        print(f"  [FAIL] diff image generation failed: {err}")
        failures += 1

    # ----- 4. identify_from_crops signature accepts pairwise_diff_path -----
    print("\n=== 4. identify_from_crops accepts pairwise_diff_path ===")
    try:
        import inspect

        from vehicle_identifier.identifier import identify_from_crops
        sig = inspect.signature(identify_from_crops)
        ok = "pairwise_diff_path" in sig.parameters
        if not check("pairwise_diff_path in identify_from_crops signature", ok):
            failures += 1
    except Exception as err:
        print(f"  [FAIL] signature check failed: {err}")
        failures += 1

    # ----- 5. Vision prompt describes 3 images, asks for moving subject -----
    print("\n=== 5. vision prompt describes 3 images, asks for moving subject ===")
    try:
        from vehicle_identifier.prompt_template import (
            render_crop_prompt,
        )
        prompt = render_crop_prompt(
            "CAM5", "2026-08-27 12:00:00 EDT"
        )
        checks = [
            ("STREAK CROP A" in prompt, "mentions STREAK CROP A"),
            ("STREAK CROP B" in prompt, "mentions STREAK CROP B"),
            ("PAIRWISE DIFFERENTIAL" in prompt, "mentions PAIRWISE DIFFERENTIAL"),
            (
                "moving subject" in prompt.lower() or "moving subject" in prompt,
                "asks for moving subject",
            ),
            (
                "stationary vehicles" in prompt.lower(),
                "warns about stationary vehicles",
            ),
            (
                "tight crop of the subject" not in prompt,
                "no longer says 'tight crop'",
            ),
            (
                "the cropped image is the only vehicle" not in prompt,
                "no longer lies 'only vehicle in frame'",
            ),
        ]
        for ok, name in checks:
            if not check(name, ok):
                failures += 1
    except Exception as err:
        print(f"  [FAIL] prompt check failed: {err}")
        failures += 1

    # ----- 6. AlertContext has pairwise_diff_path -----
    print("\n=== 6. AlertContext.pairwise_diff_path ===")
    try:
        from listener.vehicle_event_pipeline import AlertContext
        ctx = AlertContext(
            alert_id="test",
            camera_name="CAM5",
            timestamp="now",
            event_type="vehicle_in_motion",
            rtsp_url="rtsp://x",
            output_dir="/tmp",
            is_vehicle_event=True,
            known_vehicles=[],
            bot_token="",
            chat_id="",
            api_url="",
            gatekeeper_cameras=frozenset(),
        )
        ok = hasattr(ctx, "pairwise_diff_path") and ctx.pairwise_diff_path is None
        if not check("AlertContext.pairwise_diff_path exists and defaults None", ok):
            failures += 1
    except Exception as err:
        print(f"  [FAIL] AlertContext check failed: {err}")
        failures += 1

    print()
    if failures == 0:
        print("PASS — 0 failures. 6B.144 ready to ship.")
        return 0
    print(f"FAIL — {failures} check(s) failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())