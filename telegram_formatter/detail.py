"""detail.py -- Build TG#2 Telegram message dict after VM2 detail analysis.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - mode: str (required) -- one of 'vehicle', 'person', 'animal'
    - vm2_result: dict (required) -- VM2 detail output with v1 schema fields
    - crop_a, crop_b: Path | None (required) -- image paths

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
    - stdlib str(): path-to-string conversion
"""

from __future__ import annotations

from pathlib import Path
from textwrap import wrap
from typing import Any

from infra.format_ts import format_local_timestamp

_MODES = ("vehicle", "person", "animal")


# ---------------------------------------------------------------------------
# Recursive Qwen-dict renderer (copied from v1 render_qwen.py)
# ---------------------------------------------------------------------------


def _render_qwen_dict_lines(
    obj: Any,
    indent: int = 0,
) -> list[str]:
    """Render any dict as Telegram body lines.

    Args:
        obj: The dict (or nested value) to render.
        indent: Spaces of left-padding for the first level. Children
            get +3 more spaces than their parent.

    Returns:
        List of body lines (no trailing newline on each).

    Behavior:
        - Walks every key in dict-insertion order.
        - For each value:
            * None / "" / "null" / empty-list / empty-dict → skipped
            * dict → recurse with indent+3
            * list → one item per line if complex, or inline if short scalars
            * bool → "key: true" / "key: false"
            * number → "key: N"
            * string → "key: value"
        - Long string values wrap at 80 chars total width, indented.
    """
    lines: list[str] = []
    pad = " " * indent
    child_pad = " " * (indent + 3)

    if not isinstance(obj, dict):
        if _is_empty(obj):
            return lines
        lines.append(f"{pad}{_format_scalar(obj)}")
        return lines

    for key, value in obj.items():
        if _is_empty(value):
            continue

        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(_render_qwen_dict_lines(value, indent=indent + 3))
            continue

        if isinstance(value, list):

            def _short_scalar(x: Any) -> bool:
                return not isinstance(x, (dict, list)) and len(_format_scalar(x)) < 30

            if all(_short_scalar(x) for x in value) and len(value) <= 4:
                rendered = ", ".join(_format_scalar(x) for x in value)
                lines.append(f"{pad}{key}: [{rendered}]")
            else:
                lines.append(f"{pad}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        lines.extend(
                            _render_qwen_dict_lines(
                                item,
                                indent=indent + 3,
                            )
                        )
                    elif _is_empty(item):
                        continue
                    else:
                        lines.append(f"{child_pad}{_format_scalar(item)}")
            continue

        # Scalar.
        formatted = _format_scalar(value)
        wrap_width = max(40, 80 - len(pad))
        if len(formatted) > wrap_width:
            wrapped = wrap(
                formatted,
                width=wrap_width,
                subsequent_indent=child_pad,
                break_long_words=False,
                break_on_hyphens=False,
            )
            lines.append(f"{pad}{key}:")
            for chunk in wrapped:
                lines.append(f"{child_pad}{chunk}")
        else:
            lines.append(f"{pad}{key}: {formatted}")

    return lines


def _is_empty(value: Any) -> bool:
    """True if value should be skipped entirely (no body line)."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, str) and value.strip().lower() == "null":
        return True
    return bool(isinstance(value, (list, dict)) and len(value) == 0)


def _format_scalar(value: Any) -> str:
    """Format a non-dict, non-list value for body output."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value == int(value):
            return f"{int(value)}.0"
        return f"{value:g}"
    return str(value)


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
) -> dict[str, Any]:
    """Build a TG#2 Telegram message dict.

    When *alert* is provided, the caption includes 'Alert 2 of 3',
    'Alert ID: <id>', and 'Timestamp: <timestamp>' in that order,
    after Camera/Mode/Class lines and before optional body fields.

    Uses the v1 recursive renderer (render_qwen_dict_lines) to walk every
    key in vm2_result, expanding nested dicts (e.g. vehicle_features,
    attributes, signature) with indented sub-lines.
    """
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")

    # Dispatch on mode for mode-specific VM2 outputs.
    # VM2 returns mode-specific keys (not class_confirmed/class):
    #   vehicle -> {color, body_style_hint, make, model, ...}
    #   person  -> {better_crop, attributes, signature, ...}
    #   animal  -> {species, breed, size, ...}
    #   unsure  -> {class_confirmed: False, class: "unsure", reason: "..."}
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

    # Alert metadata (mirrors TG#1 caption layout).
    if alert:
        lines.append("Alert 2 of 3")
        alert_id = alert.get("id")
        if alert_id:
            lines.append(f"Alert ID: {alert_id}")
        timestamp = alert.get("timestamp")
        if timestamp:
            lines.append(f"Timestamp: {format_local_timestamp(timestamp)}")

    # Render the vm2_result dict recursively using the v1 renderer.
    body_lines = _render_qwen_dict_lines(vm2_result)
    if body_lines:
        lines.append("")
        lines.extend(body_lines)

    return {
        "caption": "\n".join(lines),
        "photos": [
            str(crop_a) if crop_a is not None else "",
            str(crop_b) if crop_b is not None else "",
        ],
    }
