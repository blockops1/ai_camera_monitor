"""
_gate_aware_capture — wire the motion-gate's frames into the vehicle and
person pipelines.

Phase 6B.115 (§11.46.6, 2026-08-25): the gate now returns frames + crops
as in-memory PIL.Image via GateVerdict. This module copies them onto
ctx for downstream stages. No filesystem check — the verdict object IS
the signal that the gate ran. Legacy fallback was REMOVED.

Phase 6B.139 (§11.60, 2026-08-27): person pipeline gets the same gate-aware
treatment. The 6-second-late fresh RTSP pull (`person_capture_stage`'s
`capture_frames` call) is sidelined; person events now reuse the gate's
4 frames directly. See PLAN §11.60 for the rationale (Qwen was analyzing
the wrong moment).

STATUS: stable
THREAD SAFETY: single-threaded (called from process_alert /
    process_person_event on a per-alert basis; no shared mutable state).

INPUTS:
  - ctx: AlertContext (vehicle) or PersonContext (person), set by listener.py
  - ctx.gate_verdict: GateVerdict | None (set by listener.py)
  - GateVerdict.frames: list[PIL.Image.Image] (4 full frames)
  - GateVerdict.frame_paths: list[str] (4 disk paths, only when
    GATE_KEEP_DISK_ARTIFACTS=true; [] otherwise)
  - GateVerdict.crop_a, GateVerdict.crop_b: PIL.Image.Image | None

OUTPUTS:
  For vehicle path (gate_aware_vehicle_capture):
  - mutates ctx.frames (4 PIL.Image frames)
  - mutates ctx.frame_paths (4 strings, only when GATE_KEEP_DISK_ARTIFACTS=true)
  - mutates ctx.crop_a, ctx.crop_b (PIL.Image crops)
  - mutates ctx.capture_source ("gate")

  For person path (gate_aware_person_capture):
  - mutates ctx.frames (4 PIL.Image frames, mirrored for parity with vehicle)
  - mutates ctx.frame_paths (2 disk paths — frames[1] and frames[2],
    written to ctx.output_dir when verdict.frame_paths is empty; copied
    from verdict.frame_paths when GATE_KEEP_DISK_ARTIFACTS=true)
  - mutates ctx.crop_a, ctx.crop_b (PIL.Image crops)
  - mutates ctx.capture_source ("gate")
  - mutates ctx.selected_frames (2 PIL.Image — frames[1], frames[2])

PUBLIC API:
  - SkipEvent (exception) — raised when the gate did not produce frames.
    The pipeline catches this and returns a sent=False result.
  - gate_aware_vehicle_capture(ctx: AlertContext) -> None
    Vehicle pipeline gate-aware capture. Copies verdict.frames / crop_a /
    crop_b onto ctx. If verdict is None or has no frames, raises SkipEvent.
  - gate_aware_person_capture(ctx: PersonContext) -> None
    Person pipeline gate-aware capture. Mirrors the vehicle function.
    Reads 4 PIL from verdict.frames (always present), populates ctx.frames,
    ctx.crop_a, ctx.crop_b, ctx.selected_frames. Writes 2 selected frames
    to disk under ctx.output_dir (regardless of GATE_KEEP_DISK_ARTIFACTS
    state) so downstream analyze_frames_queued has deterministic paths.
    If verdict is None or has no frames, raises SkipEvent.

DOES NOT DO:
  - Does NOT call motion_gate_pipeline (listener.py already does that)
  - Does NOT call the pipeline's other stages (just frame copy)
  - Does NOT change Telegram behavior or audit logging
  - Does NOT call capture_stage() — legacy 6-frame / 2-frame RTSP pull
    is REMOVED for both vehicle and person paths.
  - Does NOT check `os.path.isfile()` on the gate's writes. The verdict
    object IS the authoritative signal. See §11.46.6 for the rationale
    (eliminate the TOCTOU race, not race it).
  - Does NOT route the person pipeline to the vehicle pipeline. Both
    paths read from the SAME gate verdict but populate their OWN ctx.

CALLED BY:
  - listener.vehicle_event_pipeline.process_alert (replaces capture_stage call)
  - listener.person_event_pipeline.process_person_event (replaces
    person_capture_stage call)

RELATED:
  - PLAN.md §11.46.6 (Phase 6B.115, in-memory frames via verdict)
  - PLAN.md §11.38 (gate-aware pipeline cutover, predecessor)
  - PLAN.md §11.60 (Phase 6B.139, person path mirror — this commit)
  - listener.motion_gate_pipeline.run (the gate that produces the frames)
"""

