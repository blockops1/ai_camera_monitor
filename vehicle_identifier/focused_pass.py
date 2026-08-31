"""
focused_pass.py — Unknown-vehicle focused identification cascade.

STATUS: legacy — DEAD CODE in the slim listener.py post-6B.105c.
    The slim pipeline does not call any function in this module. The
    functions are preserved here (not deleted) because:
      (a) Note 2026-08-20 — "I'm gonna have somebody else work on
          doing the refactor after they download it from the upstream."
          This refers to the §11.31 plan for modular vehicle
          identification, of which the focused-pass cascade is a key piece.
      (b) The cascade is well-tested in the legacy path (verified by the
          archive file at listener/_focused_pass_archive_6B110.py and by
          the cascade_* audit log lines documented in §11.6).
      (c) Future activation: when the §11.31 modular vehicle matcher is
          wired into the pipeline, this module becomes a Stage B between
          `identify_stage` and `match_stage` — the focused pass runs after
          the broad vision pass and before the matcher, refining make/model
          on cropped regions for unknown_arrival events.

THREAD SAFETY: not thread-safe — functions mutate shared dicts in place
    (vehicle_events[i]["make"] = ...). Caller must serialize calls or
    pass a deep-copied events list.

INPUTS:
    - run_focused_pass: vehicle_events list, frame_paths list, vision_result
      dict, alert_id str, camera_name str (optional)
    - send_vehicle_notification: bot_token str, chat_id str, frame_path
      str|None, msg str
    - convert_unknown_to_known: vehicle_events list, alert_id str,
      crops_by_v_id dict|None, bot_token str, chat_id str, camera_name str

OUTPUTS:
    - run_focused_pass → dict mapping v_id → crop_path (parallel to
      vehicle_events), used by convert_unknown_to_known to attach the
      focused-pass crop as the identification-update Telegram photo
    - send_vehicle_notification → bool (True if Telegram accepted)
    - convert_unknown_to_known → None (mutates vehicle_events in place)

PUBLIC API:
    run_focused_pass(vehicle_events, frame_paths, vision_result, alert_id,
                     camera_name="") -> dict[str, str]
        Phase.6 Stage B. For each unknown_arrival event with a bbox,
        crop the best frame and run classify_vehicle_crop to get refined
        make/model/distinctive_features. Merges refined fields into the
        event dict so the escalation alert can include them. Returns
        v_id → crop_path mapping for the identification-update message.

    send_vehicle_notification(bot_token, chat_id, frame_path, msg) -> bool
        Send a vehicle notification: photo + text as a single Telegram
        message. Combined transport (Phase.10) — one sendPhoto with
        `caption=msg`. Failures logged but never raised.

    convert_unknown_to_known(vehicle_events, alert_id, crops_by_v_id=None,
                              bot_token="", chat_id="", camera_name="") -> None
        Phase.8 — promote unknown_arrival events to arrival when the
        focused pass made the signature match a known vehicle. Mutates
        vehicle_events in place. Phase.10 — when reclassification
        succeeds, send the identification-update Telegram (named vehicle
        + focused-pass crop photo).

DOES NOT DO:
    - Run the broad vision pass (3-crop identification) → that's
      identify_stage in vehicle_event_pipeline.py
    - Match a refined signature against the full known-vehicles list →
      infra.vehicle_matcher.match_vehicle_scored does the matching;
      this module just builds the signature dict and calls it
    - Maintain a vehicle state file → removed in 6B.57 (no longer needed)
    - Run the alert pipeline end-to-end → owned by vehicle_event_pipeline

WHY HERE:
    Phase.110 extraction (2026-08-21). These functions lived in
    listener.py (L1384-L1720, ~338 lines) because they were created
    before the module-purity review was applied to listener.py. They
    were dead code in the slim listener.py post-6B.105c — the production
    pipeline never called them. 6B.110 moves them here for two reasons:
      (a) The listener should be a thin composition root, not a 2000+
          line monolith that happens to also contain dead-but-historical
          code. Moving them shrinks listener.py by 338 lines.
      (b) §11.31's planned modular matcher work needs this logic in a
          focused, importable form. When the §11.31 plan activates, the
          listener will import `from vehicle_identifier.focused_pass
          import run_focused_pass, convert_unknown_to_known` and wire
          them into the pipeline. Today they're parked here, ready.

    Functions renamed from `_focused_pass_*` / `_vehicle_send_*` /
    `_convert_unknown_*` (underscore-prefixed because they were
    listener-private symbols) to public names (no underscore) because
    the module is now part of `vehicle_identifier/`'s public API.
    Same rename pattern as 6B.105c, 6B.106, 6B.108.

CALLED BY:
    - None in the slim production listener.py. Parked for §11.31.

    Was called by the LEGACY _process_alert (pre-6B.105c), now archived
    at listener/_process_alert_archive_6B105b.py:
      - run_focused_pass was called after identify_stage, before match_stage
      - convert_unknown_to_known was called after run_focused_pass + matcher

CALLS INTO:
    - vision_analyzer.classify_vehicle_crop: focused crop → refined dict
    - vehicle_cropper.crop_vehicle_from_frame: bbox + frame_path → crop_path
    - vehicle_artifacts: per-alert artifact index (metadata + frame/copy copies)
    - infra.send_telegram.send_photo_with_caption + send_message:
      identification-update Telegram transport
    - infra.vehicle_matcher.match_vehicle_scored: refined signature → known vehicle
    - known_vehicles.load_known_vehicles: get the known-vehicle list
    - _telegram_origin.origin_prefix: script-name tag for the identification message
    - infra.logging_setup.log (caller-provided via log module reference)
"""
from __future__ import annotations

