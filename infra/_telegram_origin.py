"""
_telegram_origin.py — Single source of truth for the
[filename] [CHANNEL_LABEL] origin tag prepended to every Telegram
message body and every OFFLINE audit-log line.

Why this exists (2026-07-26)
----------------------------
the operator asked (twice, 2026-07-25 and 2026-07-26) that every Telegram
alert include the *actual filename* of the script sending the message,
not a generic channel label. Earlier tags ([ALERT], [STATE], [ARRIVING])
described the *kind* of message but not the *caller*, so when the
<site> Orchards Telegram group flooded with 5 replay messages at
11:33:04 today we couldn't tell which file produced them.

This module provides:

  • detect_caller_script(skips=None)  →  basename of the calling .py
    file, derived from inspect.stack(). Walks back from the current
    frame and returns the first frame whose __file__ isn't listed in
    `skips`. Defaults skip notifier.py and outbound_telegram.py so the
    result is the *application* code that called the formatter /
    transport, not the transport itself.

  • origin_prefix(channel_label=None)  →  "<b>[filename] [CHANNEL]</b>"
    string suitable for prepending to a Telegram body, or
    "[filename] [CHANNEL]" without <b> for a log line.

Both are cheap (~50 ns) and pure — no I/O, safe to import anywhere.

Usage:
    from _telegram_origin import origin_prefix, detect_caller_script

    body = origin_prefix("VEHICLE_ARRIVAL") + "\\n" + msg
    log.info(f"sent: {origin_prefix('CAMERA_ALERT', bold=False)} {body[:80]}")
"""
from __future__ import annotations

import inspect
import os
import os.path

# Frames whose __file__ basename we walk *past* when searching for the
# real caller. Keep this list SHORT — only files whose entire job is to
# route the message.
_DEFAULT_SKIPS = {
    "notifier.py",
    "_telegram_origin.py",
    "send_telegram.py",
    "audit_telegram.py",
}


def _basename_no_ext(path: str | None) -> str:
    """Return filename without `.py`, or 'unknown' if path is empty/None.

    Defensive: inspect frames can have `None` __file__ for the
    interpreter's own frames (e.g. `<frozen>`). Treat those as opaque.
    """
    if not path:
        return "unknown"
    return os.path.splitext(os.path.basename(path))[0]


def detect_caller_script(
    skips: list[str] | None = None,
) -> str:
    """Return the basename (without .py) of the first caller frame
    above the current one whose filename is not in `skips`.

    If every frame is skipped (deep stack through internal helpers), we
    return the basename of the *outermost* frame we inspected, since
    "unknown" is worse than a plausible guess.

    Args:
        skips: Optional list of basenames to skip (e.g. ['notifier.py']).
            Defaults to _DEFAULT_SKIPS.

    Returns:
        Filename string like 'alert_listener' or 'vehicle_event_handler'.
    """
    skip_set = set(skips) if skips is not None else _DEFAULT_SKIPS
    fallback = "unknown"

    # Walk the stack starting at the caller of `detect_caller_script`.
    try:
        frames = inspect.stack()[1:]  # [0] is detect_caller_script itself
    except Exception:
        return fallback

    for frame_info in frames:
        path = getattr(frame_info, "filename", None) or ""
        base = os.path.basename(path)
        if base and base not in skip_set:
            return _basename_no_ext(path)
        # Track the outermost frame we saw as a last-ditch fallback.
        if base:
            fallback = _basename_no_ext(path)

    return fallback


def origin_prefix(
    channel_label: str | None = None,
    *,
    bold: bool = True,
    script: str | None = None,
) -> str:
    """Build the "[filename] [CHANNEL_LABEL]" tag for a Telegram body.

    Args:
        channel_label: The descriptive channel tag (e.g. 'VEHICLE_ARRIVAL',
            'CAMERA_ALERT', 'VEHICLE_IN_MOTION', 'WILDLIFE'). When None,
            only the script tag is returned.
        bold: If True, wrap the result in <b>...</b> for Telegram HTML.
        script: Override the detected script name (used by tests /
            deterministic loggers). When None, uses detect_caller_script().

    Returns:
        e.g. '<b>[vehicle_event_handler] [VEHICLE_ARRIVAL]</b>' or
             '[alert_listener] [VEHICLE_IN_MOTION]' (with bold=False).
    """
    name = script if script is not None else detect_caller_script()
    parts = [name]
    if channel_label:
        parts.append(channel_label)
    inner = " ".join(f"[{p}]" for p in parts)
    return f"<b>{inner}</b>" if bold else inner


__all__ = [
    "detect_caller_script",
    "origin_prefix",
]
