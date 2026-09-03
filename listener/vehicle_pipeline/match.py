"""
match — Stage 3: vehicle matching against known_vehicles + TG#3 per-vehicle loop.

STATUS: stable
THREAD SAFETY: single-threaded (one call per alert; runs inside camera semaphore)

INPUTS:
    - ctx: AlertContext (vision_result, camera_code, gatekeeper_cameras,
      known_vehicles)

OUTPUTS:
    - mutates ctx: match_verdict (MatchVerdict | NoMatch | None),
      score_top_n, match_alerts (list[MatchTelegramInput | NoMatchTelegramInput])
    - calls _emit_match_loop — fires TG#3 per-vehicle Telegrams (from emit)

PUBLIC API:
    match_stage(ctx: AlertContext) -> None
        Stage 3 driver. Gatekeeper cameras only. Suppresses match-alert
        path when Qwen's vision confidence < VISION_CONFIDENCE_FLOOR.
    _extract_signature(ctx: AlertContext) -> dict
        Build signature dict from ctx.vision_result (multi-vehicle first,
        top-level fallback).
    _to_kv_id_score(top_n: list) -> list[tuple]
        Convert (kv, score, breakdowns) tuples to (kv_id, score) pairs.
    _vision_summary_str(vision_result) -> str
        Brief verbatim identification string for TG#2 body. Multi-vehicle
        reorder so primary comes first; joined with ", plus ".
    _emit_match_loop(ctx: AlertContext) -> None
        Per-vehicle match loop — TG#3a (match) or TG#3b (no_match) Telegram.
        Failure-isolated per vehicle.

CONSTANTS:
    VISION_CONFIDENCE_FLOOR: float = 0.3
        Phase.170 (2026-09-01): minimum Qwen confidence for match-alert
        path. Pre-fix matcher ignored vision_result["confidence"] entirely.
        Verified on live alert b136f261 (2026-08-31 21:28:27 EDT) — a
        "consistent with a small enclosed trailer or cargo box" (conf=0.0)
        still produced a Jayco match (false positive). 0.3 is conservative:
        keeps honest "low confidence but I see a vehicle" alive, suppresses
        "I'm guessing" calls.

DOES NOT DO:
    - Fire TG#1/TG#2 — those happen in identify_stage and emit_result_stage.
    - Update ctx.alert — that's emit_result_stage's job (TG#3 body swap).
    - Persist to alert.jsonl — append_alert in emit_result_stage owns that.

WHY HERE:
    All match-related logic (gatekeeper-vs-not routing, signature extraction,
    confidence-floor suppression, match_alert/no_match_alert per vehicle)
    is the matcher domain. Keeping it together makes the match → emit flow
    linear and matches §11.106 + §11.86 split pattern.

CALLED BY:
    - process_alert (in __init__.py) — Stage 3 driver
    - emit_result_stage (in emit.py) — calls _emit_match_loop after TG#2

CALLS INTO:
    - vehicle_matcher.matcher.MatchVerdict, NoMatch — verdict dataclasses
    - vehicle_matcher.matcher as match — for early reference in confidence check
    - infra.vehicle_matcher.match_vehicle_scored, score_top_n, match_with_details
    - vehicle_identifier.signature.extract_signature — multi-vehicle signature
    - known_vehicles.load_known_vehicles — lazy load if not passed
    - telegram_formatter.match_telegram — TG#3a send + body builder
    - telegram_formatter.no_match_telegram — TG#3b send + body builder
    - notify._format_vehicle_summary — vehicle summary formatter (relative import)

RELATED:
    - §11.170 in PLAN.md — VISION_CONFIDENCE_FLOOR rationale
    - §11.121 — TG#3 swap into ctx.alert (in emit_result_stage)
    - b136f261 — the false-positive canary alert
"""
from __future__ import annotations

import logging

from .context import AlertContext
from .notify import _format_vehicle_summary

# Phase.116 + §11.170 (2026-09-02): module-level imports of
# MatchVerdict + NoMatch so _emit_match_loop can use them bare, AND
# so `sys.modules` registers the package form before any function
# runs. See __init__.py docstring for the full shadowing rationale.
from vehicle_matcher.matcher import MatchVerdict, NoMatch

