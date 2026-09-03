"""
notify — TG#1 ("vehicle arriving") Telegram sender + vehicle summary formatter.

STATUS: stable
THREAD SAFETY: single-threaded (one call per alert; runs inside camera semaphore)

INPUTS:
    - alert_id, camera_name, frame_path, bot_token, chat_id, captured_at — for TG#1
    - vehicle dict — for _format_vehicle_summary

OUTPUTS:
    - _send_arriving_message: returns bool (Telegram accepted or no creds)
    - _format_vehicle_summary: returns short string like "red Kubota M7 tractor"

PUBLIC API:
    _send_arriving_message(alert_id, camera_name, frame_path, bot_token, chat_id, captured_at="") -> bool
        Phase.112 (2026-08-21): TG#1 ("vehicle detected, identifying...")
        instant heads-up. Fires from identify_stage AFTER motion detector
        confirms a primary mover + vision ran. Failure-isolated.
    _format_vehicle_summary(v) -> str
        Format a vehicle dict as a short identification string.
        Defensive against None and non-dict inputs.

DOES NOT DO:
    - Decide whether TG#1 should fire — the caller (identify_stage) owns
      that gate on camera_code ∈ gatekeeper_cameras.
    - Build the message body for TG#2/TG#3 — those live in
      telegram_formatter.composite_telegram and telegram_formatter.match_telegram.
    - Send TG#1 for non-vehicle events — caller gates on is_vehicle_event.

WHY HERE:
    _send_arriving_message was inlined from listener._process_alert (Phase
    6B.105c, 2026-08-21) so the pipeline module has zero cross-listener
    dependencies. _format_vehicle_summary is its peer — they share the
    vehicle identification domain (Telegram body formatting + identification
    summary). Both are pure formatting/sending helpers with no state.

CALLED BY:
    - identify_stage (in identify.py) — calls _send_arriving_message
    - match._vision_summary_str (in match.py) — calls _format_vehicle_summary

CALLS INTO:
    - infra.send_telegram.send_photo_with_caption — actual HTTP to Telegram
    - infra.audit_telegram.log_outbound_telegram — audit line per send

RELATED:
    - match._vision_summary_str — caller of _format_vehicle_summary
    - infra.send_telegram — TG#1 transport (multipart photo + caption)
    - §11.112 in PLAN.md — TG#1 spec
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _send_arriving_message(
    alert_id: str,
    camera_name: str,
    frame_path: str,
    bot_token: str,
    chat_id: str,
    captured_at: str = "",
) -> bool:
    """
    Phase.9 message 1: instant heads-up that a vehicle is on the
    property and the system is identifying it.

    Inlined from listener.listener._process_alert (Phase.105c, 2026-08-21)
    so the pipeline module has zero cross-listener dependencies. The
    listener.py version remains as the original source of truth for the
    comment history; if either copy drifts, prefer updating this one since
    it's what the production tree calls.

    Phase.112 (2026-08-21): Note spec calls for TG#1 ("vehicle
    detected, identifying...") on every gatekeeper-camera vehicle event
    where motion detector confirms motion. The previous VEHICLE_ARRIVING_ENABLED env
    gate (default OFF, set via FARM_VEHICLE_ARRIVING_ENABLED=1) was
    RETIRED — it predated Note's current spec. The Telegram now fires
    unconditionally when called from identify_stage. Callers gate on
    `is_vehicle_event AND motion_result.primary_moving_object is not
    None` so it doesn't fire for non-vehicle events or no-motion events.

    Phase.113 (2026-08-21): Switched from `infra.notifier.notify()`
    to `infra.send_telegram.send_photo_with_caption()`. The notify()
    path adds an `[CAMERA_ALERT]` prefix and emits a redundant
    `channel=alert_notifier` audit line; we want a clean TG#1 with
    only the `channel=vehicle_arriving` audit line. Mirrors the
    pattern used by send_composite_alert and send_match_alert.

    Returns True if Telegram accepted the message (or if there were no
    creds to send with), False if the send failed.

    Failure-isolated: any exception is logged, never raised.

    Phase.114 (2026-08-25): Removed the [alert_id] prefix (diagnostic
    noise to the user) and added a footer line with the captured_at
    webhook time so the operator can correlate "when did this fire?"
    with logs. Event time, not send time, per Note correction
    ("it is actually fine to leave it as the webhook time").
    """
    from infra.audit_telegram import log_outbound_telegram

    # Build the body (no CHANNEL_LABEL prefix; this is TG#1).
    # Phase.114: footer with event time at the end.
    body_lines = [
        f"🚗 <b>[VEHICLE_IN_MOTION]</b> Vehicle moving on property at {camera_name}, identifying...",
    ]
    if captured_at:
        body_lines.append("")
        # captured_at already includes "EDT" suffix (infra.timezone.to_edt_string).
        body_lines.append(captured_at)
    body = "\n".join(body_lines)

    try:
        from infra.send_telegram import send_photo_with_caption as _tg_send
        photo_ok = bool(_tg_send(
            bot_token, chat_id, frame_path, body,
            alert_id=alert_id,
            channel="vehicle_arriving",
            event="vehicle_arriving",
        ))
    except Exception as err:
        log.warning(
            f"[{alert_id}] message 1 (arriving) send failed: {err}"
        )
        photo_ok = False

    log.info(
        f"[{alert_id}] message 1 (arriving) → telegram: "
        f"camera={camera_name}, sent={photo_ok}"
    )
    log_outbound_telegram(
        channel="vehicle_arriving",
        alert_id=alert_id,
        v_id="",
        event="vehicle_arriving",
        body=body,
        sent=bool(photo_ok),
        extra=f"camera={camera_name}",
        image_paths=[frame_path] if frame_path and photo_ok else [],
    )
    return photo_ok


def _format_vehicle_summary(v) -> str:
    """Format a vehicle dict as a short identification string.

    Defensive against None and non-dict inputs (LSP-safe: caller may
    pass None when vision_result has unexpected shape).
    """
    if not isinstance(v, dict):
        return ""
    parts: list[str] = []
    color = (v.get("color") or "").strip()
    make = (v.get("make") or "").strip()
    model = (v.get("model") or "").strip()
    body_style = (v.get("type") or v.get("body_style_hint") or "").strip()
    if color:
        parts.append(color)
    if make and model:
        parts.append(f"{make} {model}")
    elif make:
        parts.append(make)
    elif model:
        parts.append(model)
    if body_style:
        parts.append(body_style)
    return " ".join(parts)