import logging
import os
from typing import Any

# Module-specific logger — log lines tag as [focused_pass] not [listener].
log = logging.getLogger(__name__)


def run_focused_pass(
    vehicle_events: list[dict[str, Any]],
    frame_paths: list[str],
    vision_result: dict[str, Any],
    alert_id: str,
    camera_name: str = "",
) -> dict[str, str]:
    """Phase.6 Stage B — focused classify pass for unknown arrivals.

    Extracted from _process_alert so it can be unit-tested in isolation.
    For each unknown_arrival event with a bbox, crop the vehicle region
    from the best frame and run classify_vehicle_crop to get refined
    make/model/distinctive_features. Merges those into the event dict so
    the escalation alert can include them. Errors are logged but don't
    block the alert.

    Phase.10 — returns a dict mapping v_id → crop_path so the
    identification-update message (Fix A reclassification path) can
    attach the focused-pass crop as the photo. The crop is the best
    identification image (Qwen has just confirmed make/model from
    it) and is what the user should see when told "Brown F150 pickup
    identified."

    Audit logging (added 2026-07-22):
        cascade_IN      — entered with bbox=present|absent
        cascade_OUT     — refine succeeded with refined fields
        cascade_SKIPPED — event was not unknown_arrival OR bbox missing
        cascade_ERROR   — refine raised an exception

    Postmortem grep:
        grep cascade_IN logs/listener.log
        grep cascade_SKIPPED reason=no_bbox logs/listener.log
    """
    crops_by_v_id: dict[str, str] = {}
    try:
        import vehicle_artifacts as _va
        import vehicle_cropper
        from vision_analyzer import classify_vehicle_crop

        # Persist per-alert artifact index (metadata + frame copies) up
        # front so even if the cascade fails partway, the operator has
        # the frames to look at. The response files are written inside
        # analyze_frames / classify_vehicle_crop when they succeed.
        try:
            _va.write_metadata(
                alert_id,
                camera=camera_name,
                v_id="",
                bbox=None,
                best_frame_index=vision_result.get("best_frame_index"),
                frame_paths=frame_paths,
                crop_path=None,
            )
            _va.copy_frames(alert_id, frame_paths)
        except (OSError, ValueError, TypeError, AttributeError) as _va_err:
            # Defensive: vehicle_artifacts writes JSON metadata + copies
            # frame files. Anything going wrong here (bad JSON, missing
            # dir, weird type) must NOT block the cascade — the cascade
            # continues without the artifact index. The pipeline
            # tolerates the loss of operator-forensic artifacts.
            log.warning(f"[{alert_id}] vehicle_artifacts initial save failed: {_va_err}")

        for evt in vehicle_events:
            v_id = evt.get("v_id") or evt.get("vehicle_id") or ""
            event_kind = evt.get("event", "unknown")
            bbox = evt.get("bbox")
            log.info(
                f"cascade_IN: alert_id={alert_id} v_id={v_id} "
                f"event={event_kind} bbox={'present' if bbox else 'absent'}"
            )
            if event_kind != "unknown_arrival":
                log.info(
                    f"cascade_SKIPPED: alert_id={alert_id} v_id={v_id} "
                    f"reason=event_not_unknown_arrival event={event_kind}"
                )
                continue
            if not bbox:
                log.info(
                    f"cascade_SKIPPED: alert_id={alert_id} v_id={v_id} "
                    f"reason=no_bbox event={event_kind}"
                )
                continue
            try:
                # Pick the best frame. best_frame_index is 1-indexed;
                # fall back to the first frame if the index is missing
                # or out of range.
                best_idx = max(0, int(vision_result.get("best_frame_index", 1)) - 1)
                if best_idx >= len(frame_paths):
                    best_idx = 0
                frame_path = frame_paths[best_idx]

                crop_path = vehicle_cropper.crop_vehicle_from_frame(frame_path, bbox)
                if v_id:
                    crops_by_v_id[v_id] = crop_path
                refined = classify_vehicle_crop(crop_path, alert_id=alert_id)
                if refined.get("_error"):
                    log.warning(
                        f"[{alert_id}] focused classify error: {refined['_error']}"
                    )
                # Merge non-null refined fields into event.
                merged_keys: list[str] = []
                for key in ("make", "model", "distinctive_features"):
                    if refined.get(key):
                        evt[key] = refined[key]
                        merged_keys.append(key)
                if refined.get("confidence"):
                    evt["refined_confidence"] = refined["confidence"]
                log.info(
                    f"cascade_OUT: alert_id={alert_id} v_id={v_id} "
                    f"make={refined.get('make')} model={refined.get('model')} "
                    f"distinctive_features={refined.get('distinctive_features')!r} "
                    f"confidence={refined.get('confidence')} "
                    f"merged={merged_keys}"
                )
                # Refresh artifact metadata now that we know v_id + bbox +
                # crop_path, and copy the focused crop into the artifacts dir
                # for the operator to inspect.
                try:
                    _va.write_metadata(
                        alert_id,
                        camera=camera_name,
                        v_id=v_id,
                        bbox=list(bbox) if bbox else None,
                        best_frame_index=vision_result.get("best_frame_index"),
                        frame_paths=frame_paths,
                        crop_path=crop_path,
                    )
                    _va.copy_crop(alert_id, crop_path)
                except (OSError, ValueError, TypeError, AttributeError) as _va_err:
                    log.warning(
                        f"[{alert_id}] vehicle_artifacts post-cascade save failed: {_va_err}"
                    )
            except Exception as refine_err:  # noqa: BLE001
                # Defensive: cv2 + vision API + crop math can raise various
                # exception types (cv2.error which doesn't derive from
                # OSError/TypeError/ValueError, httpx errors, KeyError on
                # malformed vision_result). The cascade must continue
                # for the remaining events even if one event's refine fails.
                log.warning(f"[{alert_id}] focused classify pass failed: {refine_err}")
                log.info(
                    f"cascade_ERROR: alert_id={alert_id} v_id={v_id} "
                    f"stage=classify err={refine_err!r}"
                )
    except Exception as cascade_err:  # noqa: BLE001
        # Defensive: catches ImportError (vehicle_cropper / vision_analyzer
        # not installed), plus any setup error in the lazy imports. The
        # escalation pipeline must continue even if the focused-pass
        # cascade can't initialize — the unknown_arrival event will fire
        # the L1 unknown escalation alert as if the focused pass had
        # never been attempted.
        log.warning(f"[{alert_id}] focused classify setup failed: {cascade_err}")
        log.info(f"cascade_ERROR: alert_id={alert_id} stage=import err={cascade_err!r}")
    return crops_by_v_id


