"""
alert — Stage 5: call the threat-level LLM to produce the alert body.

STATUS: stable
THREAD SAFETY: single-threaded (one call per alert; runs inside camera semaphore)

INPUTS:
    - ctx: AlertContext (vision_result, camera_name, timestamp,
      is_vehicle_event, match_verdict, api_url)

OUTPUTS:
    - mutates ctx.alert: dict with title, threat_level, summary
        (placeholder if generate_alert fails)

PUBLIC API:
    generate_alert_stage(ctx: AlertContext) -> None
        Stage 5 driver. Calls infra.alert_generator.generate_alert()
        which uses Qwen3.5-9B at :8081. The "source" param is
        "match" for vehicle+match, "rtsp_frames" otherwise. The match-
        alert Telegram body override is applied LATER, in
        emit_result_stage (after match_loop populates ctx.match_alerts).

DOES NOT DO:
    - Send the alert to Telegram — emit_result_stage owns that.
    - Apply alert_overrides — that layer (infra.alert_overrides)
      runs inside generate_alert(); not re-applied here.
    - Decide which Telegram fires — match_loop in emit owns TG#3.

WHY HERE:
    The LLM-generated alert body is the system-of-record for
    state counters + audit + arrival detection. Keeping it as
    a single stage makes the call-site in process_alert() linear.

CALLED BY:
    - process_alert (in __init__.py) — Stage 5 driver

CALLS INTO:
    - infra.alert_generator.generate_alert — LLM call

RELATED:
    - §11.91 in PLAN.md — alert_overrides post-LLM hook
    - §11.121 — match-alert Telegram body override (applied in emit)
"""
from __future__ import annotations

import logging

from .context import AlertContext

log = logging.getLogger(__name__)


def generate_alert_stage(ctx: AlertContext) -> None:
    """Stage 5: call the threat-level LLM (Qwen3.5-9B at :8081) to
    produce the alert body (title, summary, threat_level).

    Phase 6B.91: alert_overrides may downgrade threat_level for baseline
    windows (e.g. delivery during business hours) or offhours (e.g. all
    quiet hours). Overrides applied AFTER LLM response.

    Phase 6B.121 (2026-08-22): the match-alert Telegram body override
    for confident matches is applied LATER, in emit_result_stage, after
    the match_loop has populated ctx.match_alerts. Doing it here would
    require running the matcher early (before the LLM call), which
    breaks the existing 6B.112 ordering (composite TG#2 fires before
    matcher loop per maintainer OOB 2026-08-21). So we just leave a
    placeholder here; emit_result_stage replaces ctx.alert with the
    match body before append_alert().

    Mutates ctx: alert (placeholder set; real value applied in
    emit_result_stage after match loop).
    """
    from infra.alert_generator import generate_alert

    log.info(f"[{ctx.alert_id}] generate_alert_stage: starting")

    # Determine the "source" for the LLM. Vehicle+match: "match"; vehicle
    # without match: "rtsp_frames" (Qwen-9B re-derives a more specific
    # class); non-vehicle: "rtsp_frames".
    source = "match" if (ctx.is_vehicle_event and ctx.match_verdict is not None) else "rtsp_frames"

    try:
        ctx.alert = generate_alert(
            vision_result=ctx.vision_result or {},
            camera_name=ctx.camera_name,
            timestamp=ctx.timestamp,
            source=source,
            api_url=ctx.api_url,
        )
    except Exception as err:
        log.warning(f"[{ctx.alert_id}] generate_alert raised: {err}")
        ctx.alert = {"title": "error", "threat_level": 0, "summary": ""}
