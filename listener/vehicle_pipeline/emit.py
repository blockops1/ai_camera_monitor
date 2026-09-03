"""
emit — Stage 6: finalize the alert and emit it (TG#2 + TG#3 + audit + state).

STATUS: stable
THREAD SAFETY: single-threaded (one call per alert; runs OUTSIDE the
    camera semaphore — uses no per-camera resource)

INPUTS:
    - ctx: AlertContext (alert from stage 5, best_frame_path from stage 4,
      match_alerts from match_loop)

OUTPUTS:
    - returns dict for the listener:
        {"event_type", "vehicle_match", "telegram_sent", "telegram_error", "alert_id"}
    - calls append_alert() (audit), send_composite_alert() (TG#2),
      _emit_match_loop() (TG#3), updates STATE counters.

PUBLIC API:
    emit_result_stage(ctx: AlertContext) -> dict
        Stage 6 driver. Order:
            1. alert_id suffix (vehicle: "-identified")
            2. frame_path attachment
            3. error handling (alert["title"] == "error")
            4. arrival detection (L0 → L1 bump if person + arrival)
            5. phase 6A face recognition
            6. audit append (deferred for gatekeeper vehicles)
            7. composite Telegram (TG#2)
            8. match_loop (TG#3, per vehicle)
            9. state update (total_alerts, by_threat_level, last_alert)
    _result_dict(ctx: AlertContext, sent: bool) -> dict
        Build the listener's per-alert result dict.

DOES NOT DO:
    - Fire TG#1 — identify_stage owns that.
    - Run motion detector / vision / matcher — earlier stages own those.
    - Persist outside alert_history.jsonl — no other disk write.

WHY HERE:
    Stage 6 owns the post-LLM orchestration — what gets persisted,
    what Telegrams fire in what order, what the state counters see.
    Keeping it as a single stage makes the order explicit and
    testable. Order matters: composite TG#2 must fire before
    match_loop TG#3 (Note 2026-08-21).

CALLED BY:
    - process_alert (in __init__.py) — Stage 6 driver

CALLS INTO:
    - infra.alert_history.append_alert — audit (BEFORE Telegram)
    - infra.arrival._vision_shows_person, is_arrival — L0 → L1 bump
    - infra.pipeline_integration.run_phase6a_recognition — face rec
    - infra.vision_cache.record_person_seen — arrival timestamp cache
    - listener.state.STATE — counter updates
    - telegram_formatter.composite_telegram.send_composite_alert — TG#2
    - match._emit_match_loop — TG#3 per-vehicle (relative import)
    - match._vision_summary_str — TG#2 body summary (relative import)

RELATED:
    - §11.111 in PLAN.md — extraction rationale
    - §11.122 — match-body swap (TG#3 body into ctx.alert for audit)
    - §11.123 — L1/L2/L3 threat-level routing per vehicle event
    - §11.168 — camera_code boundary translation at gatekeeper check
"""
from __future__ import annotations

import logging

from .context import AlertContext
from .match import _emit_match_loop, _vision_summary_str

log = logging.getLogger(__name__)