log = logging.getLogger(__name__)


# Phase.170 (2026-09-01): confidence floor for the
# match-alert path. match_stage() suppresses the match-alert Telegram
# chain (TG#3 + composite TG#2 after the re-org) when Qwen3-VL's
# confidence is below this value. The matcher itself still runs (no
# behavior change there — score_vehicle never read confidence). This
# is the smallest change that closes the b136f261 false-positive hole
# without retraining Qwen or touching the matcher.
VISION_CONFIDENCE_FLOOR: float = 0.3


def match_stage(ctx: AlertContext) -> None:
    """Stage 3: match the identified vehicle against known vehicles.

    Gatekeeper cameras (post-6B.104) run the match-alert path:
        match_vehicle_scored + score_top_n + match-alert gate.
    Non-gatekeeper cameras skip the match-alert path (the lead-motion
    Telegram has already been sent; no match-alert Telegram stack).

    Mutates ctx: match_verdict, score_top_n.
    """
    if not ctx.is_vehicle_event:
        # Non-vehicle events don't match.
        return

    if ctx.camera_code not in ctx.gatekeeper_cameras:
        # Phase.168 (2026-08-31): compare camera_code (CAM{N}) against
        # the code-keyed gatekeeper set. Pre-fix compared camera_name
        # (friendly) and silently failed every event to this branch —
        # see ctx.camera_code docstring.
        log.info(
            f"[{ctx.alert_id}] match_stage: {ctx.camera_name} "
            f"(code={ctx.camera_code or '?'}) is not a "
            f"gatekeeper — skipping match-alert path"
        )
        return

    from infra.vehicle_matcher import match_vehicle_scored, score_top_n

    # Lazily load the known-vehicles list if the caller didn't pass one in.
    if not ctx.known_vehicles:
        from known_vehicles import load_known_vehicles
        ctx.known_vehicles = load_known_vehicles()

    # Build the signature from the vision_result.
    signature = _extract_signature(ctx)

    if not signature:
        log.info(f"[{ctx.alert_id}] match_stage: no signature — no match")
        return

    # Phase.170 (2026-09-01): suppress match-alert path when
    # Qwen's vision confidence is below the FLOOR. Pre-fix the matcher
    # ignored `vision_result["confidence"]` entirely — it only scored
    # (color, type, make, model, vehicle_features) against known vehicles.
    # A vehicle event with confidence=0.0 (Qwen returning "consistent with
    # a small enclosed trailer or cargo box" with no wheels/lights/badges
    # visible) still passed color=white + type=trailer to the matcher and
    # produced a Jayco match (live alert b136f261 at 2026-08-31 21:28:27
    # EDT, Outside Front Solar/CAM5 — match_score=2.00, gap=0.50, false
    # positive Telegram #3). Qwen's confidence is the only signal we have
    # that says "I'm not sure this is a vehicle" — ignoring it costs us
    # false-positive match alerts on bright artifacts and edge cases.
    #
    # Threshold rationale: Qwen3-VL's confidence is well-calibrated for
    # vehicles it can actually see (verified ≥0.7 on matched alerts,
    # review data/frames/*/raw_vision_multi.json from 2026-08-30/31).
    # Values <0.3 correlate with "consistent with / appears to be / could
    # be" hedge descriptions and missing-feature signatures (wheel_style,
    # wheel_arch, badge_text_readable, etc. all null). 0.3 is conservative
    # — keeps any honest "low confidence but I see a vehicle" call alive
    # and only suppresses "I'm guessing" calls.
    #
    # We reference `match.NoMatch` (the module, not the symbol) because
    # match_stage has a lazy `from vehicle_matcher.matcher import NoMatch`
    # below this block — using the bare name here would trigger
    # Python's UnboundLocalError rule.
    from vehicle_matcher import matcher as match
    vision_conf = ctx.vision_result.get("confidence") if ctx.vision_result else None
    if isinstance(vision_conf, (int, float)) and vision_conf < VISION_CONFIDENCE_FLOOR:
        log.info(
            f"[{ctx.alert_id}] match_stage: vision confidence "
            f"{vision_conf:.2f} < floor {VISION_CONFIDENCE_FLOOR:.2f} — "
            f"suppressing match-alert path"
        )
        ctx.match_verdict = match.NoMatch(
            reason="low_vision_confidence",
            top_candidates=[],
        )
        return

    # Primary match
    match_result = match_vehicle_scored(
        sig=signature,
        known=ctx.known_vehicles,
    )

    # Top-N for the no-match Telegram body
    ctx.score_top_n = score_top_n(
        sig=signature,
        known=ctx.known_vehicles,
        n=3,
    )

    if match_result is None:
        log.info(f"[{ctx.alert_id}] match_stage: no match")
        from vehicle_matcher.matcher import (
            NoMatch,  # 6B.90 package-form (per <legacy-repo>-workflow skill)
        )
        ctx.match_verdict = NoMatch(
            reason="below_threshold",
            top_candidates=_to_kv_id_score(ctx.score_top_n),
        )
    else:
        # Legacy returns (matched_kv, top_score, gap, all_breakdowns)
        # where all_breakdowns[kv_id] = {dim: score}.
        # Modular MatchVerdict wants breakdowns for the top candidate only.
        matched_kv, top_score, gap, all_breakdowns = match_result
        top_kv_id = matched_kv.get("id", "?")
        top_breakdowns = all_breakdowns.get(top_kv_id, {})
        from vehicle_matcher.matcher import MatchVerdict
        ctx.match_verdict = MatchVerdict(
            known_vehicle=matched_kv,
            score=top_score,
            gap=gap,
            breakdowns=top_breakdowns,
            rank=0,
            all_scores=_to_kv_id_score(ctx.score_top_n),
        )
        log.info(
            f"[{ctx.alert_id}] match_stage: matched "
            f"{top_kv_id} (score={top_score:.2f})"
        )


