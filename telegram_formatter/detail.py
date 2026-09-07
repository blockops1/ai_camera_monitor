"""detail.py -- Build TG#2 Telegram message dict after VM2 detail analysis.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - mode: str (required) -- one of 'vehicle', 'person', 'animal'
    - vm2_result: dict (required) -- VM2 detail output with
      'class_confirmed', optional 'license_plate',
      'distinctive_features', 'threat_indicators'
    - crop_a, crop_b: Path | None (required) -- image paths

OUTPUTS:
    - return dict: {"caption": str, "photos": list[str]}
      shaped for the Telegram client

PUBLIC API:
    build_detail_message(mode, vm2_result, crop_a, crop_b) -> dict
        Build a Telegram-ready message dict for TG#2

DOES NOT DO:
    - Send the Telegram message (pipeline.py handles transport)
    - Derive a threat-level field (passes threat_indicators from VM2 as-is)
    - Do any filesystem I/O (paths are passed in)

CALLED BY:
    - listener/pipeline.py stage 9 (TG#2 emission after VM2)

CALLS INTO:
    - stdlib str(): path-to-string conversion
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MODES = ("vehicle", "person", "animal")


def build_detail_message(
    mode: str,
    vm2_result: dict[str, Any],
    crop_a: Path | None,
    crop_b: Path | None,
    camera_label: str = "Camera",
) -> dict[str, Any]:
    """Build a TG#2 Telegram message dict."""
    if mode not in _MODES:
        raise ValueError(
            f"mode must be one of {_MODES}, got {mode!r}"
        )

    cls = vm2_result.get("class_confirmed", vm2_result.get("class", "unknown"))
    lines = [
        f"Camera: {camera_label}",
        f"Mode: {mode}",
        f"Class confirmed: {cls}",
    ]

    plate = vm2_result.get("license_plate")
    if plate:
        lines.append(f"License plate: {plate}")

    feats = vm2_result.get("distinctive_features")
    if feats:
        lines.append(f"Distinctive features: {', '.join(feats)}")

    threats = vm2_result.get("threat_indicators")
    if threats:
        lines.append(f"Threat indicators: {', '.join(threats)}")

    return {
        "caption": "\n".join(lines),
        "photos": [
            str(crop_a) if crop_a is not None else "",
            str(crop_b) if crop_b is not None else "",
        ],
    }
