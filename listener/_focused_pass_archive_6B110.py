"""
_focused_pass_archive_6B110.py — verbatim snapshot of 3 cascade functions
and their dependencies, extracted from listener.py on 2026-08-21
(Phase.110) before they were moved to vehicle_identifier/focused_pass.py:

  - _focused_pass_for_unknown_arrival       → focused_pass.run_focused_pass
  - _vehicle_send_notification              → focused_pass.send_vehicle_notification
  - _convert_unknown_to_known_after_focused_pass → focused_pass.convert_unknown_to_known

The "unknown vehicle focused pass" cascade is the Phase.6/6B.8/6B.10
logic that:
  1. Crops the best frame for each unknown_arrival event
  2. Calls vision_analyzer.classify_vehicle_crop on each crop
  3. Merges refined make/model/distinctive_features back into the event
  4. Re-runs the matcher with the refined signature
  5. If matched, promotes the event from unknown_arrival → arrival
     and sends an "identified" Telegram with the focused-pass crop as photo
  6. If still unmatched, leaves the event as-is for the L1 unknown escalation

DEAD CODE IN SLIM listener.py (post-6B.105c): These functions are NOT
called by the slim pipeline. The only references in the production tree
are inside this archive (as a comment pointer) and inside the functions
themselves (mutual references). The slim pipeline's emit_result_stage
does NOT call any of these.

Why move them instead of delete?
  Note (2026-08-20): "I'm gonna have somebody else work on doing
  the refactor after they download it from the upstream." This refers
  to the §11.31 plan for modular vehicle identification, of which the
  focused-pass cascade is a key piece. We don't want to lose 338 lines
  of well-tested (in the legacy path) logic right before someone picks
  up §11.31 work.

  Per archive-first-workflow: archive first, then move. The archive is
  the rollback / archaeology source of truth.

=== ORIGINAL listener.py lines 1384-1720 (Phase.110 archive) ===
def _focused_pass_for_unknown_arrival(
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
        except Exception as _va_err:
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
                except Exception as _va_err:
                    log.warning(
                        f"[{alert_id}] vehicle_artifacts post-cascade save failed: {_va_err}"
                    )
            except Exception as refine_err:
                log.warning(f"[{alert_id}] focused classify pass failed: {refine_err}")
                log.info(
                    f"cascade_ERROR: alert_id={alert_id} v_id={v_id} "
                    f"stage=classify err={refine_err!r}"
                )
    except Exception as cascade_err:
        # vehicle_cropper / classify_vehicle_crop import failures
        # must not block the escalation pipeline.
        log.warning(f"[{alert_id}] focused classify setup failed: {cascade_err}")
        log.info(f"cascade_ERROR: alert_id={alert_id} stage=import err={cascade_err!r}")
    return crops_by_v_id


def _vehicle_send_notification(
    bot_token: str,
    chat_id: str,
    frame_path: str | None,
    msg: str,
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
                _tg_send_photo_caption(bot_token, chat_id, frame_path, msg)
            )
        # No frame — fall through to plain text send (preserves old behavior).
        from infra.send_telegram import send_message as _tg_send_message

        return bool(_tg_send_message(bot_token, chat_id, msg))
    except Exception as err:
        log.warning(f"vehicle_send_notification failed: {err}")
        return False


def _convert_unknown_to_known_after_focused_pass(
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
    except Exception as e:
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
            sent = _vehicle_send_notification(
                bot_token=bot_token,
                chat_id=chat_id,
                frame_path=photo,
                msg=msg,
            )
            log.info(
                f"identification_UPDATE_SENT: alert_id={alert_id} "
                f"v_id={evt.get('v_id')} → known={kv['id']} "
                f"label={label!r} sent={sent} photo={photo}"
            )


