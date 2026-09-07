"""alert.py -- Build TG#1 Telegram message dict after VM1 classification.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - verdict: GateVerdict (required) -- YOLO gate result
    - vm1_result: dict (required) -- VM1 classify output with
      'class', 'confidence', optional 'notes'
    - frames: list[Path] (required) -- 4 frame paths on disk, index 0..3
    - diff_image: Path (required) -- pairwise-differential composite

OUTPUTS:
    - return dict: {"caption": str, "photos": list[str]}
      shaped for the Telegram client

PUBLIC API:
    build_alert_message(verdict, vm1_result, frames, diff_image) -> dict
        Build a Telegram-ready message dict for TG#1

DOES NOT DO:
    - Send the Telegram message (pipeline.py handles transport)
    - Render license plates (not extracted at this stage)
    - Do any filesystem I/O (paths are passed in)

CALLED BY:
    - listener/pipeline.py stage 6 (TG#1 emission after VM1)

CALLS INTO:
    - stdlib str(): path-to-string conversion

RELATED:
    - infra.gate.GateVerdict
    - infra/vm1_prompt.SCHEMA_JSON
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.gate import GateVerdict


def build_alert_message(
    verdict: GateVerdict,
    vm1_result: dict[str, Any],
    frames: list[Path],
    diff_image: Path,
    camera_label: str = "Camera",
) -> dict[str, Any]:
    """Build a TG#1 Telegram message dict."""
    cls = vm1_result["class"]
    conf = vm1_result["confidence"]
    notes = vm1_result.get("notes")

    lines = [
        f"Camera: {camera_label}",
        f"Detected: {cls}",
        f"Confidence: {conf}",
    ]
    if notes:
        lines.append(notes)

    best_frame = str(frames[3]) if frames else str(diff_image)
    frame_paths = [str(f) for f in frames]

    return {
        "caption": "\n".join(lines),
        "photos": [best_frame, str(diff_image)] + frame_paths,
    }