from __future__ import annotations

import logging
import os

# PIL is not imported here directly — the verdict carries PIL.Image
# objects which we save to disk via .save(). Phase 6B.139 disk-write
# fallback path uses os.path.join + pil_img.save() (no PIL import).

log = logging.getLogger(__name__)


class SkipEvent(Exception):
    """Raised when the motion gate did not produce frames.

    Phase 6B.115: legacy fallback is gone. If the gate didn't run (no
    verdict) or produced no frames, the alert cannot be processed.
    process_alert catches this and returns a sent=False result.
    """


def gate_aware_vehicle_capture(ctx) -> None:
    """Vehicle pipeline gate-aware capture. Reads the gate's verdict.

    Phase 6B.115 (§11.46.6): the gate returns frames + crops as
    PIL.Image via GateVerdict. We copy them onto ctx — no filesystem
    check on the hot path. The verdict object IS the signal.

    Raises:
        SkipEvent: if ctx.gate_verdict is None, or has no frames, or
            has frames but no crops (gate failed to produce useful
            output). The pipeline catches this and returns sent=False.
    """
    verdict = getattr(ctx, "gate_verdict", None)
    if verdict is None:
        log.error(
            f"[{ctx.alert_id}] gate_aware_capture: NO GATE VERDICT — "
            f"motion gate did not run. Phase 6B.115: no legacy fallback. "
            f"Alert will be sent=False."
        )
        ctx.capture_source = "missing"
        raise SkipEvent("no gate verdict")

    pil_frames = getattr(verdict, "frames", None) or []
    if len(pil_frames) != 4:
        log.error(
            f"[{ctx.alert_id}] gate_aware_capture: verdict.frames has "
            f"{len(pil_frames)} PIL images (expected 4). Alert sent=False."
        )
        ctx.capture_source = "missing"
        raise SkipEvent(f"verdict.frames has {len(pil_frames)} PIL images, expected 4")

    # Authoritative in-memory copy for downstream stages.
    ctx.frames = list(pil_frames)  # defensive copy
    ctx.crop_a = getattr(verdict, "crop_a", None)
    ctx.crop_b = getattr(verdict, "crop_b", None)

    # Disk paths — only populated when GATE_KEEP_DISK_ARTIFACTS=true.
    # The downstream pipeline no longer reads from disk on the hot path;
    # these are kept for postmortem tooling only.
    ctx.frame_paths = list(getattr(verdict, "frame_paths", []) or [])
    ctx.capture_source = "gate"

    log.info(
        f"[{ctx.alert_id}] gate_aware_capture: reused {len(pil_frames)} gate frames "
        f"(verdict.decision={verdict.decision} class={verdict.class_label} "
        f"conf={verdict.confidence:.2f})"
    )

# --- Person path (Phase 6B.139, PLAN §11.60) ---------------------------
#
# Before 6B.139: gate_aware_person_capture was a stub that delegated to
# person_capture_stage (the 6-second-late fresh RTSP pull). That meant
# Qwen analyzed the WRONG moment — the person may have already left by
# the time the fresh frames arrived.
#
# 6B.139 mirrors the vehicle path: read PIL frames + crops from the gate
# verdict, write the 2 selected gate frames to disk for analyze_frames_queued,
# populate ctx.selected_frames + ctx.capture_source = "gate".
#
# Frame selection: person pipeline uses 2 frames for Qwen
# (PERSON_CAPTURE_FRAME_COUNT = 2). We pick the gate's middle two
# (frames[1] and frames[2]) — the bracketing frames around the motion
# event. Vehicle uses all 4; person does not need both pre-event and
# post-event since motion is small-window.
#
# Disk writing: when GATE_KEEP_DISK_ARTIFACTS=true the gate already wrote
# 4 frames and verdict.frame_paths has them. We REUSE those paths. When
# false, verdict.frame_paths is [] and we WRITE the 2 selected PIL frames
# to disk under ctx.output_dir (deterministic names:
# frame_gate_001.jpg, frame_gate_002.jpg). The gate-written frames (if
# any) are still on disk for postmortem — we don't depend on them.


