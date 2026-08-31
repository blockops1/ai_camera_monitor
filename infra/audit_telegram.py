"""
audit_telegram.py — Single-source audit log line for every Telegram send.

STATUS: stable (renamed from outbound_telegram, 2026-08-13 — was always
    the audit-side of an outbound send; "audit_" prefix matches module
    responsibility better than the bland "outbound_" prefix)
THREAD SAFETY: thread-safe (logging is thread-safe; this is just a logger)

INPUTS:
    - keyword arg channel: str (required) — origin channel
      ("vehicle_tracker" | "CAMERA_ALERT" | ...).
    - keyword arg alert_id: str (required).
    - keyword arg v_id: str (required) — vehicle id, or "".
    - keyword arg event: str (required) — "arrival" | "departure" |
      "match" | "no_match" | etc.
    - keyword arg body: str (required) — the actual message text.
    - keyword arg sent: bool (required) — did the send succeed?
    - keyword arg extra: str | None (optional) — extra context.
    - keyword arg image_paths: list[str] | None (optional).

OUTPUTS:
    - return value: None
    - log line: every send → one [OUTBOUND_TELEGRAM] line with all
      the fields above. Single source of truth for "which Telegram
      went out when."

PUBLIC API:
    log_outbound_telegram(*, channel, alert_id, v_id, event, body, sent,
                          extra=None, image_paths=None) -> None
        Emit one structured audit log line. Use OUTBOUND_TAG for grep.
    OUTBOUND_TAG — module-level constant "[OUTBOUND_TELEGRAM]". Keep
      stable — postmortem scripts depend on this exact string.
    RENAMED_FROM — module-level constant "infra.outbound_telegram".
      Records the rename so audit grep across logs before/after the
      cutover remains traceable.

DOES NOT DO:
    - Send the actual Telegram message — that's infra/send_telegram.py
    - Persist to a JSONL file — only emits a log line (single source
      of audit truth lives in logs/listener.log, not a separate JSONL).
      PLAN §10.4 reserves logs/outbound_telegram.jsonl for a future
      structured pipeline (not yet built).
    - Read or query the audit trail — operators grep listener.log.

WHY HERE:
    Before 2026-07-22 there were 4 different log line formats for
    Telegram sends across the codebase (vehicle_event_handler, notifier,
    alert_listener _sending helpers, _send_arriving_message). Postmortem
    grep had to know which one fired for which channel. This module
    gives every send site one function to call.

    Renamed from `outbound_telegram` to `audit_telegram` in the Q1
    notifier split (2026-08-13). The old name was misleading — the
    module has always been about auditing, not transport. The
    `audit_` prefix matches its real responsibility and sits cleanly
    next to the new `infra/send_telegram.py` (which handles transport).

CALLED BY:
    - infra.notifier: log_outbound_telegram() after every Telegram call
    - listener.listener gatekeeper paths: log_outbound_telegram()
      after each motion/match/no-match send (5 sites in listener.py)

CALLS INTO:
    - logging.getLogger("audit_telegram") — single named logger
      (renamed from "outbound_telegram" in the same commit)

RELATED:
    - logs/listener.log — the audit file (via logging_setup root handler)
    - infra.send_telegram — produces the sends that this module audits
"""
from __future__ import annotations

import logging

log = logging.getLogger("audit_telegram")

# Single string tag for grep. Keep stable — postmortem scripts depend on it.
# The tag itself was NOT renamed (operators grep `[OUTBOUND_TELEGRAM]`,
# not the Python module name). Only the module + logger name changed.
OUTBOUND_TAG = "[OUTBOUND_TELEGRAM]"

# Records the previous module name so a postmortem that searches the
# source history can follow the rename. Not used at runtime.
RENAMED_FROM = "infra.outbound_telegram"


def log_outbound_telegram(
    *,
    channel: str,
    alert_id: str,
    v_id: str,
    event: str,
    body: str,
    sent: bool,
    extra: str | None = None,
    image_paths: list[str] | None = None,
) -> None:
    """
    Emit a structured audit log line for every Telegram send.

    Args:
        channel: Logical channel that produced the send. Common values:
            "vehicle_tracker"  — Phase 6B state-tracker events
            "vehicle_arriving" — Phase.9 message 1 ("Vehicle entering...")
            "alert_notifier"   — main notifier path (notify())
            "notifier_text"    — direct text send via _send_message
        alert_id: The pipeline alert_id, when known. Empty string when not.
        v_id: V-NNN identity for vehicle sends, empty string for camera alerts.
        event: Event kind — "arrival" | "departure" | "unknown_arrival" |
            "level1" | "level2" | "level0_log" | "vehicle_arriving" etc.
        body: The exact message text that went (or attempted to go) to Telegram.
        sent: True if Telegram accepted the message, False if it failed or
            was suppressed upstream.
        extra: Optional free-form suffix for channel-specific metadata
            (e.g. "suppressed_by=bucket_cooldown").
        image_paths: Phase.36 — list of absolute paths to the image
            files that were attached to (or attempted to attach to) the
            Telegram message. Pass [] for text-only sends, None to default
            to []. When present, this is the single source of truth for
            "what image was sent with this message" — log greps like
            `grep 'image_paths=.*frame_X.jpg' logs/listener.log` can
            locate every send that included a specific frame.
    """
    # Phase.36: always emit image_paths so the audit line is
    # self-describing. Default to [] when caller passes None (legacy
    # call sites). Format: [path1,path2] or [].
    paths = image_paths if image_paths is not None else []
    paths_repr = "[" + ",".join(paths) + "]" if paths else "[]"
    line = (
        f"{OUTBOUND_TAG} channel={channel} alert_id={alert_id} v_id={v_id} "
        f"event={event} sent={sent} body={body!r} "
        f"image_paths={paths_repr}"
    )
    if extra:
        line = f"{line} extra={extra}"
    log.info(line)