def _extract_signature(ctx: AlertContext) -> dict:
    """Build the signature dict from ctx.vision_result.

    Phase.129b (§11.52): now reads from vehicles[] first (the
    multi-vehicle schema), falling back to top-level fields (the
    legacy single-vehicle schema). For backward compat with old
    Qwen responses that only populate top-level fields, we keep
    the legacy fallback intact.
    """
    if not ctx.vision_result:
        return {}
    vr = ctx.vision_result
    # Multi-vehicle schema: pick vehicles[primary_vehicle_index] if present.
    vehicles = vr.get("vehicles") or []
    if vehicles and isinstance(vehicles, list) and isinstance(vehicles[0], dict):
        pvi = vr.get("primary_vehicle_index", 0)
        if not isinstance(pvi, int) or pvi < 0 or pvi >= len(vehicles):
            pvi = 0
        v = vehicles[pvi]
        return {
            "color": v.get("color", ""),
            "type": v.get("type", "") or v.get("body_style_hint", ""),
            "make": v.get("make", ""),
            "model": v.get("model", ""),
            "vehicle_features": v.get("vehicle_features", []),
        }
    # Single-vehicle schema (top-level fields, legacy compat)
    return {
        "color": vr.get("color", ""),
        "type": vr.get("type", ""),
        "make": vr.get("make", ""),
        "model": vr.get("model", ""),
        "vehicle_features": vr.get("vehicle_features", []),
    }


def _to_kv_id_score(top_n: list) -> list[tuple]:
    """Convert (kv, score, breakdowns) tuples to (kv_id, score) pairs."""
    return [(kv.get("id", "?"), score) for kv, score, _ in top_n]