def send_vehicle_notification(
    bot_token: str,
    chat_id: str,
    frame_path: str | None,
    msg: str,
    *,
    alert_id: str,
    channel: str,
    event: str,
    v_id: str = "",
) -> bool:
    """Send a vehicle notification: photo + text as a single Telegram
    message (D16 fix).

    Phase.10 — combined transport. Previously this called sendPhoto
    (no caption) and sendMessage (no photo) as two separate API calls;
    that produced out-of-order arrivals and silent loss of the photo.
    Now uses _send_photo_with_caption: one sendPhoto with `caption=msg`.

    Failures are logged but never raised — this is fire-and-forget.

    Returns True if the body reached Telegram (we don't gate on the
    photo leg — a missing frame should not lose the alert text).
    """
    try:
        if not bot_token or not chat_id:
            return False
        if frame_path and os.path.isfile(frame_path):
            from infra.send_telegram import send_photo_with_caption as _tg_send_photo_caption

            return bool(
                _tg_send_photo_caption(
                    bot_token, chat_id, frame_path, msg,
                    alert_id=alert_id,
                    channel=channel,
                    event=event,
                    v_id=v_id,
                )
            )
        # No frame — fall through to plain text send (preserves old behavior).
        from infra.send_telegram import send_message as _tg_send_message

        return bool(_tg_send_message(
            bot_token, chat_id, msg,
            alert_id=alert_id,
            channel=channel,
            event=event,
            v_id=v_id,
        ))
    except Exception as err:  # noqa: BLE001
        # Defensive: the Telegram send path raises various exception
        # types (httpx errors, network errors, JSON errors). The
        # identification-update Telegram is best-effort; if it fails,
        # the L1 unknown escalation alert still fires.
        log.warning(f"vehicle_send_notification failed: {err}")
        return False


