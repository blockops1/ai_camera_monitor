"""Live sandbox probe for the 6B.89 minimal CAM5 motion Telegram.

Constructs a MotionTelegramInput with the shape the listener passes,
runs it through the formatter, and prints the body + the frame that
would be sent. Does NOT send a real Telegram — this is a sandbox
verification of the format + frame pick.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/probe_minimal_motion_alert.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram_formatter.motion_telegram import (
    MotionTelegramInput,
    build_minimal_motion_telegram_body,
)


def main() -> int:
    # Synthesize the same Qwen response shape the listener passes when
    # a vehicle is detected. Pulls from a real alert's vision_result
    # captured earlier today.
    vision_result = {
        "confidence": 0.95,
        "vehicles": [
            {
                "color": "black",
                "body_style_hint": "pickup",
                "make": "Ford",
                "model": "F-150",
                "description": "black Ford F-150 pickup with camper shell",
                "vehicle_features": {"bed_cover": "camper_shell"},
                "distinctive_features": ["camper_shell"],
                "confidence": 0.95,
            }
        ],
    }

    inp = MotionTelegramInput(
        camera_name="CAM5",
        captured_at_iso="2026-08-18 10:11:13 EDT",
        trajectory=["absent", "UM1", "UM1", "UM1", "UM1", "UM1"],
        avg_area=6183,
        vision_result=vision_result,
    )

    body = build_minimal_motion_telegram_body(inp)
    print("=== 6B.89 minimal CAM5 motion Telegram (sample) ===")
    print(body)
    print("=== END ===")

    # Frame selection probe — show what frame_paths[3] would point to.
    sample_alert = "199d0523-ef69-4ad0-a776-8038f60dd830"
    frame_dir = Path("data/frames") / sample_alert
    frames = sorted(frame_dir.glob("frame_*.jpg"))
    print()
    print(f"=== Frame pick probe (sample alert {sample_alert}) ===")
    if len(frames) >= 4:
        picked = frames[3]
        print(f"6-frame burst detected: {len(frames)} frames")
        print(f"frame_paths[3] = {picked}")
        print(f"size: {picked.stat().st_size:,} bytes")
        print(f"exists: {picked.is_file()}")
    else:
        print(f"Short burst: {len(frames)} frames (would fall back)")
    print("=== END ===")

    return 0


if __name__ == "__main__":
    sys.exit(main())