def gate_aware_person_capture(ctx) -> None:
    """Person pipeline gate-aware capture. Mirror of gate_aware_vehicle_capture.

    Phase 6B.139 (§11.60, 2026-08-27): the gate is now the sole producer
    of frames for person events. This function:

    1. Reads 4 PIL.Image from ctx.gate_verdict.frames (always present
       when the gate ran)
    2. Sets ctx.frames (full 4-frame list, parity with vehicle)
    3. Sets ctx.crop_a, ctx.crop_b from the gate's PIL crops
    4. Sets ctx.selected_frames = [frames[1], frames[2]] — the
       bracketing frames around the motion event (2 frames for Qwen,
       matching PERSON_CAPTURE_FRAME_COUNT = 2)
    5. Sets ctx.frame_paths to a 2-element list of disk paths:
       - If verdict.frame_paths has >= 2 entries (GATE_KEEP_DISK_ARTIFACTS=true):
         use verdict.frame_paths[1] and verdict.frame_paths[2]
       - Otherwise: write frames[1] and frames[2] to ctx.output_dir
         as frame_gate_001.jpg and frame_gate_002.jpg
    6. Sets ctx.capture_source = "gate"

    Raises:
        SkipEvent: if ctx.gate_verdict is None or verdict.frames has
            fewer than 4 PIL images. The pipeline catches this and
            returns a sent=False result (matches the 6B.115 contract
            for the vehicle path).
    """
    verdict = getattr(ctx, "gate_verdict", None)
    if verdict is None:
        log.error(
            f"[{ctx.alert_id}] gate_aware_person_capture: NO GATE VERDICT — "
            f"motion gate did not run. Phase 6B.139: no legacy fallback. "
            f"Alert will be sent=False."
        )
        ctx.capture_source = "missing"
        raise SkipEvent("no gate verdict")

    pil_frames = getattr(verdict, "frames", None) or []
    if len(pil_frames) != 4:
        log.error(
            f"[{ctx.alert_id}] gate_aware_person_capture: verdict.frames has "
            f"{len(pil_frames)} PIL images (expected 4). Alert sent=False."
        )
        ctx.capture_source = "missing"
        raise SkipEvent(f"verdict.frames has {len(pil_frames)} PIL images, expected 4")

    # Authoritative in-memory copy (mirror vehicle path).
    ctx.frames = list(pil_frames)  # defensive copy
    ctx.crop_a = getattr(verdict, "crop_a", None)
    ctx.crop_b = getattr(verdict, "crop_b", None)

    # 6B.139: pick the gate's middle two frames — frames[1] (pre-event)
    # and frames[2] (event moment). Qwen gets these 2 paths.
    selected_idx = (1, 2)
    selected_pil = [pil_frames[i] for i in selected_idx]

    # Persist selected_frames as the canonical 2-frame Qwen input.
    # Phase 6B.140 will read selected_frames for best-frame selection.
    ctx.selected_frames = list(selected_pil)

    verdict_paths = list(getattr(verdict, "frame_paths", []) or [])

    if len(verdict_paths) >= 4:
        # GATE_KEEP_DISK_ARTIFACTS=true: gate already wrote the 4 frames.
        # Reuse the paths directly (no extra disk I/O).
        ctx.frame_paths = [verdict_paths[i] for i in selected_idx]
    else:
        # GATE_KEEP_DISK_ARTIFACTS=false (or legacy verdict): the gate
        # only kept PIL in memory. Write the 2 selected frames to disk
        # under ctx.output_dir so analyze_frames_queued has a path to
        # read. Deterministic names avoid collisions if the same alert_id
        # is reprocessed.
        output_dir = getattr(ctx, "output_dir", "") or ""
        if not output_dir:
            log.error(
                f"[{ctx.alert_id}] gate_aware_person_capture: ctx.output_dir "
                f"empty; cannot write selected frames to disk"
            )
            ctx.frame_paths = []
        else:
            os.makedirs(output_dir, exist_ok=True)
            written: list[str] = []
            for i, pil_img in enumerate(selected_pil, start=1):
                p = os.path.join(output_dir, f"frame_gate_{i:03d}.png")
                try:
                    # §11.88 (2026-09-01) — PNG lossless, NOT JPEG q85.
                    pil_img.save(p, format="PNG")
                    written.append(p)
                except Exception as save_err:
                    log.error(
                        f"[{ctx.alert_id}] gate_aware_person_capture: "
                        f"failed to write {p}: {save_err}"
                    )
            ctx.frame_paths = written

    ctx.capture_source = "gate"

    log.info(
        f"[{ctx.alert_id}] gate_aware_person_capture: reused {len(pil_frames)} gate frames, "
        f"selected={selected_idx} → {len(ctx.frame_paths)} disk paths "
        f"(verdict.decision={verdict.decision} class={verdict.class_label} "
        f"conf={verdict.confidence:.2f} verdict_paths={len(verdict_paths)})"
    )