def convert_unknown_to_known(
    vehicle_events: list[dict[str, Any]],
    alert_id: str,
    crops_by_v_id: dict[str, str] | None = None,
    bot_token: str = "",
    chat_id: str = "",
    camera_name: str = "",
) -> None:
    """Phase.8 — promote unknown_arrival events to arrival when the
    focused pass made the signature match a known vehicle.

    For each event currently kind=unknown_arrival:
      1. Build a signature from the event (color + type + make + model
         post-focused-merge).
      2. Run _match_known_vehicle against the current known list.
      3. If a known vehicle matches, swap the event in-place:
         - kind: unknown_arrival → arrival
         - vehicle_id: <known id>  (canonical name)
         - v_id: keep the V-NNN for audit (was the temp id)
         - label: known vehicle's label
         - log: cascade_RECLASSIFIED so postmortems can grep
      4. If still no match, leave the event as-is (the L1 unknown
         escalation will fire below as before).

    Phase.10 — when a reclassification succeeds, send a SECOND
    Telegram notification naming the vehicle with the focused-pass
    crop photo. This fills the ~22s gap between message 1 ("Vehicle
    entering, identifying...") and message 2 (the full arrival or
    level1 alert). The user said (2026-07-23): "as soon as the
    vehicle tracker sees that there's a vehicle coming onto the farm,
    I want an alert. ... give a refined another telegram notification,
    which includes the best picture and a notification of which
    vehicle has come on board."

    Failures are logged but never raise — this is a refinement layer
    on top of the existing unknown_arrival path. A failure here must
    not block the alert pipeline.

    Audit logging:
        cascade_RECLASSIFIED        — focused pass made it match a known
        cascade_RECLASS_NOMATCH     — focused pass ran but didn't match
        identification_UPDATE_SENT  — known-vehicle update sent to Telegram
        identification_UPDATE_SKIPPED_no_creds  — known vehicle, no bot
                                      creds, log only
    """
    try:
        from infra.vehicle_matcher import match_vehicle_scored
        from known_vehicles import load_known_vehicles

        known = load_known_vehicles()
    except Exception as e:  # noqa: BLE001
        # Defensive: catches ImportError + any error in the lazy import
        # block. Same rationale as run_focused_pass's outer catch — the
        # reclassification layer is best-effort and must not block the
        # alert pipeline.
        log.info(f"cascade_RECLASS_ERROR: alert_id={alert_id} stage=import err={e!r}")
        return

    for evt in vehicle_events:
        if evt.get("event") != "unknown_arrival":
            continue

        # Build a signature from the merged event. The focused pass
        # has already written refined make/model into the event dict.
        sig = {
            "color": evt.get("color") or "unknown",
            "type": evt.get("type") or "unknown",
        }
        if evt.get("make"):
            sig["make"] = evt["make"]
        if evt.get("model"):
            sig["model"] = evt["model"]

        scored = match_vehicle_scored(sig, known)
        if scored is None:
            log.info(
                f"cascade_RECLASS_NOMATCH: alert_id={alert_id} "
                f"v_id={evt.get('v_id')} sig_color={sig.get('color')!r} "
                f"sig_type={sig.get('type')!r} "
                f"make={sig.get('make')!r} model={sig.get('model')!r}"
            )
            continue
        kv, _top_score, _top_gap, _breakdowns = scored

        # Convert in place. We keep v_id as the original V-NNN for
        # audit (records that this alert fired on a fresh V-NNN mint
        # that the focused pass then identified).
        log.info(
            f"cascade_RECLASSIFIED: alert_id={alert_id} "
            f"v_id={evt.get('v_id')} → known={kv['id']} "
            f"label={kv.get('label')!r} sig_color={sig.get('color')!r} "
            f"sig_type={sig.get('type')!r} "
            f"make={sig.get('make')!r} model={sig.get('model')!r}"
        )
        evt["event"] = "arrival"
        evt["vehicle_id"] = kv["id"]
        evt["label"] = kv.get("label")
        if kv.get("color") and not evt.get("color"):
            evt["color"] = kv["color"]
        if kv.get("type") and not evt.get("type"):
            evt["type"] = kv["type"]

        # Phase.57 (2026-08-05) — state machine removed. The legacy
        # V-NNN state cleanup that lived here (_read_state +
        # transition_vehicle_state + _write_state) is gone with it; there
        # is no on/off-property state to clean up. V-NNN ids continue to
        # appear in audit jsonl for forensics, but they are NOT persisted
        # to a state file.

        # Drop bbox/make/model — they were for the unknown alert only.
        # The arrival event uses the known label and the original
        # color+type from the signature (already in the event).

        # Phase.10 — send the identification update message.
        # Naming the vehicle is the whole point: the user gets a heads-up
        # within ~5s of detection ("Brown F150 pickup identified") rather
        # than waiting ~22s for message 2. The focused-pass crop is the
        # best frame for identification — Qwen has already confirmed
        # make/model from it. Fall back to evt[best_frame_path] if no
        # crop_path was passed.
        label = kv.get("label") or kv["id"]
        cam = camera_name or evt.get("camera", "")
        # 2026-07-26: prepend [filename] [VEHICLE_IDENTIFIED] tag so the
        # identification-update Telegram message shows the script that
        # produced it. Previously this message had no prefix at all (the
        # only vehicle-message path missing the script tag — confirmed
        # by user screenshot of "[name one]'s sedan identified
        # at CAM1" with no header).
        from _telegram_origin import origin_prefix
        ident_tag = origin_prefix("VEHICLE_IDENTIFIED")
        msg = (
            f"{ident_tag}\n"
            f"🚗 <b>{label}</b> identified at {cam}\n"
            f"   {evt.get('ts', '')}"
        )
        # Prefer the focused-pass crop for this v_id (best identification
        # image); fall back to the event's best_frame_path.
        evt_v_id = evt.get("v_id") or ""
        photo = None
        if crops_by_v_id:
            photo = crops_by_v_id.get(evt_v_id)
        if not photo:
            photo = evt.get("best_frame_path")

        if not bot_token or not chat_id:
            log.info(
                f"identification_UPDATE_SKIPPED_no_creds: "
                f"alert_id={alert_id} v_id={evt.get('v_id')} → "
                f"known={kv['id']} label={label!r}"
            )
        else:
            sent = send_vehicle_notification(
                bot_token=bot_token,
                chat_id=chat_id,
                frame_path=photo,
                msg=msg,
                alert_id=alert_id,
                channel="vehicle_id_focused_pass",
                event="identification_update",
                v_id=evt.get("v_id", ""),
            )
            log.info(
                f"identification_UPDATE_SENT: alert_id={alert_id} "
                f"v_id={evt.get('v_id')} → known={kv['id']} "
                f"label={label!r} sent={sent} photo={photo}"
            )


