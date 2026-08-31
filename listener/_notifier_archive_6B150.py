"""
_notifier_archive_6B150.py — Threat-level Telegram routing (ARCHIVED 2026-08-28).

the operator 2026-08-28: "let's remove heartbeat and the notifier out of
the application, and archive the script. We may want to referred to
them later but probably not."

This is the original `infra/notifier.py` verbatim. Kept for
historical reference. Phase.150 (PLAN §11.72) confirmed that
no production code has called `notify()` since 2026-08-21
(commit 6B.113 replaced it with direct `infra.send_telegram`
calls from `telegram_formatter/match_telegram.py`).

To restore: `git mv listener/_notifier_archive_6B150.py
infra/notifier.py` and re-wire whichever pipeline stage you
want to route through threat-level gating.

STATUS: archived (was: stable post-cooldown-extraction in commit
    5432cd9; post-transport-extraction 2026-08-13 — moved to
    infra/send_telegram.py)
THREAD SAFETY: thread-safe (cooldown map uses threading.Lock internally;
    Telegram send is per-call, no shared mutable state)

INPUTS:
    - function arg alert: dict (required) — output from
      alert_generator.generate_alert()
    - function arg bot_token: str (required) — Telegram bot token
    - function arg chat_id: str (required) — Telegram chat ID
    - function arg cooldown_seconds: int (optional, default 120)
    - function arg bucket_cooldown_seconds: int (optional, default
      infra.cooldown.DEFAULT_BUCKET_COOLDOWN = 300)
    - function arg vision_result: dict (optional) — Qwen raw output
    - function arg quiet_hour_now_override: datetime | None (optional)

OUTPUTS:
    - return value: bool (True on successful send, False otherwise)
    - network call: up to 3 Telegram messages per call (photo, vision
      report, alert text) — delegated to infra/send_telegram.py
    - side effect: bucket-keyed cooldown map is updated via
      infra/cooldown.py

WHY HERE (historical):
    The threat-level-based routing was the single decision point that
    converted alert dicts into Telegram messages. Every alert path
    (motion, match, heartbeat, escalation) used to go through this module.

    Two extraction rounds preceded the archival shape:
      1. Cooldown logic → infra/cooldown.py (commit 5432cd9)
      2. Transport → infra/send_telegram.py + audit → infra/audit_telegram.py
         (Q1 of PLAN §9, 2026-08-13)

    After the transport split, notifier.py owned one thing: gating +
    orchestration. Then the vehicle pipeline (Phase.113, 2026-08-21)
    switched to direct send_telegram calls, leaving notify() with zero
    production callers. Archived 2026-08-28 in Phase.150.

CALLS INTO:
    - infra.cooldown: is_in_cooldown / is_in_bucket_cooldown / make_bucket_key
    - infra.send_telegram: send_photo + send_message (the three-message path)
    - infra.audit_telegram: log_outbound_telegram() once per branch
      (suppressions count as sent=False audit lines)
    - infra._telegram_origin: origin_prefix() for the channel label

RELATED:
    - infra.alert_generator — produces the alert dict
    - infra.cooldown — owns the cooldown map
    - infra.send_telegram — Telegram HTTP transport
    - infra.audit_telegram — audit-side of every send
    - telegram_formatter/match_telegram.py — the module that
      REPLACED the production caller on 2026-08-21
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from infra._telegram_origin import origin_prefix
from infra.audit_telegram import log_outbound_telegram
from infra.cooldown import DEFAULT_BUCKET_COOLDOWN, is_in_vision_block_cooldown
from infra.paths import LOCAL_TZ
from infra.send_telegram import send_message, send_photo

# Channel label for any Telegram message routed through notifier.py
# (L1/L2 + vision-report + direct text). 2026-07-26 — descriptive
# label replacing the meaningless [ALERT] tag.
CHANNEL_LABEL = "CAMERA_ALERT"


DEFAULT_COOLDOWN = 120  # seconds


log = logging.getLogger("notifier")


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def notify(
    alert: dict,
    bot_token: str,
    chat_id: str,
    cooldown_seconds: int = DEFAULT_COOLDOWN,
    bucket_cooldown_seconds: int = DEFAULT_BUCKET_COOLDOWN,
    vision_result: dict | None = None,
    quiet_hour_now_override: datetime | None = None,
) -> bool:
    """
    Route an alert to Telegram based on threat level.

    Args:
        alert: Alert dict matching the Alert Output schema. May optionally
            contain `frame_path` (absolute path to a JPEG) — if present
            and the file exists, sendPhoto is used; otherwise sendMessage.
        bot_token: Telegram bot API token.
        chat_id: Telegram chat ID to send to.
        cooldown_seconds: Suppress duplicate alert_ids within this window
            (short-window dedup for retry storms).
        bucket_cooldown_seconds: Suppress duplicate (camera, title-bucket)
            within this window. Catches the case where each webhook gets a
            new UUID (alert_id-cooldown doesn't help) but the same
            (camera, title) pair keeps generating the same alert (the
            CAM4 overnight vehicle flood pattern — same
            pattern originally on Building Back Solar pre-2026-07-29).
        vision_result: Optional raw vision model output (objects, colors,
            species, scene_description, etc.). If provided, a "vision report"
            message is sent between the photo and the alert text, giving the
            receiver the full picture (raw observations + interpretation).
        quiet_hour_now: Optional override for the current datetime used
            by the quiet-hours filter. Tests inject a fixed datetime so
            they don't depend on wall-clock time. Production callers pass
            None (defaults to datetime.now() in the America/New_York tz).

    Returns:
        True if handled (sent or intentionally suppressed/logged).
        False if Telegram send failed entirely (all transports).
    """
    from infra import cooldown
    from infra.quiet_hours import in_quiet_hours

    threat_level = alert.get("threat_level", 1)
    alert_id = alert.get("alert_id", "")

    # Phase.52 — quiet hours filter (outermost gate).
    # If the camera is in QUIET_HOURS_CAMERAS and the current time is
    # in [21:00, 07:00) America/New_York, suppress Telegram emission.
    # Frames still captured (this filter runs AFTER frame capture, in the
    # notifier), vision still ran, state still updates — only Telegram
    # is silenced. Audit log emits `QUIET_HOURS_SUPPRESSED` so the drop
    # is observable. Level2 alerts from silenced cameras are also caught
    # by this filter — the operator has explicitly chosen silence over real
    # nighttime level2 (covered by 6B.52 PRD "Out of scope").
    now = (
        quiet_hour_now_override
        if quiet_hour_now_override is not None
        else datetime.now(LOCAL_TZ)
    )
    camera = alert.get("camera", "")
    if in_quiet_hours(now, camera):
        local_t = now.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")
        log.info(
            f"QUIET_HOURS_SUPPRESSED channel=alert_notifier "
            f"camera={camera!r} alert_id={alert_id!r} "
            f"event=level{threat_level} local_time={local_t} "
            f"reason=quiet_hours"
        )
        image_paths = [alert["frame_path"]] if alert.get("frame_path") else []
        log_outbound_telegram(
            channel="alert_notifier",
            alert_id=alert_id,
            v_id="",
            event=f"level{threat_level}",
            body=alert.get("description", "") or alert.get("title", ""),
            sent=False,
            extra="suppression_reason=quiet_hours",
            image_paths=image_paths,
        )
        return True

    # Level 0: log only, no Telegram
    if threat_level == 0:
        log.info(f"Level 0 (log only): {alert.get('title', 'unknown')}")
        # Phase.36: image_paths reflects what would have been sent
        # (the level1+ photo path) so the audit line is honest about
        # which image was suppressed. If frame_path missing, [].
        image_paths = [alert["frame_path"]] if alert.get("frame_path") else []
        log_outbound_telegram(
            channel="alert_notifier",
            alert_id=alert_id,
            v_id="",
            event="level0_log",
            body=alert.get("description", "") or alert.get("title", ""),
            sent=False,
            extra="suppression_reason=level0_log_only",
            image_paths=image_paths,
        )
        return True

    # Short-window cooldown (per-alert_id): same UUID within cooldown_seconds
    # gets suppressed — catches retry storms from the same webhook.
    if cooldown.is_in_cooldown(alert_id, cooldown_seconds):
        log.info(f"Suppressed (cooldown): {alert.get('title', 'unknown')}")
        image_paths = [alert["frame_path"]] if alert.get("frame_path") else []
        log_outbound_telegram(
            channel="alert_notifier",
            alert_id=alert_id,
            v_id="",
            event=f"level{threat_level}",
            body=alert.get("description", "") or alert.get("title", ""),
            sent=False,
            extra="suppression_reason=alert_id_cooldown",
            image_paths=image_paths,
        )
        return True

    # Bucket cooldown (per camera + title_bucket): different alert_ids
    # but the same (camera, title_bucket) within bucket_cooldown_seconds
    # get suppressed — kills the overnight vehicle flood.
    bucket_key = cooldown.make_bucket_key(alert)
    if bucket_key and cooldown.is_in_bucket_cooldown(
        bucket_key, bucket_cooldown_seconds
    ):
        log.info(
            f"Suppressed (bucket cooldown): {alert.get('title', 'unknown')}"
        )
        image_paths = [alert["frame_path"]] if alert.get("frame_path") else []
        log_outbound_telegram(
            channel="alert_notifier",
            alert_id=alert_id,
            v_id="",
            event=f"level{threat_level}",
            body=alert.get("description", "") or alert.get("title", ""),
            sent=False,
            extra=f"suppression_reason=bucket_cooldown bucket_key={bucket_key}",
            image_paths=image_paths,
        )
        return True

    # Validate inputs
    if not bot_token:
        log.warning("Missing bot token")
        image_paths = [alert["frame_path"]] if alert.get("frame_path") else []
        log_outbound_telegram(
            channel="alert_notifier",
            alert_id=alert_id,
            v_id="",
            event=f"level{threat_level}",
            body=alert.get("description", "") or alert.get("title", ""),
            sent=False,
            extra="suppression_reason=missing_bot_token",
            image_paths=image_paths,
        )
        return False

    if not chat_id:
        log.warning("Missing chat ID")
        image_paths = [alert["frame_path"]] if alert.get("frame_path") else []
        log_outbound_telegram(
            channel="alert_notifier",
            alert_id=alert_id,
            v_id="",
            event=f"level{threat_level}",
            body=alert.get("description", "") or alert.get("title", ""),
            sent=False,
            extra="suppression_reason=missing_chat_id",
            image_paths=image_paths,
        )
        return False

    # Build message text
    text = _format_message(alert, threat_level)

    # Decide: photo first (no caption), then vision report, then alert text
    # Three-message order so receiver sees: (1) raw photo, (2) what the vision
    # model saw (structured observations), (3) text-model interpretation + advice.
    frame_path = alert.get("frame_path")

    # Verify frame exists before attempting send. send_telegram handles
    # missing files inside the try/except, but we want to skip the photo
    # leg cleanly here so the audit line correctly reports photo_ok.
    import os
    photo_ok = False
    if frame_path and os.path.isfile(frame_path):
        photo_ok = send_photo(
            bot_token, chat_id, frame_path,
            alert_id=alert_id,
            channel="alert_notifier",
            event="level0_text",
        )

    # Vision report: only if vision_result was provided and the alert went
    # to Telegram (Level 1+). Format gracefully degrades if any field is missing.
    # Phase.101 — global 30-min rate-limit on the vision block. The alert
    # body and photo always send; only the optional 🔍 VISION_CAM4ERVATIONS
    # bubble is throttled. Silent in Telegram; logged at INFO for audit.
    if vision_result:
        vision_text = _format_vision_message(vision_result)
        if vision_text:
            if is_in_vision_block_cooldown():
                log.info(
                    f"[{alert_id}] vision_block suppressed (global cooldown): "
                    f"camera={alert.get('camera_name', '?')}"
                )
            else:
                # 2026-07-26: prepend [filename] [VISION_CAM4ERVATIONS] tag so the
                # vision-block Telegram message is identifiable in chat history.
                # the operator asked specifically for the script filename + a meaningful
                # channel label. This was previously a no-prefix message — the
                # only L1-alert child message that didn't carry a tag.
                vision_tag = origin_prefix("VISION_CAM4ERVATIONS")
                send_message(
                    bot_token, chat_id, f"🔍 {vision_tag}\n\n{vision_text}",
                    alert_id=alert_id,
                    channel="alert_notifier",
                    event="level1_vision",
                )

    # Alert text (interpretation + recommendations)
    text_ok = send_message(
        bot_token, chat_id, text,
        alert_id=alert_id,
        channel="alert_notifier",
        event="alert_text",
    )

    # Consider it a failure only if the alert text didn't go through.
    # Photo and vision report are nice-to-haves; the alert text is critical.
    if not text_ok:
        return False

    # Cooldown timestamps are recorded by is_in_cooldown() /
    # is_in_bucket_cooldown() on the miss path above, so we don't
    # need to record them again here.
    if vision_result:
        transport = "photo+vision+text" if photo_ok else "vision+text"
    else:
        transport = "photo+text" if photo_ok else "text-only"
    log.info(
        f"Sent Level {threat_level} alert ({transport}): {alert.get('title', 'unknown')}"
    )
    # OUTBOUND_TELEGRAM audit line — successful send. Body is the
    # formatted alert text (or whatever was last sent). Single tag so
    # `grep OUTBOUND_TEGRAM` finds it regardless of which suppression
    # branch could have applied.
    # Phase.36: image_paths is the photo that was attached (or [] for
    # text-only). This is the source of truth for "what picture was sent
    # with this alert" — the operator's request.
    log_outbound_telegram(
        channel="alert_notifier",
        alert_id=alert_id,
        v_id="",
        event=f"level{threat_level}",
        body=text,
        sent=True,
        extra=f"transport={transport}",
        image_paths=[frame_path] if frame_path and photo_ok else [],
    )
    return True


# --------------------------------------------------------------------------
# Private — message text formatting (HTML)
# --------------------------------------------------------------------------
#
# These are HTML formatters used only by the threat-level routing path
# (parse_mode=HTML). The plain-text renderers in /telegram_formatter/ are
# a separate system used by the pipeline/orchestrator path. Different
# rendering surface, different rendering rules — they coexist by design.
# --------------------------------------------------------------------------

from html import escape as _html_escape


def _format_message(alert: dict, threat_level: int) -> str:
    """
    Format alert for Telegram. HTML parse_mode — only <, >, &, " need escape.

    Emoji escalation:
        Level 1 → ⚠️  (warning)
        Level 2 → 🚨 (critical/siren)
        other   → ⚠️  (fail-safe to Level 1)

    All user/vision-supplied text is HTML-escaped. The only structural HTML
    tags we add (<b>, <i>) are safe by construction.
    """
    if threat_level == 2:
        emoji = "🚨"
    else:
        emoji = "⚠️"

    camera = _html_escape(alert.get("camera", "Unknown camera"))
    title = _html_escape(alert.get("title", "Untitled alert"))
    description = _html_escape(alert.get("description", "No description."))
    recommendations_raw = alert.get("recommendations", []) or []
    recommendations = [_html_escape(r) for r in recommendations_raw]
    timestamp = _html_escape(alert.get("timestamp", ""))
    source = _html_escape(alert.get("source", ""))

    # 2026-07-26: replace literal "[ALERT]" tag with the descriptive
    # origin prefix ([filename] [CAMERA_ALERT]). The filename is
    # auto-detected via inspect.stack() in notifier.py so we report
    # whichever application file (alert_listener, vehicle_event_handler,
    # etc.) actually called into notifier.
    prefix = origin_prefix(CHANNEL_LABEL)
    lines = [f"{emoji} {prefix} [{camera}] {title}", "", description]

    if recommendations:
        lines.append("")
        lines.append("<b>Recommended actions:</b>")
        for rec in recommendations:
            lines.append(f"• {rec}")

    # Footer with timestamp and source (for traceability)
    footer_parts = []
    if timestamp:
        footer_parts.append(timestamp)
    if source:
        footer_parts.append(source)
    if footer_parts:
        lines.append("")
        lines.append(f"<i>{' · '.join(footer_parts)}</i>")

    return "\n".join(lines)


def _format_vision_message(vision_result: dict) -> str:
    """
    Format raw vision model output as a Telegram message. Sent between the
    photo and the alert text so the receiver sees:
      (1) raw photo
      (2) what the vision model observed (objects, scene, colors, species)
      (3) text-model interpretation + recommendations

    Format is HTML. All values are HTML-escaped. Sections that are empty
    or None are skipped — the message degrades gracefully for partial data.
    """
    if not isinstance(vision_result, dict):
        return ""

    lines = ["🔍 <b>Vision Model Observations</b>", ""]

    # Scene description — the most informative single field
    scene = vision_result.get("scene_description", "")
    if scene:
        lines.append(f"<b>Scene:</b> {_html_escape(scene)}")
        lines.append("")

    # Primary subject + actions
    primary = vision_result.get("primary_subject", "")
    actions = vision_result.get("actions", []) or []
    if primary and primary != "none":
        lines.append(f"<b>Primary subject:</b> {_html_escape(primary)}")
        if actions:
            actions_str = ", ".join(str(a) for a in actions)
            lines.append(f"<b>Actions:</b> {_html_escape(actions_str)}")
        lines.append("")

    # Objects detected
    objects = vision_result.get("objects_detected", []) or []
    if objects:
        objects_str = ", ".join(str(o) for o in objects)
        lines.append(f"<b>Objects detected:</b> {_html_escape(objects_str)}")
        lines.append("")

    # Colors (for greeting via speaker / visual context)
    colors = vision_result.get("colors", {})
    color_parts = []
    if isinstance(colors, dict):
        vehicle_color = colors.get("vehicle")
        if vehicle_color:
            color_parts.append(f"vehicle: {vehicle_color}")
        clothing_primary = colors.get("clothing_primary")
        if clothing_primary:
            color_parts.append(f"clothing: {clothing_primary}")
        clothing_secondary = colors.get("clothing_secondary")
        if clothing_secondary:
            color_parts.append(f"also wearing: {clothing_secondary}")
        other_color = colors.get("other")
        if other_color:
            color_parts.append(f"other: {other_color}")
    if color_parts:
        lines.append(f"<b>Colors:</b> {_html_escape(' · '.join(color_parts))}")
        lines.append("")

    # Species (for animal identification)
    species = vision_result.get("species")
    if species and species != "null":
        lines.append(f"<b>Species:</b> {_html_escape(species)}")
        lines.append("")

    # Notable details — defensive about callers that pass a string instead
    # of a list. Real pipeline emits a list of pre-bulleted lines (one per
    # detail). If a caller accidentally passes a "\n"-joined string, each
    # character would otherwise become its own bullet (verified bug
    # 2026-07-26 by reviewing a Telegram message that listed each char of
    # the notable-details text as a separate "•" bullet). Normalize here.
    raw_notable = vision_result.get("notable_details", []) or []
    if isinstance(raw_notable, str):
        notable = [line.strip() for line in raw_notable.splitlines() if line.strip()]
    else:
        notable = list(raw_notable)
    if notable:
        lines.append("<b>Notable details:</b>")
        for detail in notable:
            lines.append(f"• {_html_escape(str(detail))}")
        lines.append("")

    # Confidence (for trust calibration — useful for filtering false positives)
    confidence = vision_result.get("confidence")
    if isinstance(confidence, (int, float)) and confidence > 0:
        # Render as percentage with 0 decimals
        lines.append(f"<i>Confidence: {confidence * 100:.0f}%</i>")

    # Strip trailing empties, but keep header if any content was added
    while lines and lines[-1] == "":
        lines.pop()

    # If only the header remains (no real content), return empty string so the
    # caller doesn't send a useless "🔍 Vision Model Observations" with nothing under it.
    if len(lines) <= 1:
        return ""

    return "\n".join(lines)
