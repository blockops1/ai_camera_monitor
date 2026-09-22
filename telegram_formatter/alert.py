"""alert.py -- Build TG#1 Telegram message dict after VM1 classification.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - verdict: GateVerdict (required) -- YOLO gate result
    - vm1_result: dict (required) -- VM1 classify output with
      'class', 'confidence', optional 'notes'
    - artifacts: AlertArtifacts (required) -- composite + full frame paths
    - camera_label: str (required) -- human-readable camera name
    - position: int (required) -- TG alert position in sequence (1-3)
    - alert: dict | None (optional) -- parent alert dict, for error context

OUTPUTS:
    - return dict: {"caption": str, "photos": list[str]}
      shaped for the Telegram client

PUBLIC API:
    build_alert_message(verdict, vm1_result, artifacts, camera_label,
                        position, alert=None) -> dict
        Build a Telegram-ready MarkdownV2 message dict for TG#1

DOES NOT DO:
    - Send the Telegram message (pipeline.py handles transport)
    - Render license plates (not extracted at this stage)
    - Do any filesystem I/O (paths are passed in)

CALLED BY:
    - listener/pipeline.py stage 7 (TG#1 emission after VM1)

CALLS INTO:
    - stdlib str(): path-to-string conversion
    - infra.format_ts.format_local_timestamp

RELATED:
    - infra.gate.GateVerdict
    - infra.alert_artifacts.AlertArtifacts
"""

from __future__ import annotations

from infra.format_ts import format_local_timestamp

_CAPTION_MAX_LENGTH = 1024  # Telegram caption limit

# Emoji icons per class type (Telegram MarkdownV2 supports emoji inline)
_CLASS_EMOJI: dict[str, str] = {
    "person": "\U0001f464",  # \xf0\x9f\x91\xa4
    "vehicle": "\U0001f697",  # \xf0\x9f\x9a\x97
    "animal": "\U0001f415",  # \xf0\x9f\x90\x95
}
_DEFAULT_EMOJI = "\U0001f4e6"  # \xf0\x9f\x93\xa6  (other/unknown)

_CAMERA_EMOJI = "\U0001f3a5"  # \xf0\x9f\x8e\xa5


def _truncate_caption(caption: str) -> str:
    """Truncate caption to 1024 chars, adding ellipsis if needed."""
    if len(caption) <= _CAPTION_MAX_LENGTH:
        return caption
    return caption[: _CAPTION_MAX_LENGTH - 3] + "..."


def _escape_markdown_v2(text: str) -> str:
    """Escape all MarkdownV2 special characters in *text*.

    Telegram MarkdownV2 reserves: *_\\][`()>#!-+={}.

    Per Telegram docs: escape these chars with a backslash so they render
    literally in the message body.
    """
    # MarkdownV2 reserved chars + backslash itself.
    # MarkdownV2 reserved chars: _, *, [, ], (, ), ~, `, >, #, +, =, -, |, {, }, ., !
    # Plus backslash itself (which must be doubled for literal output).
    ESCAPED = "_*[]()~`>#+-=|{}." + chr(92)
    return "".join(f"\\{ch}" if ch in ESCAPED else ch for ch in text)


def _emoji_for_class(cls: str) -> str:
    """Return the emoji icon for a VM1 classification."""
    return _CLASS_EMOJI.get(cls, _DEFAULT_EMOJI)


def build_alert_message(
    verdict, vm1_result, artifacts, camera_label="Camera", position=1, alert=None
):
    """Build a TG#1 Telegram message dict (MarkdownV2).

    Returns a 4-6 line Markdown body:

        Camera -- HH:MM AM/PM
        Class *bold* \u2022 confidence N%
        TG X of 3
        short_id

    plus an optional notes line if vm1_result["notes"] is non-empty.

    Returns photos = [composite_path] if composite_path is not None.
    If composite_path is None (gate returned no bbox), returns photos = [].
    """
    composite_path = artifacts.composite_path

    cls = vm1_result["class"]
    conf = vm1_result.get("confidence", "likely")
    notes = vm1_result.get("notes")

    # Extract alert metadata.
    alert_id = alert.get("id") if alert else None
    timestamp = alert.get("timestamp") if alert else None

    # Build the MarkdownV2 body lines.
    lines: list[str] = []

    # Line 1: camera header with emoji and time.
    if timestamp:
        time_str = format_local_timestamp(timestamp)
    else:
        time_str = ""
    if time_str:
        header = f"{_CAMERA_EMOJI} *{_escape_markdown_v2(camera_label)}* -- {_escape_markdown_v2(time_str)}"
    else:
        header = f"{_CAMERA_EMOJI} *{_escape_markdown_v2(camera_label)}*"
    lines.append(header)

    # Line 2: class with emoji and confidence.
    emoji = _emoji_for_class(cls)
    conf_str = _escape_markdown_v2(str(conf))
    class_line = f"{emoji} *{_escape_markdown_v2(cls)}* \u2022 confidence {conf_str}%"
    lines.append(class_line)

    # Line 3: TG position marker.
    lines.append(f"TG {position} of 3")

    # Line 4: short alert ID (first 8 chars).
    if alert_id:
        lines.append(alert_id[:8])

    # Line 5 (optional): notes.
    if notes:
        lines.append(_escape_markdown_v2(notes))

    # Join with newlines, truncate to Telegram caption limit.
    caption = "\n".join(lines)
    caption = _truncate_caption(caption)

    # Photos: composite only.
    photos: list[str] = []
    if composite_path is not None:
        photos.append(composite_path)

    return {
        "caption": caption,
        "photos": photos,
    }
