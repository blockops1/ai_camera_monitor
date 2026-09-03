"""
_process_alert_archive_6B105b.py — Phase.105b (2026-08-20)

Archive of the original `_process_alert` function from listener.py BEFORE
the F2 slim. Preserved here for rollback reference. NOT IMPORTED — this
file is dead code that lives only to keep the original implementation
recoverable without going back through git history.

To restore the original behavior, copy this block back into listener.py's
_process_alert. See PLAN.md §11.35 for the F2 design rationale.

This file is NOT loaded into the runtime. It exists solely as a
preserved snapshot per archive-first-workflow.
"""
# Original lines 2326..4080 of listener/listener.py (1755 lines).
def _process_alert(
    alert_id: str, camera_name: str, timestamp: str, event: str, rtsp_url: str
) -> None:
    """
    Run the full pipeline: capture → analyze → generate → notify.

    Args:
        alert_id: UUID for this alert run.
        camera_name: Human-readable label.
        timestamp: ISO 8601 event timestamp.
        event: Event type ("motion", "person", "vehicle", etc.).
        rtsp_url: Full RTSP URL for this camera.
    """
    log.info(f"[{alert_id}] Starting pipeline for {camera_name} (event={event})")

    # Phase.67 (2026-08-08) — pre-bound to None so the legacy
    # single-crop / 6-frame match-alert path can read id_result safely.
    # The 3-crop path (Phase.65+) reassigns this with the result of
    # identify_from_crops(); the legacy path never touches it. Pyright
    # needs an unconditional bind for static analysis.
    id_result = None

    # Phase 1.2 (2026-08-05) — motion+match cooldown lives at module
    # level (`_MOTION_COOLDOWN`) so /status can read it before any
    # alert fires and tests can import it without invoking
    # _process_alert. All worker threads share this singleton via the
    # same thread-safe `_MotionCooldown` class.

    # Load Telegram creds up front — Phase.9 message 1 ("arriving")
    # fires before vision has run, so we can't wait for the post-vision
    # load block. The same creds are reused for vehicle events and the
    # main pipeline further down.
    _bot_token, _chat_id = _load_telegram_creds()

    # 1. Capture frames
    # Phase.9: capture window extended from 3@2s (~4s) to 6@4s (~22s).
    # Vehicles approaching from ~80m are tiny dots in the first frame; 6
    # frames over 22s lets the vehicle traverse most of the approach so
    # the closer/later frames have enough surface area for make/model
    # identification. For persons this also helps — closer to the
    # camera by frame 6 means better InsightFace pixels.
    #
    # Vehicle-only: split capture into two phases so we can fire message 1
    # ("Vehicle entering, identifying...") as soon as frame 1 lands,
    # without waiting for the full 22s capture + identify cycle.
    output_dir = os.path.join(ALERT_FRAME_DIR, alert_id)
    is_vehicle_event = event == "vehicle"

    if is_vehicle_event:
        # Single RTSP session for all 6 frames. Note 2026-08-02: the
        # previous phase-1/phase-2 split (separate capture_frames() calls
        # with a Telegram send between them) added ~4s of latency from
        # RTSP reconnect + outbound HTTP. Result: the Tesla was already
        # gone from FOV by frame 002 of the burst. Collapse to one
        # capture_frames(count=6, interval=2) call so all 6 frames share
        # the same RTSP connection and start within ~0.5s of webhook fire.
        # Message 1 ("arriving") now fires AFTER all 6 frames are
        # captured — trades ~10s of "arriving" latency for the burst
        # landing intact. Acceptable; will revisit if it becomes a
        # problem.
        #
        # 2026-08-06 — gatekeeper (OFS) override: instead of the trailing
        # tail, sample 6 frames spanning T-12s through T+0s of camera time
        # at 15fps (indices [0, 30, 60, 90, 120, 150]). Note wants to
        # see the pre-event motion trail for vehicle arrivals, not just
        # what was in FOV at webhook fire. This requires the 180-frame
        # ring buffer (was 30, see src/persistent_rtsp.py).
        frame_paths = capture_frames(
            rtsp_url=rtsp_url,
            output_dir=output_dir,
            count=6,
            interval=2,
            max_size=(3840, 2160),
            timeout=30,
            frame_offsets=(
                # Phase.58 baseline (2026-08-06): trail starts at head of
                # deque (T-12s) and runs to T+0s. Verified live in this morning's
                # Tesla event (6 distinct frames, 2s spacing).
                # 2026-08-07 Phase.60 attempted to shift back 4s, but the
                # geometry didn't fit — reverted.
                [0, 30, 60, 90, 120, 150]
                if camera_name in GATEKEEPER_CAMERAS
                else None
            ),
        )
        if not frame_paths:
            log.error(f"[{alert_id}] No frames captured — aborting")
            STATE["by_threat_level"][-1] += 1
            return

        # Fire message 1 ("arriving") with frame_001. Done after the
        # capture completes so we don't break the single-RTSP-session
        # invariant above.
        _send_arriving_message(
            alert_id=alert_id,
            camera_name=camera_name,
            frame_path=frame_paths[0],
            bot_token=_bot_token,
            chat_id=_chat_id,
        )
    else:
        # Non-vehicle: single capture call.
        # 2026-08-02: aligned to 2s interval to match vehicle capture cadence
        # for consistency (so all 6-frame captures land on the same
        # T+0, +2, +4, +6, +8, +10 cadence).
        frame_paths = capture_frames(
            rtsp_url=rtsp_url,
            output_dir=output_dir,
            count=6,
            interval=2,
            max_size=(3840, 2160),
            timeout=30,
        )

    if not frame_paths:
        log.error(f"[{alert_id}] No frames captured — aborting")
        STATE["by_threat_level"][-1] += 1
        return

    log.info(f"[{alert_id}] Captured {len(frame_paths)} frames")

    # Default best_frame_path = first captured frame. Once vision analysis
    # runs, line ~589 may overwrite this with the vision-selected frame
    # (frame_001 typically). The variable is hoisted early so the vehicle
    # dispatch block (Fix 3 escalation) can attach a photo to unknown-
    # vehicle alerts before the main alert pipeline runs.
    best_frame_path = frame_paths[0]

    # Phase.64 — frame-differencing motion detector. Runs BEFORE vision.
    # Computes trajectory deterministically from pixel centers and produces
    # a cropped image of the moving object for vision classification.
    # Replaces Qwen3-VL's hallucinated trajectory field. ~115ms cost.
    motion_result = None
    if is_vehicle_event and len(frame_paths) >= 2:
        motion_result = detect_motion_opencv(
            frame_paths=frame_paths,
            output_dir=output_dir,
            alert_id=alert_id,
        )
        if motion_result.no_motion_detected:
            log.info(
                f"[{alert_id}] motion_detector: no motion detected "
                f"(falling back to 6-frame static prompt)"
            )
        else:
            primary = motion_result.primary_moving_object
            log.info(
                f"[{alert_id}] motion_detector: found primary moving object "
                f"(avg_area={primary.avg_area if primary else 0}, "
                f"trajectory={primary.trajectory if primary else []})"
            )

    # Acquire this camera's LLM slot for the entire analysis block.
    # Reason: the local vision LLM (Qwen3-VL-8B at :8080) drops concurrent
    # requests to HTTP 500. Serializing per-camera lets different cameras
    # analyze in parallel while keeping each camera's calls sequential.
    with acquire_for_camera(camera_name):
        # 2. Vision analysis -- adaptive frame selection
        #
        # Phase.13 (2026-07-26):
        #   - For event="vehicle", ALWAYS send all captured frames in a single
        #     call using VEHICLE_MOTION_PROMPT_TEMPLATE. We skip the
        #     single-frame first-pass because a confident-but-wrong answer
        #     on frame 1 (e.g. "blue sedan parked" when actually the F-150
        #     is mid-frame) used to bury the real moving vehicle and would
        #     suppress the arrival event. Qwen3-VL uses temporal reasoning
        #     across the burst, comparing positions across frames, to
        #     identify the moving vehicle and whether one is actually
        #     moving. Capacity verified: 6x720p + ~500-token prompt fits
        #     in 8,192-ctx llama-server ~3,200 tokens; ~14s per call.
        #   - For non-vehicle events (person/motion/animal), keep the
        #     legacy single-frame-first path: fast (~2-3s) and the temporal
        #     reasoning isn't needed for those classifications.
        if is_vehicle_event and len(frame_paths) > 1:
            # Phase.64 — vision prompt and frame selection driven by
            # motion detector. If motion detector found a primary moving
            # object, classify JUST that crop (single image, much faster
            # and more accurate than 6-frame multi-image). The trajectory
            # in the alert body comes from the detector, NOT from vision.
            # Fallback: motion detector found nothing → use 6-frame static
            # prompt to still classify what's in the scene.
            if motion_result is not None and not motion_result.no_motion_detected:
                crop_paths = motion_result.crop_paths  # up to 3 crops (Phase.65)
                if crop_paths:
                    # Phase.65 — the identifier routine is the SINGLE
                    # entry point: it calls vision on each crop, picks the
                    # most informative signature, and hands off to the
                    # existing match_vehicle_scored. No duplicate vision
                    # calls. Trajectory injection happens here too because
                    # the routine returns the matched vision_result.
                    #
                    # Phase.78b (2026-08-14) — call-site fix. The
                    # listener was passing stale kwargs (alert_id,
                    # vision_api_url, known_vehicles) that the
                    # identify_from_crops signature does not accept, plus
                    # missing captured_at. Every vehicle event raised
                    # TypeError and fell through to single-crop vision.
                    # known_vehicles is loaded separately below for the
                    # matcher (it is the matcher's input, not the
                    # identifier's).
                    try:
                        from vehicle_identifier import (
                            VisionError,
                            VisionResult,
                            identify_from_crops,
                        )

                        id_result = identify_from_crops(
                            crop_paths=crop_paths,
                            camera_name=camera_name,
                            captured_at=timestamp,
                            api_url=VISION_API_URL,
                            output_dir=output_dir,
                            alert_id=alert_id,
                        )
                        # The routine returns an IdentifierResult. The
                        # vision classification lives on .vision_result
                        # (NOT id_result["vision_classification"] —
                        # that key is a telemetry-skip-set entry, not a
                        # field on the dataclass).
                        _vr = id_result.vision_result
                        # Phase.87 (PLAN §11.17): identify_from_crops
                        # returns vision_result as a VisionResult object
                        # (NOT a dict) on success, a VisionError object
                        # on failure, and None when there were no crops.
                        # The pre-6B.87 `isinstance(_vr, dict)` check
                        # swallowed both VisionResult and VisionError into
                        # {}, which silently stripped Qwen's full
                        # identification (make/model/color/confidence)
                        # from the gatekeeper motion alert body — the
                        # body rendered `qwen confidence: (empty)` and
                        # `1. vehicle` even when the crops succeeded at
                        # 0.85-0.95 confidence. Unwrap each case.
                        if isinstance(_vr, VisionResult):
                            vision_result = _vr.to_dict()
                        elif isinstance(_vr, dict):
                            vision_result = _vr
                        else:
                            # VisionError or None: the failure sentinel
                            # shape is what callers expect downstream.
                            vision_result = (
                                _vr.to_dict()
                                if isinstance(_vr, VisionError)
                                else {}
                            )
                        # Phase.94 (2026-08-18) — crop-prompt →
                        # alert-prompt schema lift. The crop prompt
                        # (vehicle_identifier/prompt_template.py) returns
                        # color/make/model/description/confidence;
                        # infra/alert_prompt._build_payload expects
                        # primary_subject/objects_detected/actions/
                        # scene_description. Without translation the
                        # alert LLM receives an effectively-empty dict
                        # and writes "empty exterior scene" L0 even when
                        # vision correctly identified a vehicle
                        # (verified on alert 0eefa8e9 Tesla drive-by
                        # 15:08 EDT OFG: Tesla Model Y blue conf=0.98
                        # but L0 "empty exterior scene" was produced).
                        # Delegate to the dedicated module — listener
                        # orchestrates, this plumbing does not.
                        from infra.vision_schema_lift import (
                            lift_crop_to_alert_schema,
                        )
                        vision_result = lift_crop_to_alert_schema(vision_result)
                        # Inject detector trajectory into vision result.
                        primary = motion_result.primary_moving_object
                        trajectory = primary.trajectory if primary else []
                        if isinstance(vision_result, dict):
                            vehicles = vision_result.get("vehicles") or []
                            if vehicles:
                                vehicles[0]["frame_positions"] = trajectory
                            else:
                                # Phase.76 (2026-08-11) — when the
                                # crop prompt returns identification
                                # fields at the TOP level of
                                # vision_result (the 6B.66 schema path),
                                # vision_result["vehicles"] is empty. We
                                # previously synthesized a bare
                                # [{frame_positions}] placeholder which
                                # STRIPPED the color / body_style_hint /
                                # make / model / vehicle_features that
                                # the crop prompt DID return. The
                                # downstream match path
                                # (extract_signature + match_vehicle)
                                # then saw color='unknown' type='unknown'
                                # and returned NO_MATCH, suppressing the
                                # 2nd identification Telegram.
                                #
                                # Fix: lift the top-level identification
                                # fields into the synthesized vehicles[0]
                                # so extract_signature's first branch
                                # (the vehicles[]-present path) picks
                                # them up cleanly.
                                top_vf = vision_result.get("vehicle_features") or {}
                                synthesized = {
                                    "frame_positions": trajectory,
                                }
                                for _fld in (
                                    "color", "body_style_hint",
                                    "make", "model",
                                ):
                                    _val = vision_result.get(_fld)
                                    if _val is not None and _val != "":
                                        synthesized[_fld] = _val
                                # Lift vehicle_features as a NESTED dict so
                                # extract_signature()'s `vf = v.get("vehicle_features")`
                                # reads them correctly. The matcher reads
                                # the flat sig[...] fields that
                                # extract_signature produces from this
                                # nested dict.
                                _nested_vf: dict = {}
                                for _fld in (
                                    "wheel_style", "wheel_arch", "wheel_color",
                                    "roofline_style", "front_grille_style",
                                    "headlight_signature", "rear_lights_signature",
                                    "tailgate_type",
                                    "window_tint", "cab_marker_lights", "bed_cover",
                                ):
                                    _val = top_vf.get(_fld)
                                    if _val is not None and _val != "":
                                        _nested_vf[_fld] = _val
                                _badge = top_vf.get("badge_text_readable")
                                if _badge is not None and _badge != "":
                                    _nested_vf["badge_text_readable"] = _badge
                                if _nested_vf:
                                    synthesized["vehicle_features"] = _nested_vf
                                vision_result["vehicles"] = [synthesized]
                            # Phase.77 (2026-08-11) — DO NOT inject
                            # the matcher's label into vision_result.
                            # The Motion Telegram body renders Qwen's
                            # identification verbatim from the vehicle
                            # dict Qwen returned; the matcher's label
                            # belongs in the 2nd Telegram only.
                            # Previously (6B.76) we set
                            # vision_result["identified_label"] =
                            # id_result["label"] which contaminated
                            # the Motion Telegram body with the
                            # matcher's verdict (e.g. name two's white
                            # pickup was wrongly labeled "Jayco Jay
                            # Feather travel trailer" because the
                            # matcher scored it against v_jayco_camper
                            # at 3.20).
                            # Phase.78b (2026-08-14) — IdentifierResult
                            # is a dataclass with .crops_used,
                            # .fallback_used, .elapsed_ms, .best_crop_path,
                            # .vision_result, .signature. There is no
                            # .get(), no .identified, no .kv_id, no
                            # .label, no .confidence on it. The previous
                            # block read dict-style fields that don't
                            # exist (and never did after the 6B.65
                            # refactor). The matcher's IDENTIFIED verdict
                            # is logged separately below at the
                            # _send_match_alert call site.
                            log.info(
                                f"[{alert_id}] vehicle_identifier: ran "
                                f"crops_used={id_result.crops_used}/{len(crop_paths)} "
                                f"fallback_used={id_result.fallback_used!r} "
                                f"elapsed_ms={id_result.elapsed_ms:.0f} "
                                f"best_crop_path={id_result.best_crop_path!r}"
                            )
                        _active_prompt = "VEHICLE_STATIC_PROMPT (via vehicle_identifier)"
                    except Exception as e:
                        log.warning(
                            f"[{alert_id}] vehicle_identifier: routine raised {e!r}, "
                            f"falling back to single-crop vision"
                        )
                        _active_prompt = "VEHICLE_STATIC_PROMPT (single-crop fallback)"
                        vision_result = analyze_frames_queued(
                            frame_paths=[crop_paths[0]],
                            camera_name=camera_name,
                            api_url=VISION_API_URL,
                            alert_id=alert_id,
                            event_hint="vehicle",
                            captured_at=timestamp,
                        )
                        primary = motion_result.primary_moving_object
                        trajectory = primary.trajectory if primary else []
                        if isinstance(vision_result, dict):
                            vehicles = vision_result.get("vehicles") or []
                            if vehicles:
                                vehicles[0]["frame_positions"] = trajectory
                else:
                    # Detector found motion but no usable crop — fall back.
                    log.warning(
                        f"[{alert_id}] motion detector found motion but no crop; "
                        f"falling back to 6-frame static prompt"
                    )
                    _active_prompt = "VEHICLE_STATIC_PROMPT (6-frame fallback)"
                    vision_result = analyze_frames_queued(
                        frame_paths=frame_paths,
                        camera_name=camera_name,
                        api_url=VISION_API_URL,
                        alert_id=alert_id,
                        event_hint="vehicle",
                        captured_at=timestamp,
                    )
            else:
                # Motion detector found no motion OR didn't run — fall back
                # to 6-frame static prompt so we still get a scene
                # classification for re-alerts or parked-vehicle cases.
                _active_prompt = "VEHICLE_STATIC_PROMPT (6-frame, no-motion-detected)"
                log.info(
                    f"[{alert_id}] vehicle_event -> 6-frame static analysis "
                    f"(no motion detected by frame-differencing)"
                )
                vision_result = analyze_frames_queued(
                    frame_paths=frame_paths,
                    camera_name=camera_name,
                    api_url=VISION_API_URL,
                    alert_id=alert_id,
                    event_hint="vehicle",
                    captured_at=timestamp,
                )
        else:
            # Non-vehicle events (person/motion/animal): single-frame first
            # pass, escalate on low confidence or multi-subject.
            first_frame = [frame_paths[0]]
            vision_result = analyze_frames_queued(
                frame_paths=first_frame,
                camera_name=camera_name,
                api_url=VISION_API_URL,
                alert_id=alert_id,
                event_hint=event,
            )

            if "error" in vision_result.get("objects_detected", []):
                log.error(
                    f"[{alert_id}] Vision analysis failed on first frame: "
                    f"{vision_result.get('scene_description')}"
                )
                STATE["by_threat_level"][-1] += 1
                return

            selected_frames = select_frames(frame_paths, vision_result)
            if len(selected_frames) > 1:
                log.info(
                    f"[{alert_id}] Escalating to {len(selected_frames)} frames "
                    f"(first-pass: {vision_result.get('primary_subject')!r}, "
                    f"conf={vision_result.get('confidence'):.2f}, "
                    f"objects={vision_result.get('objects_detected')})"
                )
                vision_result = analyze_frames_queued(
                    frame_paths=selected_frames,
                    camera_name=camera_name,
                    api_url=VISION_API_URL,
                    alert_id=alert_id,
                    event_hint=event,
                )
                if "error" in vision_result.get("objects_detected", []):
                    log.warning(
                        f"[{alert_id}] Multi-frame analysis failed; "
                        f"using first-frame result"
                    )
                    vision_result = analyze_frames_queued(
                        frame_paths=first_frame,
                        camera_name=camera_name,
                        api_url=VISION_API_URL,
                        alert_id=alert_id,
                        event_hint=event,
                    )

        log.info(
            f"[{alert_id}] Vision: {vision_result.get('primary_subject')} — {vision_result.get('scene_description')}"
        )

        # Cache the latest vision result for the heartbeat (top-of-hour re-evaluation).
        # Done before alert generation so even if generation fails the cache is fresh.
        set_last_vision(
            camera_name=camera_name,
            vision_result=vision_result,
            frame_path=frame_paths[0],
            timestamp=timestamp,
        )

        # Phase.0: vehicle state tracker. Failure-isolated.
        # Only Outside Front Solar (the gatekeeper camera) flips state;
        # other cameras pass through with no events.
        # (Prior gatekeeper was Building Front Corner; swapped to Outside
        # Front Solar during the 2026-07-24 RLC-510A swap. BFC is now
        # physically retired as of 2026-07-29.)
        #
        # State-tracker events route to Telegram via _vehicle_send, which
        # calls send_telegram.send_message (not just log). Until 2026-07-21 this
        # was a log-only stub and vehicle arrival/departure notifications
        # never actually reached Telegram — covered by
        # tests/test_vehicle_event_handler.py::TestHandleVehicleEvents.
        #
        # Telegram creds are loaded once at the top of _process_alert
        # (Phase.9 — needs them before phase-1 capture to fire
        # message 1) and reused here for vehicle events + the main
        # pipeline notify() call below.
        bot_token = _bot_token
        chat_id = _chat_id

        try:
            # Phase.57 (2026-08-05) — STATE MACHINE removed.
            # Note's directive: no state machine, no tracking of what's
            # on/off property, no V-NNN persistence, no arrival/departure
            # events, no BFC retirement logic.
            #
            # What replaced it: vehicle motion alert + 2nd-Telegram match
            # alert (below). Lead Telegram = "Vehicle motion at OFS" with
            # the picture. Match Telegram = "🎯 Matched vehicle at OFS"
            # when a known-vehicle match clears the spec thresholds.
            # Nothing persists between alerts; nothing has TTL.
            #
            # The matcher's home is vehicle_matcher.match_vehicle_scored —
            # not vehicle_state. Loading known vehicles is in known_vehicles.
            vehicle_events = None

            # Pick the best frame for the lead motion alert photo.
            # Preference order (added 2026-07-25 after morning incident where
            # frame_paths[0] sent a tiny far-away 4Runner; loop test confirmed
            # middle-of-burst = vehicle mid-camera, large in frame):
            #   1. face_visibility.best_frame_index (vision's pick for the
            #      most visible subject across the burst)
            #   2. vision_result["best_frame_index"] (legacy field)
            #   3. Middle of burst (frame_paths[len//2]) — vehicle is mid-
            #      camera, large in frame; far better than frame 0 which
            #      catches the vehicle at trigger time when it's smallest
            #      and farthest.
            #   4. First frame (last-resort fallback, preserves prior
            #      behavior when the burst has fewer than 3 frames).
            _best_frame_path = None
            if frame_paths:
                _best_idx = None
                # Phase.27 (2026-07-30): for vehicle events, ONLY consult
                # the top-level best_frame_index set by Qwen. Do NOT consult
                # face_visibility.best_frame_index — faces are irrelevant to
                # vehicle classification (Note: "why is a face visible
                # relevant at all to a vehicle classify?"). The default
                # face_visibility.best_frame_index=1 was hijacking the frame
                # choice and sending frame_001.jpg (tiny far-away vehicle)
                # even when a middle-of-burst frame would show it large
                # and clear.
                legacy = vision_result.get("best_frame_index")
                if (
                    isinstance(legacy, (int, float))
                    and 0 < int(legacy) <= len(frame_paths)
                ):
                    _best_idx = int(legacy) - 1
                if _best_idx is not None:
                    _best_frame_path = frame_paths[_best_idx]
                elif len(frame_paths) >= 3:
                    # Middle of burst: vehicle is closest to the camera,
                    # largest in frame. Verified against Solar gatekeeper
                    # burst data on 2026-07-25.
                    _best_frame_path = frame_paths[len(frame_paths) // 2]
                else:
                    _best_frame_path = frame_paths[0]

            # Phase.57 (2026-08-05) — DELETED: `_vehicle_send`
            # closure. Was the Telegram transport for state-tracker
            # arrival/departure events. Without tick() events, there is
            # no caller for it. The motion alert + match alert below
            # are the only Telegram paths now.

            # Phase.70 (2026-08-08) — import MatchDetail lazily so
            # the type annotation below resolves at type-check time
            # without forcing a top-of-function import that would pull
            # vehicle_state into every alert.
            from infra.vehicle_matcher import MatchDetail

            def _send_match_alert(
                alert_id: str,
                camera_name: str,
                vision_vehicle: dict,
                match_detail: MatchDetail,
                frame_path: str | None,
                crop_path: str | None,
                bot_token: str,
                chat_id: str,
                captured_at: str,
            ) -> None:
                """Phase.57 (2026-08-05) — match alert.

                Phase.67 (2026-08-08) — crop photo + Eastern timestamp:
                - photo: prefer the bbox crop that fed the matcher
                  (one of the 3 crops from identify_from_crops). Falls
                  back to frame_path (a wide captured frame) and finally
                  to text-only if neither file exists.
                - timestamp: convert the webhook's UTC ISO string to
                  America/New_York so the alert reads "15:31 EDT" instead
                  of "T15:31:19.000+0000".

                Sends a 2nd Telegram when the OFS motion alert's vision
                sig for a moving vehicle matches a known vehicle above
                the spec's confidence + gap thresholds. Independent of
                the state machine — the matcher runs purely on
                (vision_signature × known_vehicles.json), no persistence.

                Message format:
                    🎯 Matched vehicle at OFS
                       14:08 EDT
                       Match: name two's white pickup (90% confidence)
                       Reasoning: model substring matches; color matches;
                       body type matches

                Anti-spam: NOT called for non-moving vehicles and NOT
                called for vehicles that don't match above threshold.
                Callers are responsible for gating.
                """
                kv = match_detail.kv
                label = kv.get("label", kv.get("id", "unknown"))
                conf_pct = round(100 * match_detail.score)
                msg_lines = [
                    f"🎯 <b>Matched vehicle at {camera_name}</b>",
                ]
                # Phase.67 — render timestamp in Eastern so the user
                # doesn't have to mentally subtract 4 hours. Webhook
                # timestamp arrives like "2026-08-08T15:31:19.000+0000"
                # (ISO with +0000 suffix). ZoneInfo handles EST/EDT
                # transitions transparently.
                if captured_at:
                    try:
                        _dt = datetime.fromisoformat(captured_at)
                        _et = _dt.astimezone(ZoneInfo("America/New_York"))
                        msg_lines.append(f"   {_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                    except (ValueError, TypeError):
                        # Fall back to raw timestamp if parsing fails —
                        # better than dropping the field entirely.
                        msg_lines.append(f"   {captured_at}")
                msg_lines.append(f"   Match: {label} ({conf_pct}% confidence)")
                if match_detail.reasons:
                    msg_lines.append(
                        f"   Reasoning: {'; '.join(match_detail.reasons)}"
                    )
                # Add distinguishing features if the matched vehicle
                # has distinctive_features populated (helpful when the
                # reason list is brief — the user can see what makes
                # this known vehicle THIS known vehicle).
                distinct = kv.get("distinctive_features") or []
                if distinct:
                    msg_lines.append(f"   Distinctive: {distinct[0]}")
                msg = "\n".join(msg_lines)
                log.info(
                    f"[{alert_id}] OFS match alert: kv={kv.get('id')!r} "
                    f"label={label!r} score={match_detail.score:.2f} "
                    f"gap={match_detail.gap:.2f} reasons={match_detail.reasons}"
                )

                # Phase.67 — prefer the bbox crop over the wide frame.
                # Crops live in data/frames/{alert_id}/crops/ and are the
                # same image Qwen3-VL actually identified the vehicle from.
                photo_path = None
                if crop_path and os.path.isfile(crop_path):
                    photo_path = crop_path
                elif frame_path and os.path.isfile(frame_path):
                    photo_path = frame_path
                photo_ok = False
                if bot_token and chat_id:
                    if photo_path:
                        try:
                            from infra.send_telegram import (
                                send_photo_with_caption as _tg_send_photo,
                            )
                            photo_ok = _tg_send_photo(
                                bot_token, chat_id, photo_path, msg
                            )
                        except Exception as e:
                            log.warning(f"[{alert_id}] match alert photo send failed: {e}")
                    if not photo_ok:
                        try:
                            from infra.send_telegram import send_message as _tg_send_message
                            photo_ok = bool(_tg_send_message(bot_token, chat_id, msg))
                        except Exception as e:
                            log.warning(f"[{alert_id}] match alert text send failed: {e}")
                    log_outbound_telegram(
                        channel="gatekeeper_match",
                        alert_id=alert_id,
                        v_id=kv.get("id", ""),
                        event="vehicle_matched",
                        body=msg,
                        sent=bool(photo_ok),
                        extra=f"score={match_detail.score:.2f} gap={match_detail.gap:.2f} kv={kv.get('id')}",
                        image_paths=[photo_path] if photo_path else [],
                    )
                else:
                    log.warning(
                        f"[{alert_id}] OFS match alert NOT sent (no telegram creds)"
                    )

            def _send_no_match_alert(
                alert_id: str,
                camera_name: str,
                vision_vehicle: dict,
                top3: list,
                thresholds: dict,
                frame_path: str | None,
                crop_path: str | None,
                bot_token: str,
                chat_id: str,
                captured_at: str,
            ) -> None:
                """Phase.77 (2026-08-11) — no-match Telegram.

                Sends a 2nd Telegram when the matcher did NOT clear the
                spec thresholds for a moving vehicle. The user sees
                Qwen's identification (vehicle line above) PLUS the
                top-3 closest known_vehicles candidates with their
                per-dimension score breakdowns, so it's clear why no
                match cleared (e.g. "the closest candidate scored 0.7
                but the threshold is 0.6 — close, but no cigar").

                Anti-spam: only fires when (a) at least one moving
                vehicle was detected and (b) the matcher returned None
                for that vehicle. The call site in the matcher loop
                already gates on those conditions.

                Message format:
                    ❌ <b>No match for vehicle at OFS</b>
                       14:08 EDT
                       Vision: A white pickup truck with chrome horizontal grille
                       Top 3 candidates:
                         1. v_brown_f150 (Brown F150 pickup (camper top)): score=2.50
                            color_match=0.7 make_match=2.0 type_match=1.0
                         2. v_jayco_camper (Jayco Jay Feather travel trailer): score=3.20
                            color_match=0.7 cab_marker_match=1.0 bed_cover_match=1.0
                         3. v_carson_white (name two's white Silverado pickup): score=2.20
                            color_match=0.7 type_match=0.8
                       Threshold: confidence >= 0.60, gap >= 0.15

                The Motion Telegram fired earlier already showed
                Qwen's verbatim identification. This 2nd Telegram
                reports what the matcher thought (or didn't think).
                """
                msg_lines = [
                    f"❌ <b>No match for vehicle at {camera_name}</b>",
                ]
                if captured_at:
                    try:
                        _dt = datetime.fromisoformat(captured_at)
                        _et = _dt.astimezone(ZoneInfo("America/New_York"))
                        msg_lines.append(f"   {_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                    except (ValueError, TypeError):
                        msg_lines.append(f"   {captured_at}")

                # Phase.78 (2026-08-11) — render the FULL Qwen
                # verifier output for this vehicle. Every key Qwen
                # returned, no curation. The user wants to see exactly
                # what the model said so they can decide if the
                # matcher's no-match verdict is wrong.
                qwen_lines = _render_qwen_dict_lines(vision_vehicle, indent=3)
                if qwen_lines:
                    msg_lines.extend(qwen_lines)
                else:
                    msg_lines.append("   Vision: (empty)")

                # Top-3 candidates with per-dimension scores
                if top3:
                    msg_lines.append("   Top 3 candidates:")
                    for rank, (kv, score, breakdown) in enumerate(top3, start=1):
                        kv_id = kv.get("id", "?")
                        label = kv.get("label", kv_id)
                        msg_lines.append(
                            f"     {rank}. {kv_id} ({label}): score={score:.2f}"
                        )
                        # Sort breakdown by score contribution desc
                        # so the user sees the most influential
                        # dimensions first.
                        if breakdown:
                            ranked_dims = sorted(
                                breakdown.items(), key=lambda kv_pair: -kv_pair[1]
                            )
                            dim_str = ", ".join(
                                f"{d}={w:.1f}" for d, w in ranked_dims
                            )
                            msg_lines.append(f"        dims: {dim_str}")
                else:
                    msg_lines.append("   No candidates scored above 0.")

                conf_t = thresholds.get("confidence", 0.6)
                gap_t = thresholds.get("gap", 0.15)
                msg_lines.append(
                    f"   Threshold: confidence >= {conf_t:.2f}, gap >= {gap_t:.2f}"
                )

                msg = "\n".join(msg_lines)
                log.info(
                    f"[{alert_id}] OFS no-match alert: top3={[t[0].get('id') for t in top3]} "
                    f"thresholds={thresholds}"
                )

                # Photo: prefer the bbox crop that fed the matcher.
                photo_path = None
                if crop_path and os.path.isfile(crop_path):
                    photo_path = crop_path
                elif frame_path and os.path.isfile(frame_path):
                    photo_path = frame_path
                photo_ok = False
                if bot_token and chat_id:
                    if photo_path:
                        try:
                            from infra.send_telegram import (
                                send_photo_with_caption as _tg_send_photo,
                            )
                            photo_ok = _tg_send_photo(
                                bot_token, chat_id, photo_path, msg
                            )
                        except Exception as e:
                            log.warning(f"[{alert_id}] no-match alert photo send failed: {e}")
                    if not photo_ok:
                        try:
                            from infra.send_telegram import send_message as _tg_send_message
                            photo_ok = bool(_tg_send_message(bot_token, chat_id, msg))
                        except Exception as e:
                            log.warning(f"[{alert_id}] no-match alert text send failed: {e}")
                    log_outbound_telegram(
                        channel="gatekeeper_match",
                        alert_id=alert_id,
                        v_id="",
                        event="vehicle_no_match",
                        body=msg,
                        sent=bool(photo_ok),
                        extra=f"top3={[t[0].get('id') for t in top3]} thresholds={thresholds}",
                        image_paths=[photo_path] if photo_path else [],
                    )
                else:
                    log.warning(
                        f"[{alert_id}] OFS no-match alert NOT sent (no telegram creds)"
                    )

            def _send_motion_alert(
                alert_id: str,
                camera_name: str,
                vision_result: dict,
                vehicles_to_report: list,
                frame_paths: list,
                bot_token: str,
                chat_id: str,
                captured_at: str,
                detector_trajectory: list | None = None,
                motion_result=None,
            ) -> None:
                """Phase 2026-08-03: gatekeeper vehicle motion alert.

                Phase.89 (PLAN.md §11.20, Note 2026-08-18):
                minimal alert — single frame + 3-line body (header,
                timestamp, "n. <vehicle> (confidence: 0.95)"). No
                detector metadata, no full Qwen dump, no 6-frame
                composite. Previous behavior (6B.81/6B.78/6B.77) sent
                a 6-frame group + a full diagnostic body.

                Sends one Telegram message per OFS vehicle alert whose
                vision result enumerated at least one VEHICLE IN MOTION.
                Criterion (Note 2026-08-03): vision's per-vehicle
                `motion` field == "moving". Parked vehicles, unknown
                vehicles, and empty scenes do NOT trigger a Telegram.

                Body is delegated to
                ``telegram_formatter.build_minimal_motion_telegram_body``
                so the format is testable and the listener doesn't do
                inline string building (module-purity rule, Phase.89).

                Frame selection: the 4th captured frame (``frame_paths[3]``)
                is the picture-of-record. Falls back to the middle frame
                when fewer than 4 frames were captured, then ``frame_paths[0]``.
                The 4th frame is well-defined for the OFS 6-frame burst
                (Note 2026-08-18: "just the fourth frame").

                This is independent of vehicle_state.py — it does NOT
                require an arrival/departure event to fire.
                """
                vehicles = vehicles_to_report or []
                if not vehicles:
                    return  # gatekeeper condition — caller checked

                # Build the body via the dedicated formatter. The
                # formatter is pure (no I/O) so the listener stays free
                # of inline string building — see AGENTS.md Step 4
                # (each module does one thing). Lazy import keeps
                # `from telegram_formatter import …` out of the
                # module-level imports (module-purity rule).
                from telegram_formatter.motion_telegram import (
                    MotionTelegramInput,
                    build_minimal_motion_telegram_body,
                )
                _motion_input = MotionTelegramInput(
                    camera_name=camera_name,
                    captured_at_iso=captured_at or "",
                    trajectory=detector_trajectory or [],
                    avg_area=(
                        int(motion_result.primary_moving_object.avg_area)
                        if motion_result
                        and getattr(motion_result, "primary_moving_object", None)
                        and getattr(motion_result.primary_moving_object, "avg_area", None)
                        else 0
                    ),
                    vision_result=vision_result if isinstance(vision_result, dict) else None,
                )
                msg = build_minimal_motion_telegram_body(_motion_input, vehicle_idx=0)

                log.info(
                    f"[{alert_id}] OFS motion alert (6B.89 minimal): "
                    f"{len(vehicles)} vehicle(s), msg_len={len(msg)}"
                )

                # Pick the 4th frame for the picture-of-record. Fall
                # back to middle → first when the burst was shorter
                # than 4 frames. Always at least one frame is sent when
                # the burst was non-empty.
                photo_frame: str | None = None
                if frame_paths:
                    if len(frame_paths) >= 4:
                        photo_frame = frame_paths[3]
                    elif len(frame_paths) >= 2:
                        photo_frame = frame_paths[len(frame_paths) // 2]
                    else:
                        photo_frame = frame_paths[0]

                photo_ok = False
                if bot_token and chat_id and photo_frame and os.path.isfile(photo_frame):
                    try:
                        from infra.send_telegram import (
                            send_photo_with_caption as _tg_send_photo,
                        )
                        photo_ok = _tg_send_photo(
                            bot_token, chat_id, photo_frame, msg
                        )
                    except Exception as e:
                        log.warning(f"[{alert_id}] motion alert photo send failed: {e}")

                if not photo_ok and bot_token and chat_id:
                    # Text-only fallback when the photo send fails for
                    # any reason (no frame on disk, Telegram rejected,
                    # network blip). The alert body is still useful.
                    try:
                        from infra.send_telegram import send_message as _tg_send_message
                        photo_ok = bool(_tg_send_message(bot_token, chat_id, msg))
                    except Exception as e:
                        log.warning(f"[{alert_id}] motion alert text send failed: {e}")

                if photo_ok:
                    log_outbound_telegram(
                        channel="gatekeeper_motion",
                        alert_id=alert_id,
                        v_id="",
                        event="vehicle_motion",
                        body=msg,
                        sent=True,
                        extra=(
                            f"vehicles={len(vehicles)} camera={camera_name} "
                            f"frames={len(frame_paths)} frame_used={photo_frame}"
                        ),
                        image_paths=[photo_frame] if photo_frame else list(frame_paths),
                    )
                else:
                    log.warning(
                        f"[{alert_id}] OFS motion alert NOT sent (no telegram creds or send failed)"
                    )

            # Phase.79 (2026-08-14) — composite motion-trail alert.
            # Renders a single 1280x1280 image showing, for each of the
            # 6 captured frames: the raw scene + the pairwise differential
            # diff mask painted on top + the differential's bbox in green
            # + a frame-number label. This is the "see what the detector
            # saw" companion to the lead motion Telegram.
            #
            # Independent of vision classification — the composite just
            # renders the data the differential already produced
            # (MovingObject.bbox_per_frame + trajectory). Sends as a
            # separate Telegram so the operator gets the motion trail
            # visualization distinct from the arriving/motion media
            # group and the matcher verdict.
            def _send_composite_alert(
                alert_id: str,
                camera_name: str,
                frame_paths: list,
                motion_result_arg,  # MotionResult — typed loosely to avoid
                # importing infra.motion_detector.MotionResult at module
                # level; the listener already imports it.
                bot_token: str,
                chat_id: str,
                captured_at: str | None,
            ) -> None:
                """Send the motion-trail composite as a separate Telegram.

                Skips silently if the differential didn't find motion
                or if the composite render fails for any reason
                (e.g. a frame path is unreadable on disk). The motion
                alert and match alert are not affected by a composite
                failure.
                """
                primary = (
                    motion_result_arg.primary_moving_object
                    if motion_result_arg is not None else None
                )
                if primary is None or not primary.bbox_per_frame:
                    log.info(
                        f"[{alert_id}] composite_alert: skipped "
                        f"(no primary mover or empty bboxes)"
                    )
                    return

                try:
                    from infra.motion_visualization import (
                        render_motion_composite,
                    )
                    composite_path = render_motion_composite(
                        frame_paths=frame_paths,
                        moving_object=primary,
                    )
                except Exception as e:
                    log.warning(
                        f"[{alert_id}] composite_alert: render failed {e!r}, "
                        f"falling back to no composite"
                    )
                    return

                if not composite_path or not os.path.isfile(composite_path):
                    log.warning(
                        f"[{alert_id}] composite_alert: render returned "
                        f"no path, skipping"
                    )
                    return

                traj_str = " → ".join(primary.trajectory or [])
                msg_lines = [f"🛣️ <b>Motion trail at {camera_name}</b>"]
                if captured_at:
                    msg_lines.append(f"   {captured_at}")
                if traj_str:
                    msg_lines.append(f"   trajectory: {traj_str}")
                msg = "\n".join(msg_lines)

                photo_ok = False
                if bot_token and chat_id:
                    try:
                        from infra.send_telegram import (
                            send_photo_with_caption as _tg_send_photo,
                        )
                        photo_ok = _tg_send_photo(
                            bot_token, chat_id, composite_path, msg
                        )
                    except Exception as e:
                        log.warning(
                            f"[{alert_id}] composite_alert: send failed {e!r}"
                        )
                if photo_ok:
                    log.info(
                        f"[{alert_id}] composite_alert: sent "
                        f"path={composite_path} size_kb={os.path.getsize(composite_path) // 1024} "
                        f"trajectory={traj_str}"
                    )
                else:
                    log.warning(
                        f"[{alert_id}] composite_alert NOT sent "
                        f"(no telegram creds or send failed)"
                    )

            # Phase.57 (2026-08-05) — DELETED the entire cascade
            # below:
            #   - `_focused_pass_for_unknown_arrival(vehicle_events, ...)`
            #     (Phase.6 Stage B — refined make/model from crops)
            #   - `_convert_unknown_to_known_after_focused_pass(...)`
            #     (Phase.8/6B.10 — re-run matcher + 2nd Telegram)
            #   - `vehicle_event_handler.handle_vehicle_events(
            #         vehicle_events, _vehicle_send)`
            #     (Phase.0 — arrived/departed Telegrams)
            # With the state machine removed (vehicle_events = None)
            # and `_vehicle_send` deleted, these would crash. The
            # matcher-then-2nd-Telegram path below is the replacement.

            # Phase 2026-08-03: gatekeeper-camera vehicle motion alert.
            # Note: vehicle state (arrival/departure) is no longer the
            # primary signal. The primary signal is "<gatekeeper> just saw a
            # vehicle in motion — tell me about it." This sends a
            # Telegram ONLY when vision returned at least one vehicle
            # AND that vehicle's motion field is "moving". No alert
            # when vision sees only parked/stationary vehicles, when
            # vision can't identify the vehicle, or when vision sees
            # no vehicle at all. The vehicle_tracker channel above is
            # left in place for back-compat; this is the new
            # always-on motion alert.
            #
            # Phase.93 (2026-08-18) — extends the match-alert path
            # from OFS-only to all GATEKEEPER_CAMERAS. OFG joined the
            # gatekeeper tier in 6B.87 (persistent RTSP + motion detector +
            # 3-crop vision pipeline) but the match-alert loop at
            # line ~3639 was still hard-coded to OFS, dropping the
            # 2nd-Telegram (and the no-match 3rd Telegram) for every
            # OFG vehicle event. Verified on alert 0eefa8e9 (Tesla
            # drive-by 15:08 EDT): crop vision correctly identified
            # Tesla Model Y blue with conf=0.98 on 3 crops; synthesis
            # populated vision_result["vehicles"][0] correctly; but the
            # match loop never ran because camera_name != OFS.
            # Symptom: zero 2nd-Telegrams, zero no-match Telegrams for
            # OFG vehicle events, even when the matcher would have
            # cleared v_owner1_darkblue_tesla_y. Fixed by switching the
            # gate to `camera_name in GATEKEEPER_CAMERAS:` so OFS + OFG
            # both got the lead motion + match + no-match Telegram stack.
            #
            # Phase.104 (2026-08-20) — OFG demoted from gatekeeper
            # tier per Note's request that OFG behave like the other
            # cameras. The gate below still says `camera_name in
            # GATEKEEPER_CAMERAS:` but the set is now OFS-only, so OFG
            # vehicle events once again do NOT reach this match-alert
            # loop (intentional this time, not the 6B.93 bug). See
            # PLAN §11.32 for the full demotion narrative.
            if camera_name in GATEKEEPER_CAMERAS:
                vehicles_in_scene = (
                    vision_result.get("vehicles") if isinstance(vision_result, dict) else None
                )
                if vehicles_in_scene:
                    # Phase 1.1b (2026-08-05) — OR gate. A vehicle triggers
                    # the motion alert when EITHER the structured `motion`
                    # field == "moving" OR Qwen's prose implies motion
                    # (substring match against motion_reasoning /
                    # description / caption). This closes the morning-Tesla
                    # failure mode (alert 21973131 13:51:51 EDT — Qwen
                    # described "moving across the gravel road" but the
                    # field was stationary, so no Telegram fired).
                    _MOTION_KEYWORDS = (
                        "moving", "driving", "drives", "approaching",
                        "entering", "passing", "crossing",
                    )
                    def _prose_implies_motion(v: dict) -> bool:
                        _reasoning = (
                            v.get("motion_reasoning")
                            or v.get("description")
                            or v.get("caption")
                            or ""
                        ).lower()
                        return any(kw in _reasoning for kw in _MOTION_KEYWORDS)

                    # Phase.71 (2026-08-08) — detector is the sole source of
                    # truth for motion. The earlier 2-OR-gate (vision
                    # motion field + prose-substring) was the cause of the
                    # missed Tesla departure (alert 2d58e423, 13:14 EDT):
                    # vision saw the car as stationary because the
                    # captured crops showed it parked-or-just-starting,
                    # but the OpenCV motion detector correctly observed
                    # motion across the 6 frames. Note's principle
                    # (2026-08-08): "the only thing that should determine
                    # if there's motion is the OpenCV motion detector."
                    #
                    # Safety threshold: 5000 px of total motion pixels
                    # (filters spider webs, single-frame noise, camera
                    # vibration). The detector already has
                    # MIN_FRAMES_SEEN >= 3 + MIN_AVG_AREA_PX >= 500 +
                    # POSITION_CHANGE_MIN >= 5 filters built in.
                    #
                    # Scope: detector must have run (motion_result is
                    # not None) and must have found something. Vision
                    # still gates the "is this a vehicle" question
                    # (vehicles_in_scene is non-empty above).
                    _detector_finds_motion = bool(
                        motion_result is not None
                        and not motion_result.no_motion_detected
                        and motion_result.total_motion_pixels >= 5000
                    )
                    # Phase.91 (2026-08-18) — prose-OR fallback.
                    # Detector-only (6B.71) drops the alert when OpenCV
                    # misses (Tesla drive-by test 15:08 EDT: 1 candidate
                    # with 140,837 px total but filtered out at
                    # MIN_FRAMES_SEEN / POSITION_CHANGE_MIN). Vision
                    # correctly identified a blue sedan moving across
                    # the gravel road but the OFS gate said no motion.
                    # The fix: prefer detector, but fall back to
                    # prose-implies-motion per vehicle (1.1b logic,
                    # already present as dead code at lines 3386-3397)
                    # when detector missed. Stricter than 1.1b: only
                    # triggers when (a) detector missed AND (b) vision
                    # saw a vehicle in scene. Preserves 6B.71's
                    # detector-truth preference while closing the
                    # morning-Tesla gap.
                    _use_prose_fallback = (
                        not _detector_finds_motion
                        and bool(vehicles_in_scene)
                    )
                    moving_vehicles = (
                        [
                            v for v in vehicles_in_scene
                            if _detector_finds_motion or _prose_implies_motion(v)
                        ]
                        if (_detector_finds_motion or _use_prose_fallback)
                        else []
                    )
                    _detector_trajectory = (
                        motion_result.primary_moving_object.trajectory
                        if motion_result is not None
                        and motion_result.primary_moving_object is not None
                        else []
                    )
                    log.info(
                        f"[{alert_id}] OFS motion gate (6B.71): "
                        f"detector_finds_motion={_detector_finds_motion} "
                        f"total_motion_px={motion_result.total_motion_pixels if motion_result else 0} "
                        f"reference_method={motion_result.reference_method if motion_result else 'n/a'} "
                        f"trajectory={_detector_trajectory} "
                        f"vehicles_in_scene={len(vehicles_in_scene)} "
                        f"moving_vehicles={len(moving_vehicles)}"
                    )
                    # Phase 1.1a (2026-08-05) — disagreement logger.
                    # When vision describes motion in its prose but the
                    # structured `motion` field is stationary (or vice
                    # versa), capture it for postmortem. This is the
                    # morning-Tesla failure mode (alert 21973131:
                    # "moving across the gravel road" but threat_level=0).
                    # Prose source: the alert_generator output is not yet
                    # available here, so we use Qwen's per-vehicle
                    # `motion_reasoning` (if present in the vision
                    # result) or, as a fallback, a simple substring
                    # check against any prose-shaped field. This is a
                    # diagnosis aid, not a correctness check — we are
                    # *finding* the disagreements, not fixing them.
                    for _pv in vehicles_in_scene:
                        _field = (_pv.get("motion") or "").strip().lower()
                        _field_moving = _field == "moving"
                        # Only check prose-vs-field disagreement when
                        # there's prose to compare against. Otherwise
                        # we'd log noise for every "no reasoning"
                        # vehicle (i.e. the prompt's reasoning field
                        # wasn't populated). Skip quietly.
                        _reasoning = (
                            _pv.get("motion_reasoning")
                            or _pv.get("description")
                            or _pv.get("caption")
                            or ""
                        )
                        if not _reasoning:
                            continue
                        _prose = _prose_implies_motion(_pv)
                        if _prose != _field_moving:
                            log.warning(
                                f"[{alert_id}] OFS motion disagreement: "
                                f"prose_implies_moving={_prose} "
                                f"field={_field!r}; "
                                f"color={_pv.get('color')!r} "
                                f"type={_pv.get('body_style_hint') or _pv.get('type')!r} "
                                f"fired={'yes' if (_prose or _field_moving) else 'no'}"
                            )
                    if moving_vehicles:
                        # Phase 1.2 (2026-08-05) — motion+match cooldown.
                        # Two webhooks for the same physical event within
                        # 60s would otherwise produce two motion alerts +
                        # two match alerts (see LOGIC-FLOWS §F2.5c).
                        # Key: (camera, captured_at minute). The standard
                        # alert path has UUID-keyed cooldown in
                        # `notifier._alert_cooldown`; this is the
                        # motion+match equivalent.
                        _cooldown_key = (camera_name, timestamp[:16]) if timestamp else None
                        if _cooldown_key and _MOTION_COOLDOWN.is_cool(_cooldown_key):
                            log.info(
                                f"[{alert_id}] OFS motion cooldown: "
                                f"key={_cooldown_key} — suppressing duplicate motion alert"
                            )
                            # Drop the alert entirely (no lead motion, no
                            # match loop). The standard alert path is
                            # unaffected and can still fire.
                        else:
                            if _cooldown_key:
                                _MOTION_COOLDOWN.mark(_cooldown_key)
                            _send_motion_alert(
                                alert_id=alert_id,
                                camera_name=camera_name,
                                vision_result=vision_result,
                                vehicles_to_report=moving_vehicles,
                                frame_paths=frame_paths,
                                bot_token=bot_token,
                                chat_id=chat_id,
                                captured_at=timestamp,
                                detector_trajectory=_detector_trajectory,
                                motion_result=motion_result,
                            )
                            # Phase.79 (2026-08-14) — fire the
                            # motion-trail composite immediately after
                            # the lead motion alert. This is the 2nd
                            # Telegram in the OFS message stack
                            # (lead motion + composite + match).
                            # Rendered independently from vision: just
                            # the differential's own data
                            # (bbox_per_frame, trajectory).
                            _send_composite_alert(
                                alert_id=alert_id,
                                camera_name=camera_name,
                                frame_paths=frame_paths,
                                motion_result_arg=motion_result,
                                bot_token=bot_token,
                                chat_id=chat_id,
                                captured_at=timestamp,
                            )

                        # ------------------------------------------------------------------
                        # 2026-08-19 — MATCHER RUNS AFTER TELEGRAMS #1 AND #2.
                        # Note: "What I want is for the matcher to run after
                        # the other two alerts are sent to me. Why is that so
                        # hard to explain? What you were doing before it was
                        # just running the match before I was sent the alerts.
                        # Can't you just take the output of the vision model,
                        # stuff it in a variable, and hold onto that until you
                        # get to the part of the loop where you need to run
                        # the match?"
                        #
                        # vision_result and moving_vehicles are already in
                        # scope as variables in the OFS/OFG alert handler. The
                        # match loop runs here, AFTER _send_motion_alert (Telegram
                        # #1) and _send_composite_alert (Telegram #2) have both
                        # fired. The match loop is wrapped in its OWN
                        # try/except so any matcher failure is logged but
                        # cannot suppress Telegrams #1 or #2 — Telegrams already
                        # went out.
                        #
                        # The 6B.96 lazy-import discipline (`from telegram_formatter
                        # import ` must not pull in match_telegram) is preserved:
                        # match_telegram / no_match_telegram are imported lazily
                        # when this block runs, AFTER Telegrams #1 and #2 have
                        # been delivered to the user. The first-alert import
                        # chain stays clean.
                        # ------------------------------------------------------------------
                        try:
                            from infra.vehicle_matcher import (
                                load_spec,
                                match_with_details,
                                score_top_n,
                            )
                            from known_vehicles import load_known_vehicles
                            from vehicle_identifier.signature import extract_signature
                            _known_v = load_known_vehicles()
                            # Pick the same best frame the lead motion
                            # alert used so the user sees one picture
                            # for both messages.
                            _match_frame = None
                            if frame_paths:
                                _legacy = (
                                    vision_result.get("best_frame_index")
                                    if isinstance(vision_result, dict)
                                    else None
                                )
                                if (
                                    isinstance(_legacy, (int, float))
                                    and 0 < int(_legacy) <= len(frame_paths)
                                ):
                                    _match_frame = frame_paths[int(_legacy) - 1]
                                elif len(frame_paths) >= 3:
                                    _match_frame = frame_paths[len(frame_paths) // 2]
                                else:
                                    _match_frame = frame_paths[0]
                            matches_sent = 0
                            no_match_vehicles: list = []  # Phase.77
                            for _mv in moving_vehicles:
                                # extract_signature() expects a
                                # vision_result-shaped dict and picks
                                # primary_vehicle_index. Wrap each
                                # moving vehicle so per-vehicle sigs
                                # come out cleanly.
                                _wrap = {
                                    "vehicles": [_mv],
                                    "primary_vehicle_index": 0,
                                }
                                _sig = extract_signature(_wrap)
                                if not _sig:
                                    continue
                                _md = match_with_details(_sig, _known_v)
                                if _md is None:
                                    # Phase.77 (2026-08-11) — the
                                    # matcher cleared nothing. Track
                                    # this vehicle so we can fire a
                                    # no-match Telegram below with
                                    # the top-3 candidates. The
                                    # Motion Telegram already showed
                                    # Qwen's identification verbatim;
                                    # this 2nd Telegram is the
                                    # matcher's verdict (or lack
                                    # thereof).
                                    no_match_vehicles.append(
                                        (_mv, _sig)
                                    )
                                    continue
                                # Phase.67 — best_crop_path is set by
                                # Phase.78b (2026-08-14) — best_crop_path
                                # is a dataclass attribute, not a dict key.
                                # identify_from_crops when the new 3-crop
                                # path ran (Phase.65); None for the
                                # legacy single-crop / 6-frame path.
                                _best_crop_path = (
                                    id_result.best_crop_path
                                    if id_result is not None
                                    else None
                                )
                                _send_match_alert(
                                    alert_id=alert_id,
                                    camera_name=camera_name,
                                    vision_vehicle=_mv,
                                    match_detail=_md,
                                    frame_path=_match_frame,
                                    crop_path=_best_crop_path,
                                    bot_token=bot_token,
                                    chat_id=chat_id,
                                    captured_at=timestamp,
                                )
                                matches_sent += 1
                            if matches_sent:
                                log.info(
                                    f"[{alert_id}] OFS match path: "
                                    f"{matches_sent} of {len(moving_vehicles)} "
                                    f"moving vehicle(s) matched a known entry"
                                )
                            else:
                                log.info(
                                    f"[{alert_id}] OFS match path: "
                                    f"0 of {len(moving_vehicles)} moving "
                                    f"vehicle(s) cleared the spec threshold"
                                )
                            # Phase.77 (2026-08-11) — fire a no-match
                            # Telegram for each moving vehicle that
                            # didn't match. The Motion Telegram already
                            # showed Qwen's verbatim identification;
                            # this 2nd Telegram reports what the matcher
                            # considered (top-3 with breakdowns) so the
                            # user can see why nothing cleared. Anti-
                            # spam: only fires for vehicles where vision
                            # succeeded AND motion was detected AND the
                            # matcher returned None.
                            if no_match_vehicles:
                                # score_top_n + load_spec are imported
                                # once at the top of the match block
                                # (line ~3129). No need to re-import here.
                                _spec = load_spec()
                                _thresholds = _spec.get(
                                    "thresholds",
                                    {"confidence": 0.6, "gap": 0.15},
                                )
                                if _spec is not None:
                                    for _nmv, _nsig in no_match_vehicles:
                                        _top3 = score_top_n(
                                            _nsig, _known_v, n=3, spec=_spec
                                        )
                                        # Phase.78b (2026-08-14) —
                                        # best_crop_path is a dataclass
                                        # attribute, not a dict key.
                                        _nm_best_crop = (
                                            id_result.best_crop_path
                                            if id_result is not None
                                            else None
                                        )
                                        _send_no_match_alert(
                                            alert_id=alert_id,
                                            camera_name=camera_name,
                                            vision_vehicle=_nmv,
                                            top3=_top3,
                                            thresholds=_thresholds,
                                            frame_path=_match_frame,
                                            crop_path=_nm_best_crop,
                                            bot_token=bot_token,
                                            chat_id=chat_id,
                                            captured_at=timestamp,
                                        )
                        except Exception as _match_exc:
                            # The matcher MUST NEVER break the motion
                            # alert path. The first two Telegrams have
                            # already gone out — this 3rd Telegram is
                            # optional enrichment. If anything fails
                            # here, log and move on.
                            # Phase 1.3 (2026-08-05) — track the failure
                            # so /status surfaces a sustained matcher
                            # failure (rate > 1/5min) instead of leaving
                            # it invisible. The first failure in a
                            # 5-min window stays at WARNING; subsequent
                            # failures in the same window escalate to
                            # ERROR for the postmortem grep.
                            _failure_count = _MATCHER_FAILURES.record(_match_exc)
                            if _failure_count >= 2:
                                log.error(
                                    f"[{alert_id}] OFS match path failed "
                                    f"({_failure_count}x in 5min): {_match_exc!r}"
                                )
                            else:
                                log.warning(
                                    f"[{alert_id}] OFS match path failed: {_match_exc!r}"
                                )
                    else:
                        log.info(
                            f"[{alert_id}] OFS motion alert SKIPPED: "
                            f"vision saw {len(vehicles_in_scene)} vehicle(s) but "
                            f"none are moving (motion field not 'moving')"
                        )
                else:
                    # Phase.63 (2026-08-07) — fallback Telegram send
                    # when vision failed but frames were captured cleanly.
                    # If vision returned an error sentinel ("error" in
                    # objects_detected), don't silently drop — push the 6
                    # captured frames to Telegram so the user can see the
                    # scene themselves. Note's ask: "send me whatever
                    # picture you get from it" when vision can't classify.
                    _vision_failed = (
                        isinstance(vision_result, dict)
                        and "error" in (vision_result.get("objects_detected") or [])
                    )
                    if _vision_failed and frame_paths:
                        log.warning(
                            f"[{alert_id}] OFS motion alert SKIPPED: vision "
                            f"failed — sending {len(frame_paths)} captured "
                            f"frames to Telegram as fallback"
                        )
                        try:
                            from infra.send_telegram import send_photo_group
                            _fallback_caption = (
                                f"⚠️ [OFS_VISION_FAILED] {camera_name} vehicle "
                                f"webhook at {timestamp}.\n\n"
                                f"Vision analysis failed (queue error / parse error). "
                                f"Frames captured cleanly from persistent RTSP ring buffer. "
                                f"Reviewing {len(frame_paths)} images to determine activity."
                            )
                            _sent = send_photo_group(
                                bot_token=bot_token,
                                chat_id=chat_id,
                                frame_paths=frame_paths,
                                caption=_fallback_caption,
                            )
                            log_outbound_telegram(
                                channel="ofs_vision_failed_fallback",
                                alert_id=alert_id,
                                v_id="",
                                event="ofs_vision_failed",
                                body=_fallback_caption,
                                sent=bool(_sent),
                                extra=f"frames={len(frame_paths)}",
                                image_paths=frame_paths,
                            )
                        except Exception:
                            log.exception(
                                f"[{alert_id}] OFS vision-failed fallback send failed"
                            )
                    else:
                        log.info(
                            f"[{alert_id}] OFS motion alert SKIPPED: "
                            f"vision saw no vehicles in scene"
                        )

            # Phase.x channel retirement — channel #3 retired.
            # vehicle_tracker already emits "📡 Vehicle Tracker / ⚠️
            # Unknown vehicle V-NNN arrived at <camera>" via the state
            # tracker path; the alert_notifier escalation below
            # duplicates that with a separate "Unknown vehicle at
            # front corner" message. With this gate closed, the user
            # gets one notification per unknown arrival instead of two.
            # Re-enable by setting FARM_VEHICLE_ESCALATION_ENABLED=1.
            if not VEHICLE_ESCALATION_ENABLED:
                log.info(
                    f"[{alert_id}] vehicle escalation SKIPPED: "
                    f"channel #3 retired (FARM_VEHICLE_ESCALATION_ENABLED=0). "
                    f"vehicle_tracker channel remains the sole Telegram source."
                )
            else:
                for evt in vehicle_events:
                    if evt.get("event") != "unknown_arrival":
                        continue
                    try:
                        v_id = evt.get("v_id") or evt.get("vehicle_id") or "unknown"

                        # Build description: include refined fields when the
                        # focused classify pass provided them.
                        color = evt.get("color", "?")
                        vtype = evt.get("type", "?")
                        make = evt.get("make", "")
                        model = evt.get("model", "")
                        features = evt.get("distinctive_features", "")

                        desc_parts = [
                            (
                                f"A {color} {vtype} was detected at {camera_name} "
                                f"at {evt.get('ts', '')}."
                            )
                        ]
                        if make or model:
                            desc_parts.append(
                                f"Refined identification: "
                                f"{' '.join(p for p in (make, model) if p).strip()}."
                            )
                        if features:
                            desc_parts.append(f"Notable: {features}.")
                        # Phase.10 — Note: "I prefer my notifications not to
                        # tell me what to do like review footage." Strip the
                        # "If this vehicle is unfamiliar, review footage..."
                        # tail. The alert still tells the user what was seen
                        # and where; the user decides next steps.

                        escalation_alert = {
                            "alert_id": f"{alert_id}-unknown-vehicle-{v_id}",
                            "camera": camera_name,
                            "timestamp": timestamp,
                            "threat_level": 1,
                            "title": "Unknown vehicle at front corner",
                            "description": " ".join(desc_parts),
                            # Phase.10 — recommendations removed (same
                            # reason as the description tail). The notifier
                            # handles missing recommendations gracefully.
                            "frame_path": best_frame_path,
                        }
                        sent = notify(
                            alert=escalation_alert,
                            bot_token=bot_token,
                            chat_id=chat_id,
                            cooldown_seconds=120,
                            vision_result=vision_result,
                        )
                        log.info(
                            f"[{alert_id}] unknown vehicle → escalated "
                            f"(threat=1, sent={sent}, v_id={v_id})"
                        )
                    except Exception as inner_err:
                        log.warning(
                            f"[{alert_id}] unknown vehicle escalation failed: {inner_err}"
                        )
        except Exception as err:
            log.warning(f"[{alert_id}] unknown vehicle path swallowed: {err}")

        # Pick the best frame to send with the alert.
        # Preference order (revised 2026-07-25 after morning incident):
        #   1. face_visibility.best_frame_index from vision (if present)
        #   2. vision_result["best_frame_index"] (legacy)
        #   3. Latest frame in selected_frames (Phase.9 default — works
        #      for arrivals; vehicle is closer by the end of the burst)
        #   4. Middle of full frame_paths burst (better for departures
        #      and for short bursts where the last frame is at the edge)
        #   5. First frame (last-resort fallback)
        best_frame_path = None
        # Phase.13 (2026-07-26): vehicle events send all frames in one
        # call, so the analysis_frames we sent == all captured frames.
        # For non-vehicle events selected_frames comes from
        # select_frames() in the legacy two-pass path above. Pick whichever
        # is populated -- the analysis set the vision call actually saw.
        # Vehicle: selected_frames stays [] (we never called select_frames),
        # so analysis_frames = frame_paths. Non-vehicle: selected_frames
        # is the 1-frame first pass (or the escalated multi-frame).
        analysis_frames = locals().get("selected_frames") or frame_paths
        # Phase.27 (2026-07-30): same fix as the vehicle-event path —
        # do NOT consult face_visibility.best_frame_index. The default
        # value=1 hijacks the priority chain. Note: "why is a face
        # visible relevant at all to a vehicle classify?"
        legacy = vision_result.get("best_frame_index")
        if (
            isinstance(legacy, (int, float))
            and 0 < int(legacy) <= len(analysis_frames)
        ):
            best_frame_path = analysis_frames[int(legacy) - 1]
        elif len(analysis_frames) >= 2:
            # Phase.9: latest frame in the analysis set -- for
            # arrivals, vehicle is closer/larger than at frame 0.
            best_frame_path = analysis_frames[-1]
        elif len(analysis_frames) == 1:
            best_frame_path = analysis_frames[0]
        elif len(frame_paths) >= 3:
            best_frame_path = frame_paths[len(frame_paths) // 2]
        elif frame_paths:
            best_frame_path = frame_paths[0]

        # 3. Generate alert
        alert = generate_alert(
            vision_result=vision_result,
            camera_name=camera_name,
            timestamp=timestamp,
            source="rtsp_frames",
            api_url=TEXT_API_URL,
        )

    # Use the original alert_id (not the one the LLM generated — we want traceability).
    # Phase.9: vehicle events suffix with "-identified" so the notifier's
    # 120s cooldown treats message 2 as independent from message 1's
    # "-arriving" alert — without the suffix, the second send would be
    # suppressed as a "duplicate" of message 1.
    if is_vehicle_event:
        alert["alert_id"] = f"{alert_id}-identified"
    else:
        alert["alert_id"] = alert_id

    # Attach the best frame so notifier can sendPhoto with caption.
    # notifier.py handles missing files by falling back to sendMessage.
    alert["frame_path"] = best_frame_path

    if alert.get("title") == "error":
        log.error(f"[{alert_id}] Alert generation failed")
        STATE["by_threat_level"][-1] += 1
        return

    log.info(
        f"[{alert_id}] Alert generated: Level {alert.get('threat_level')} — {alert.get('title')}"
    )

    # Arrival detection: bump L0 → L1 only on the empty→occupied transition
    # (e.g. 9 AM arrival after a quiet night). Once you're there, motion events
    # fire every ~30s — we don't want to nag, the text model classifies those
    # correctly as routine. The 4-hour gap means "long quiet, someone arrived".
    if alert.get("threat_level") == 0 and _vision_shows_person(vision_result):
        if is_arrival(camera_name):
            log.info(f"[{alert_id}] Arrival detected — bumping L0 → L1")
            alert["threat_level"] = 1
            alert["source"] = "arrival"
            title_lower = (alert.get("title") or "").lower()
            if any(
                kw in title_lower
                for kw in ["routine", "no threat", "all clear", "no concern"]
            ):
                alert["title"] = f"Arrival detected — Person present in {camera_name}"
        # Always record the person-seen timestamp so future motion events
        # within the gap are NOT classified as arrivals.
        record_person_seen(camera_name, when_iso=timestamp)

    # Phase 6A: face recognition + property state + response engine.
    # Runs BEFORE Telegram notification so identified/unknown messages
    # arrive in the same conversational flow. Phase 6A failures are
    # swallowed internally — this call cannot break the existing pipeline.
    try:
        run_phase6a_recognition(
            frame_paths=frame_paths,
            vision_result=vision_result,
            camera=camera_name,
        )
    except Exception as err:
        log.warning(f"[{alert_id}] Phase 6A swallowed at caller: {err}")

    # 4. Persist alert to JSONL history (outbox)
    # Phase.24 §5 fix — write to audit log BEFORE notify so a failed
    # history write doesn't leave orphan Telegram messages, and a
    # successful Telegram send isn't lost if the audit log later
    # fails. Prevents the race window where notify() succeeds but
    # append_alert() fails — the alert existed in Telegram but not in
    # the audit trail.
    history_ok = append_alert(alert)
    if not history_ok:
        log.warning(
            f"[{alert_id}] outbox_failed: append_alert returned False — "
            "skipping Telegram send to avoid orphan message."
        )
        sent = False
    else:
        # 5. Notify (only if outbox succeeded)
        # Telegram creds are loaded earlier (before vehicle_state.tick) so
        # vehicle events and the main pipeline share the same transport.
        sent = notify(
            alert=alert,
            bot_token=bot_token,
            chat_id=chat_id,
            cooldown_seconds=120,
            vision_result=vision_result,
        )

    # Update state
    STATE["total_alerts"] += 1
    threat_level = alert.get("threat_level", -1)
    STATE["by_threat_level"][threat_level] = (
        STATE["by_threat_level"].get(threat_level, 0) + 1
    )
    STATE["last_alert"] = {
        "alert_id": alert_id,
        "camera": camera_name,
        "timestamp": timestamp,
        "threat_level": threat_level,
        "title": alert.get("title"),
        "sent_to_telegram": sent,
        "persisted_to_history": history_ok,
    }

    log.info(f"[{alert_id}] Pipeline complete. Telegram: {sent}, History: {history_ok}")

