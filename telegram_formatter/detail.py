"""detail.py -- Build TG#2 Telegram message dict after VM2 detail analysis.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - mode: str (required) -- one of 'vehicle', 'person', 'animal'
    - vm2_result: dict (required) -- VM2 detail output
    - crop_a, crop_b: Path | None (required) -- image paths
    - camera_label: str (optional) -- camera name
    - alert: dict | None (optional) -- alert metadata dict
    - position: int (optional) -- TG position 1-3 for marker

OUTPUTS:
    - return dict: {"caption": str, "photos": list[str]}
      shaped for the Telegram client

PUBLIC API:
    build_detail_message(mode, vm2_result, crop_a, crop_b) -> dict
        Build a Telegram-ready message dict for TG#2

DOES NOT DO:
    - Send the Telegram message (pipeline.py handles transport)
    - Derive a threat-level field (operator-locked: no threat fields
      anywhere; see vision-prompt-design skill Rule 6)
    - Do any filesystem I/O (paths are passed in)

CALLED BY:
    - listener/pipeline.py stage 9 (TG#2 emission after VM2)

CALLS INTO:
    - infra.format_ts.format_local_timestamp
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.format_ts import format_local_timestamp

_MODES = ("vehicle", "person", "animal", "unsure")

# Curated field lists per mode -- only these appear in the TG body.
# Values are (label, source_key) tuples.
# source_key maps to a key in the VM2 result (or sub-dict for person attrs).
_PERSON_CURATED: list[tuple[str, str]] = [
    ("Clothing upper", "clothing_upper"),
    ("Clothing lower", "clothing_lower"),
    ("Hat", "hat"),
    ("Footwear", "footwear"),
    ("Posture", "posture"),
    ("Action", "action"),
    ("Accessories", "accessories"),
]

_VEHICLE_CURATED: list[tuple[str, str]] = [
    ("Make", "make"),
    ("Model", "model"),
    ("Color", "color"),
    ("Type", "body_style_hint"),
]

# Map from VM2 top-level key to display label for distinguishing features.
_DISTINGUISHING_KEYS: list[tuple[str, str]] = [
    ("wheel_style", "Wheel style"),
    ("headlight_signature", "Headlight signature"),
    ("rear_lights_signature", "Rear lights signature"),
    ("front_grille_style", "Front grille"),
    ("window_tint", "Window tint"),
    ("bed_cover", "Bed cover"),
    ("badge_text_readable", "Badge text"),
]


def _build_attribute_lines(
    mode: str,
    vm2_result: dict[str, Any],
) -> list[str]:
    """Build curated bullet lines for the given mode.

    Returns a list of lines like:
        \u2022 Clothing upper: white t-shirt
        \u2022 Posture: walking
    """
    lines: list[str] = []

    if mode == "person":
        attrs = vm2_result.get("attributes") or {}
        for label, key in _PERSON_CURATED:
            value = attrs.get(key)
            if value is not None and value != "" and str(value).strip().lower() != "null":
                lines.append(f"\u2022 {label}: {value}")

    elif mode == "vehicle":
        for label, key in _VEHICLE_CURATED:
            value = vm2_result.get(key)
            if value is not None and value != "" and str(value).strip().lower() != "null":
                lines.append(f"\u2022 {label}: {value}")
        # Distinguishing features from vehicle_features sub-dict
        vf = vm2_result.get("vehicle_features")
        if isinstance(vf, dict):
            for vm2_key, label in _DISTINGUISHING_KEYS:
                value = vf.get(vm2_key)
                if value is not None and value != "" and str(value).strip().lower() != "null":
                    lines.append(f"\u2022 {label}: {value}")

    elif mode == "animal":
        # No curated list for animal -- emit all top-level scalars.
        _animal_lines: list[str] = []
        for key, value in vm2_result.items():
            if key in ("better_crop",):
                continue
            if isinstance(value, (dict, list)):
                continue
            if value is not None and value != "" and str(value).strip().lower() != "null":
                label = key.replace("_", " ").title()
                _animal_lines.append(f"\u2022 {label}: {value}")
        lines.extend(_animal_lines)

    elif mode == "unsure":
        for key, value in vm2_result.items():
            if isinstance(value, (dict, list)):
                continue
            if value is not None and value != "" and str(value).strip().lower() != "null":
                lines.append(f"\u2022 {key}: {value}")

    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_detail_message(
    mode: str,
    vm2_result: dict[str, Any],
    crop_a: Path | None,
    crop_b: Path | None,
    camera_label: str = "Camera",
    alert: dict[str, Any] | None = None,
    position: int = 2,
) -> dict[str, Any]:
    """Build a TG#2 Telegram message dict.

    When *alert* is provided, the caption includes 'TG {position} of 3',
    'Alert ID: <id>', and 'Timestamp: <timestamp>' in that order,
    after Camera/Mode/Class lines and before the attribute bullet list.

    Emits a curated, single-line-per-attribute bullet list under a
    'Attributes:' header instead of a recursive VM2 dict dump.
    """
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")

    # Dispatch on mode for mode-specific VM2 outputs.
    if mode == "unsure":
        cls = "unsure"
        cls_line = "Class: unsure"
    elif mode == "vehicle":
        cls = vm2_result.get("make") or vm2_result.get("color") or "vehicle"
        cls_line = f"Class confirmed: {cls}"
    elif mode == "person":
        attrs = vm2_result.get("attributes") or {}
        cls = attrs.get("clothing_upper") or "person"
        cls_line = f"Class confirmed: {cls}"
    elif mode == "animal":
        cls = vm2_result.get("species") or "animal"
        cls_line = f"Class confirmed: {cls}"
    else:
        raise ValueError(f"mode must be one of {_MODES + ('unsure',)}, got {mode!r}")

    lines: list[str] = [
        f"Camera: {camera_label}",
        f"Mode: {mode}",
        cls_line,
    ]

    # Alert metadata (mirrors TG position marker).
    # Alert ID lines removed per US-073g — operator doesn't need these in TG captions.
    if alert:
        lines.append(f"TG {position} of 3")
        timestamp = alert.get("timestamp")
        if timestamp:
            # Wrap in <code> tags — rendered natively in HTML parse_mode
            # (switched from MarkdownV2, US-074).
            lines.append(f"Timestamp: <code>{format_local_timestamp(timestamp)}</code>")

    # Curated attribute bullet list (flat, no nesting).
    attr_lines = _build_attribute_lines(mode, vm2_result)
    if attr_lines:
        lines.append("")
        lines.append("Attributes:")
        lines.extend(attr_lines)

    # TG position marker.
    lines.append(f"TG {position} of 3")

    return {
        "caption": "\n".join(lines),
        "photos": [
            str(crop_a) if crop_a is not None else "",
            str(crop_b) if crop_b is not None else "",
        ],
    }