def _vision_summary_str(vision_result) -> str:
    """Build a brief verbatim identification string from vision_result.

    Used by TG#2 (composite Telegram body) — the "identified as:" line.

    Phase.129 (initial): looked at vision_result["vehicles"][0] first
    (multi-vehicle schema) then fell back to top-level fields.

    Phase.130 (§11.53): handles multi-vehicle results correctly.
      1. Reads vehicles[] (multi-vehicle schema)
      2. Reorders so primary_vehicle_index's vehicle comes FIRST
      3. Joins each non-empty identification with ", plus " so the operator
         sees every vehicle Qwen identified (the primary first, then
         secondary/incidental vehicles in the order Qwen emitted them).
         Example: "red Kubota M7 tractor, plus silver Toyota 4Runner SUV"
      4. Falls back to single-vehicle top-level fields when vehicles[] is
         missing (legacy compat for responses from older code paths)

    Returns:
        A short string like "red Kubota M7 tractor, plus silver Toyota
        4Runner SUV" or "white Honda Civic sedan" for a single-vehicle
        result, or "" if no identification is present.
    """
    if not isinstance(vision_result, dict):
        return ""

    # Multi-vehicle schema (Phase.129 + 6B.130)
    vehicles = vision_result.get("vehicles") or []
    if isinstance(vehicles, list) and vehicles:
        primary_idx = vision_result.get("primary_vehicle_index", 0)
        # Clamp to list bounds (defensive — primary idx may be out of range)
        if not isinstance(primary_idx, int) or primary_idx < 0:
            primary_idx = 0
        if primary_idx >= len(vehicles):
            primary_idx = 0

        # Order: primary first, then the rest in their original order
        ordered: list = []
        primary = vehicles[primary_idx]
        if isinstance(primary, dict):
            ordered.append(primary)
        for i, v in enumerate(vehicles):
            if i == primary_idx:
                continue
            if isinstance(v, dict):
                ordered.append(v)

        # Format each, drop empties, join with ", plus "
        parts = [s for s in (_format_vehicle_summary(v) for v in ordered) if s]
        if parts:
            return ", plus ".join(parts)

    # Single-vehicle schema (top-level fields — legacy compat)
    return _format_vehicle_summary(vision_result)


