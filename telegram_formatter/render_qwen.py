"""Generic Qwen-dict renderer.

maintainer OOB 2026-08-11: "when I tell you that I want all the output
of the identifier vision model output sent to me in the telegram
that's actually what I mean. It's not up to you to interpret my
request when I tell you I want something very specific."

Walks every key in any dict and emits a body line for each.
No curated whitelist, no truncation. The only keys skipped are
fields that other modules inject into the dict (matcher verdict,
pipeline bookkeeping). Those are NOT Qwen's output and must never
appear in the Motion Telegram body.

Pure function: no I/O, no globals, no side effects.
"""

from __future__ import annotations

from textwrap import wrap
from typing import Any

# Fields the matcher or pipeline inject into the vehicle dict.
# These are NOT Qwen's output. They must never appear in the
# Motion Telegram body. If the user wants the matcher's verdict
# they look at the Match Telegram.
MATCHER_OUTPUT_SKIP_KEYS: frozenset[str] = frozenset({
    # Matcher verdict — would leak "Jayco" into a "white pickup" alert.
    "identified_label", "identified_owner", "identified",
    "identification_confidence", "identification_crops_used",
    "identification_fallback",
    # Matcher-internal bookkeeping.
    "kv_id", "label", "owner",
    "signature", "breakdown",
    "vision_classification",
    # Pipeline bookkeeping.
    "best_crop_path", "crops_used", "fallback_used", "elapsed_ms",
    "frame_positions", "motion",
})


# Fields where None means "I checked, I don't know." Render as
# "key: unknown" so the user sees the model considered it.
_KEEP_NULL_AS_UNKNOWN: frozenset[str] = frozenset()


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


def render_qwen_dict_lines(
    obj: Any,
    indent: int = 0,
    skip_keys: frozenset[str] = MATCHER_OUTPUT_SKIP_KEYS,
) -> list[str]:
    """Render any dict as Telegram body lines.

    Args:
        obj: The Qwen response dict (or any nested dict within it).
        indent: Spaces of left-padding for the first level. Children
            get +3 more spaces than their parent.
        skip_keys: Field names to omit entirely. Defaults to the
            matcher-output skip set; tests can override.

    Returns:
        List of body lines (no trailing newline on each).

    Behavior:
        - Walks every key in dict-insertion order.
        - For each value:
            * None / "" / "null" / empty-list / empty-dict → skipped
            * dict → recurse with indent+3
            * list → one item per line, "key: [item]" or "key:" then
              each item on its own indented line
            * bool → "key: true" / "key: false"
            * number → "key: N"
            * string → "key: value"
        - Long string values wrap at 80 chars total width, indented.
    """
    lines: list[str] = []
    pad = " " * indent
    child_pad = " " * (indent + 3)

    if not isinstance(obj, dict):
        # Top-level non-dict: render as a single value line.
        if _is_empty(obj):
            return lines
        lines.append(f"{pad}{_format_scalar(obj)}")
        return lines

    for key, value in obj.items():
        if key in skip_keys:
            continue
        if _is_empty(value):
            continue

        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(render_qwen_dict_lines(value, indent=indent + 3,
                                                skip_keys=skip_keys))
            continue

        if isinstance(value, list):
            # Inline if all items are short scalars, else one per line.
            def _short_scalar(x: Any) -> bool:
                return (not isinstance(x, (dict, list))
                        and len(_format_scalar(x)) < 30)

            if all(_short_scalar(x) for x in value) and len(value) <= 4:
                rendered = ", ".join(_format_scalar(x) for x in value)
                lines.append(f"{pad}{key}: [{rendered}]")
            else:
                lines.append(f"{pad}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        lines.extend(render_qwen_dict_lines(
                            item, indent=indent + 3, skip_keys=skip_keys,
                        ))
                    elif _is_empty(item):
                        continue
                    else:
                        lines.append(f"{child_pad}{_format_scalar(item)}")
            continue

        # Scalar.
        formatted = _format_scalar(value)
        # Wrapping: 80 chars total width minus leading indent.
        wrap_width = max(40, 80 - len(pad))
        if len(formatted) > wrap_width:
            wrapped = wrap(formatted, width=wrap_width,
                           subsequent_indent=child_pad,
                           break_long_words=False,
                           break_on_hyphens=False)
            lines.append(f"{pad}{key}:")
            for chunk in wrapped:
                lines.append(f"{child_pad}{chunk}")
        else:
            lines.append(f"{pad}{key}: {formatted}")

    return lines