def emit_result_stage(ctx: AlertContext) -> dict:
    """Stage 6: finalize the alert and emit it.

    Order matters:
        1. alert_id suffix (vehicle: "-identified" so cooldown treats msg 2 as independent)
        2. frame_path attachment
        3. error handling (alert["title"] == "error")
        4. arrival detection (L0 → L1 bump if person + arrival)
        5. phase 6A recognition (face recognition + property state)
        6. audit append (BEFORE notify per 6B.24 §5)
        7. notify (skip if audit failed)
        7.5. composite Telegram (Phase.111 — motion-trail visualization
             with bbox overlays; fires AFTER the lead motion Telegram so
             the lead motion body + photo don't get delayed by the ~140ms
             composite render. Failure-tolerant: never suppresses the
             lead motion Telegram that already went out.)
        8. state update (total_alerts, by_threat_level, last_alert)

    Returns: result dict for the listener.
        {
            "event_type": final_event_type,
            "vehicle_match": match_verdict,
            "telegram_sent": sent,
            "telegram_error": None,
            "alert_id": alert_id,
        }
    """
    from infra.alert_history import append_alert

    # Lazy import of infra helpers
    from infra.arrival import _vision_shows_person, is_arrival
    from infra.pipeline_integration import run_phase6a_recognition
    from infra.vision_cache import record_person_seen

    # Phase.112: notify() (the LLM-generated alert body Telegram)
    # was removed from this stage. The slim previously sent a single
    # Telegram with the Qwen9B-generated title+summary + best-frame
    # Note's spec (2026-08-21) defines a 3-Telegram gatekeeper stack:
    #   TG#1 = "vehicle detected" (arriving) — fires from identify_stage
    #   TG#2 = "vehicle in motion" + composite motion-trail photo — fires HERE
    #   TG#3 = match/no-match + 3-crop composite photo — fires HERE (per vehicle)
    # The LLM-generated ctx.alert is still used by:
    #   - state counters (STATE["by_threat_level"])
    #   - audit (append_alert) — persists the LLM summary to history
    #   - arrival detection (L0 → L1 bump if person + arrival)
    # But it is NOT sent to Telegram anymore. Per-vehicle match loop
    # handles TG#3.

    log.info(f"[{ctx.alert_id}] emit_result_stage: starting")

    # 1. alert_id suffix
    if ctx.is_vehicle_event:
        ctx.alert["alert_id"] = f"{ctx.alert_id}-identified"
    else:
        ctx.alert["alert_id"] = ctx.alert_id

    # 2. frame_path attachment
    ctx.alert["frame_path"] = ctx.best_frame_path

    # 3. error handling
    if ctx.alert.get("title") == "error":
        log.error(f"[{ctx.alert_id}] emit_result_stage: alert generation failed")
        try:
            from state import STATE
        except ImportError:
            from listener.state import STATE
        STATE["by_threat_level"][-1] += 1
        return _result_dict(ctx, sent=False)

    log.info(
        f"[{ctx.alert_id}] emit_result_stage: alert generated "
        f"Level {ctx.alert.get('threat_level')} — {ctx.alert.get('title')}"
    )

    # 4. arrival detection
    if ctx.alert.get("threat_level") == 0 and _vision_shows_person(ctx.vision_result):
        if is_arrival(ctx.camera_name):
            log.info(f"[{ctx.alert_id}] Arrival detected — bumping L0 → L1")
            ctx.alert["threat_level"] = 1
            ctx.alert["source"] = "arrival"
            title_lower = (ctx.alert.get("title") or "").lower()
            if any(
                kw in title_lower
                for kw in ["routine", "no threat", "all clear", "no concern"]
            ):
                ctx.alert["title"] = (
                    f"Arrival detected — Person present in {ctx.camera_name}"
                )
        # Always record the person-seen timestamp so future motion events
        # within the gap are NOT classified as arrivals.
        record_person_seen(ctx.camera_name, when_iso=ctx.timestamp)

    # 5. phase 6A face recognition
    try:
        run_phase6a_recognition(
            frame_paths=ctx.frame_paths,
            vision_result=ctx.vision_result,
            camera=ctx.camera_name,
        )
    except Exception as err:
        log.warning(f"[{ctx.alert_id}] Phase 6A swallowed at caller: {err}")

    # Phase.122 (2026-08-22, Note): defer append_alert until
    # AFTER _emit_match_loop, so alert.jsonl can capture the match
    # body that TG#3 actually sent (not the LLM-fallback's generic L0
    # title).
    #
    # History: 6B.121 placed a swap BEFORE append_alert, but that was
    # wrong because _emit_match_loop runs AFTER append_alert in this
    # stage. The swap saw an empty match_alerts list and was a silent
    # no-op. Confirmed via b079e97a (red Jeep, 11:23 EDT): TG#3 said
    # "❌ No match" but alert.jsonl said "Normal Daytime Scene - No
    # Activity Detected".
    #
    # Defer append_alert until after match_loop. Track via _defer_alert.
    # Phase.168 (2026-08-31): same camera_code-vs-gatekeeper gate as
    # TG#1 (L531) and match_stage (L652). Pre-fix used camera_name which
    # silently failed every vehicle event.
    _defer_alert = ctx.is_vehicle_event and ctx.camera_code in (
        ctx.gatekeeper_cameras or frozenset()
    )
    if not _defer_alert:
        # 6. audit append (BEFORE Telegram sends) — non-vehicle and
        # non-gatekeeper paths take this branch immediately.
        history_ok = append_alert(ctx.alert)
    else:
        # Vehicle/gatekeeper path — append_alert deferred until after
        # match_loop below so we can capture the match body.
        history_ok = True
    if not history_ok:
        log.warning(
            f"[{ctx.alert_id}] outbox_failed: append_alert returned False — "
            f"skipping Telegram sends to avoid orphan messages."
        )

    # Phase.112: split the single notify() (LLM body Telegram) into
    # the 3-Telegram gatekeeper message stack (TG#2 + TG#3). TG#1 already fired
    # from identify_stage. The legacy notify() call is GONE — the LLM
    # body is no longer sent as a Telegram; it's used for state +
    # audit + arrival detection only.
    sent = bool(history_ok)  # Only False if audit failed

    # 7. composite motion-trail Telegram (TG#2 in the gatekeeper stack).
    # Fires AFTER TG#1 (arriving — fired in identify_stage). Per Note
    # OOB 2026-08-21: "the matcher should run after the other two
    # alerts are sent to me." So this fires BEFORE the matcher loop.
    # Failure-tolerant: any failure logs + skips; doesn't block match
    # loop or state update. Skipped if motion_result has no primary.
    # Phase.168 (2026-08-31): same camera_code gate as TG#1.
    # Non-gatekeeper vehicle events skip TG#2 too.
    if (
        sent
        and ctx.is_vehicle_event
        and ctx.camera_code in (ctx.gatekeeper_cameras or frozenset())
        and ctx.motion_result is not None
    ):
        try:
            from telegram_formatter.composite_telegram import (
                send_composite_alert,
            )
            primary = ctx.motion_result.primary_moving_object
            # Phase.115: read bbox_a + bbox_b from the gate verdict
            # (gate's diff bboxes in native coords). trajectory comes
            # from motion_result.primary_moving_object.trajectory
            # which is now built from the gate's bboxes (4 cells).
            verdict = getattr(ctx, "gate_verdict", None)
            bbox_a = getattr(verdict, "bbox_a", None)
            bbox_b = getattr(verdict, "bbox_b", None)
            trajectory = list(primary.trajectory) if primary else []
            # Build a brief vision summary for TG#2 body. Take the top
            # identified vehicle's make/model/color from vision_result.
            vision_summary = _vision_summary_str(ctx.vision_result)
            composite_sent = send_composite_alert(
                alert_id=ctx.alert_id,
                camera_name=ctx.camera_name,
                frames=ctx.frames,                # Phase.115: in-memory PIL images
                output_dir=ctx.output_dir,         # composite.jpg written here
                bbox_a=bbox_a,
                bbox_b=bbox_b,
                bot_token=ctx.bot_token,
                chat_id=ctx.chat_id,
                captured_at=ctx.timestamp,
                trajectory=trajectory,
                vision_summary=vision_summary,
            )
            log.info(
                f"[{ctx.alert_id}] composite_alert (TG#2): "
                f"{'sent' if composite_sent else 'skipped/failed'}"
            )
        except Exception as err:
            log.warning(
                f"[{ctx.alert_id}] composite_alert: caller caught {err!r}"
            )

    # 8. per-vehicle match loop (TG#3 in the gatekeeper stack).
    # Runs AFTER TG#1 + TG#2 (per Note 2026-08-21). For each vehicle
    # in vision_result["vehicles"] (or wrapped single-vehicle if absent):
    #   - extract signature
    #   - run match_with_details
    #   - send match_alert or no_match_alert
    # Non-gatekeeper cameras skip this entirely (no matcher fires for them).
    # Skipped if match_verdict is missing or no known_vehicles.
    # Phase.168 (2026-08-31): camera_code gate; pre-fix silently
    # dropped every vehicle event here.
    if (
        sent
        and ctx.is_vehicle_event
        and ctx.camera_code in ctx.gatekeeper_cameras
        and ctx.known_vehicles
    ):
        _emit_match_loop(ctx)

    # Phase.122 (2026-08-22, Note): if the match_loop populated
    # ctx.match_alerts with one or more MatchTelegramInput, replace
    # ctx.alert with the first match body so alert.jsonl stays consistent
    # with what TG#3 told the user. Then call append_alert (which was
    # deferred from above so the swap could run first).
    #
    # History: this was originally placed BEFORE append_alert() in
    # 6B.121, but that was wrong because _emit_match_loop runs AFTER
    # append_alert in this stage. The swap saw an empty match_alerts
    # list and was a silent no-op. Confirmed bug via b079e97a (red
    # Jeep, 11:23 EDT): TG#3 said "❌ No match" but alert.jsonl said
    # "Normal Daytime Scene - No Activity Detected".
    if _defer_alert:
        if ctx.match_alerts:
            try:
                first = ctx.match_alerts[0]
                # Phase.122 (2026-08-22): handle BOTH match and
                # no-match Telegram inputs. b079e97a (red Jeep) was a
                # no-match event, and 6B.121's swap only knew about
                # MatchTelegramInput — which is why the no-match case
                # was always going to get the LLM-fallback title.
                # Detect by duck-typing (NoMatchTelegramInput has a
                # `no_match` attribute; MatchTelegramInput has `verdict`).
                if hasattr(first, "no_match"):
                    from telegram_formatter.no_match_telegram import (
                        build_no_match_telegram_body,
                    )
                    body = build_no_match_telegram_body(first)
                    is_match = False
                else:
                    from telegram_formatter.match_telegram import (
                        build_match_telegram_body,
                    )
                    body = build_match_telegram_body(first)
                    is_match = True
                first_line = body.split("\n", 1)[0]
                # Phase.123 (2026-08-22, Note): vehicle threat
                # level routing per Note's spec:
                #   L1 = known vehicle at the gatekeeper (you want Telegram alert)
                #   L2 = unknown vehicle at the gatekeeper (higher alert — no match)
                #   L3 = emergency vehicle (police/ambulance/firetruck)
                #       — NOT YET WIRED — see plan §emergency-vehicles.
                #       Will require (a) emergency-vehicle entries in
                #       known_vehicles.json with vehicle_class=
                #       "emergency" and (b) routing logic in this swap.
                #       Per Note 2026-08-22: "we can add other
                #       categories later." Deferred.
                if is_match:
                    # Known vehicle — match found.
                    # TODO(6B.123+): if known_vehicle has vehicle_class=
                    # "emergency", route to L3 instead of L1. No
                    # entries have that field yet, so all matches → L1
                    # today.
                    threat_level = 1  # L1 — known vehicle
                else:
                    # No match — unknown vehicle. L2 per Note spec.
                    threat_level = 2  # L2 — unknown vehicle
                ctx.alert = {
                    "title": first_line,
                    "threat_level": threat_level,
                    "summary": body,
                }
                if is_match:
                    ctx.alert["matched_vehicle_id"] = (
                        first.verdict.known_vehicle.get("id")
                    )
                    ctx.alert["match_score"] = first.verdict.score
                    ctx.alert["match_gap"] = first.verdict.gap
                log.info(
                    f"[{ctx.alert_id}] emit_result_stage: alert.jsonl "
                    f"replaced with {'match' if is_match else 'no-match'} "
                    f"body "
                    f"level={threat_level} "
                    f"({first_line[:60]!r})"
                )
            except Exception as err:
                log.warning(
                    f"[{ctx.alert_id}] emit_result_stage match-body swap "
                    f"raised {err!r}; persisting original ctx.alert"
                )
        # Append once — with the swapped body if the swap ran, else
        # with the original ctx.alert (e.g. no known_vehicles, etc).
        history_ok = append_alert(ctx.alert)

    # 9. state update
    try:
        from state import STATE
    except ImportError:
        from listener.state import STATE
    STATE["total_alerts"] += 1
    threat_level = ctx.alert.get("threat_level", -1)
    STATE["by_threat_level"][threat_level] = (
        STATE["by_threat_level"].get(threat_level, 0) + 1
    )
    STATE["last_alert"] = {
        "alert_id": ctx.alert_id,
        "camera": ctx.camera_name,
        "timestamp": ctx.timestamp,
        "threat_level": threat_level,
        "title": ctx.alert.get("title"),
        "sent_to_telegram": sent,
        "persisted_to_history": history_ok,
    }

    log.info(
        f"[{ctx.alert_id}] emit_result_stage: complete. "
        f"Telegram: {sent}, History: {history_ok}"
    )

    return _result_dict(ctx, sent=sent)


def _result_dict(ctx: AlertContext, sent: bool) -> dict:
    """Build the listener's per-alert result dict."""
    return {
        "event_type": ctx.event_type,
        "vehicle_match": ctx.match_verdict,
        "telegram_sent": sent,
        "telegram_error": None,
        "alert_id": ctx.alert_id,
    }