def _emit_match_loop(ctx: AlertContext) -> None:
    """Phase.112: per-vehicle match loop — TG#3a / TG#3b in the gatekeeper stack.

    Fires AFTER TG#1 (arriving) and TG#2 (vehicle in motion + composite).
    Per Note 2026-08-21: "the matcher should run after the other two
    alerts are sent to me."

    For each vehicle in `vision_result["vehicles"]` (or wrapped single-
    vehicle if absent), extract a signature via `extract_signature`,
    run `match_with_details`, and send a match_alert (TG#3a) or
    no_match_alert (TG#3b) accordingly. Each TG#3 attachment is the
    vertical 3-crop composite from `_concat_crops_vertical`.

    The slim match_stage already ran in stage 3 and populated
    `ctx.match_verdict` for ONE vehicle (the top-identified one). This
    function ALSO handles multi-vehicle cases where vision returned
    >1 vehicles — it loops over each, ignoring ctx.match_verdict for
    vehicles that aren't already scored.

    Failure-isolated: per-vehicle failure logs and continues; never
    raises to the caller.
    """
    from infra.vehicle_matcher import match_with_details, score_top_n
    from telegram_formatter.match_telegram import (
        MatchTelegramInput,
        send_match_alert,
        send_no_match_alert,
    )
    from telegram_formatter.no_match_telegram import (
        NoMatchTelegramInput,
    )
    from vehicle_identifier.signature import extract_signature

    # Build vehicle list. Multi-vehicle schema first; fallback to wrap.
    vr = ctx.vision_result if isinstance(ctx.vision_result, dict) else {}
    vehicles = vr.get("vehicles") or []
    if not vehicles:
        # Wrap top-level fields as a single-vehicle list
        single = {k: v for k, v in vr.items() if k != "vehicles"}
        if single.get("make") or single.get("type") or single.get("color"):
            vehicles = [single]

    if not vehicles:
        log.info(
            f"[{ctx.alert_id}] match_loop: no vehicles in vision_result "
            f"— skipping TG#3"
        )
        return

    # Crop paths for the 3-crop composite photo
    crop_paths: list[str] = []
    if ctx.motion_result is not None and ctx.motion_result.crop_paths:
        crop_paths = ctx.motion_result.crop_paths[:3]

    # Thresholds: use ctx.score_top_n's first candidate score as a hint,
    # otherwise default. The matcher's actual threshold is read inside
    # match_with_details — these are for the body only.
    confidence_threshold = 0.6
    gap_threshold = 0.15

    sent_count = 0
    for v_idx, veh in enumerate(vehicles):
        if not isinstance(veh, dict):
            continue
        # Wrap so extract_signature picks vehicles[v_idx] as primary
        wrap = {
            "vehicles": [veh],
            "primary_vehicle_index": 0,
            "frame_positions": vr.get("frame_positions", []),
        }
        sig = extract_signature(wrap)
        if not sig:
            log.info(
                f"[{ctx.alert_id}] match_loop: vehicle[{v_idx}] "
                f"no signature — skipping"
            )
            continue

        try:
            match_detail = match_with_details(sig, ctx.known_vehicles)
        except Exception as e:
            log.warning(
                f"[{ctx.alert_id}] match_loop: vehicle[{v_idx}] "
                f"matcher raised {e!r} — skipping"
            )
            continue

        if match_detail is None:
            # No match — compute top-3 for the no-match body
            top_n = score_top_n(
                sig=sig, known=ctx.known_vehicles, n=3,
            )
            top_n_breakdowns = [
                (kv.get("id", "?"), score, breakdowns)
                for kv, score, breakdowns in top_n
            ]
            no_match = NoMatch(
                reason="below_threshold",
                top_candidates=[
                    (kv.get("id", "?"), score) for kv, score, _ in top_n
                ],
            )
            no_match_telegram_input = NoMatchTelegramInput(
                camera_name=ctx.camera_name,
                captured_at_iso=ctx.timestamp,
                no_match=no_match,
                top_n_breakdowns=top_n_breakdowns,
                match_threshold=confidence_threshold,
                gap_threshold=gap_threshold,
                alert_id=f"{ctx.alert_id}-v{v_idx}",
            )
            # Phase.122 (2026-08-22, Note): stash the
            # NoMatchTelegramInput so emit_result_stage can write its
            # body to alert.jsonl (the LLM-fallback title "Normal
            # Daytime Scene - No Activity Detected" was a lie).
            # b079e97a (red Jeep, 11:23:24 EDT) was the canary.
            ctx.match_alerts.append(no_match_telegram_input)
            sent = send_no_match_alert(
                alert_id=f"{ctx.alert_id}-v{v_idx}",
                camera_name=ctx.camera_name,
                no_match_telegram_input=no_match_telegram_input,
                crop_paths=crop_paths,
                bot_token=ctx.bot_token,
                chat_id=ctx.chat_id,
                captured_at=ctx.timestamp,
            )
            sent_count += int(sent)
        else:
            # match_detail is MatchDetail (kv, score, gap, reasons,
            # matched_dim_weights). Build a MatchVerdict for the body.
            verdict = MatchVerdict(
                known_vehicle=match_detail.kv,
                score=match_detail.score,
                gap=match_detail.gap,
                breakdowns=getattr(match_detail, "matched_dim_weights", {}) or {},
                rank=0,
                all_scores=[
                    (kv.get("id", "?"), score)
                    for kv, score, _ in score_top_n(
                        sig=sig, known=ctx.known_vehicles, n=3,
                    )
                ],
            )
            match_input = MatchTelegramInput(
                camera_name=ctx.camera_name,
                captured_at_iso=ctx.timestamp,
                verdict=verdict,
                match_threshold=confidence_threshold,
                gap_threshold=gap_threshold,
                alert_id=f"{ctx.alert_id}-v{v_idx}",
            )
            # Phase.121 (2026-08-22): stash the MatchTelegramInput so
            # emit_result_stage can use its body as the alert.jsonl record
            # instead of the LLM-fallback's generic L0 title.
            ctx.match_alerts.append(match_input)
            sent = send_match_alert(
                alert_id=f"{ctx.alert_id}-v{v_idx}",
                camera_name=ctx.camera_name,
                match_telegram_input=match_input,
                crop_paths=crop_paths,
                bot_token=ctx.bot_token,
                chat_id=ctx.chat_id,
                captured_at=ctx.timestamp,
            )
            sent_count += int(sent)

    log.info(
        f"[{ctx.alert_id}] match_loop: {sent_count}/{len(vehicles)} TG#3 "
        f"sent for {len(vehicles)} vehicles"
    )
