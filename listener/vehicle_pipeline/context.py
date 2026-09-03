"""
context — Per-alert carrier object (AlertContext) shared across all 6 stages.

STATUS: stable
THREAD SAFETY: single-threaded (one AlertContext per alert; never shared across threads)

INPUTS:
    - Constructed by listener._process_alert; fields set before process_alert() runs.

OUTPUTS:
    - AlertContext: dataclass instance; mutated by each stage in sequence.

PUBLIC API:
    AlertContext
        Per-alert carrier. 14 inputs from listener (never mutated after construction);
        10 stage outputs (capture, identify, match, select_frame, alert, telemetry).
        Phase.168: camera_code populated by listener once at construction —
        gatekeeper-membership checks compare camera_code against gatekeeper_cameras,
        NOT camera_name. Pre-fix silently failed because camera_name is friendly
        (from webhook) and gatekeeper_cameras contains CAM{N} codes.

DOES NOT DO:
    - Decide which stage runs next — process_alert() in __init__.py owns that.
    - Mutate the input fields (alert_id, camera_name, rtsp_url, etc.) — they are
      listener-owned and immutable.
    - Persist to disk — alert_history.append_alert() does that from emit.

CALLED BY:
    - listener.vehicle_pipeline.process_alert: every stage signature takes ctx
    - listener.listener._process_alert: constructs AlertContext and calls process_alert

CALLS INTO:
    - (nothing — pure dataclass, no imports beyond stdlib)

RELATED:
    - process_alert in __init__.py — the driver that flows ctx through stages
    - §11.105b in PLAN.md — design rationale for the carrier-object shape
    - §11.168 — camera_code boundary translation at construction
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class AlertContext:
    """Per-alert carrier object. Set by the listener before calling
    process_alert; mutated by each stage in sequence.

    Field groups:
        Inputs (set by the listener, never mutated):
            alert_id, camera_name, timestamp, event_type, rtsp_url,
            output_dir, is_vehicle_event, known_vehicles, bot_token,
            chat_id, api_url, gatekeeper_cameras
        Stage 1 outputs (capture):
            frame_paths
        Stage 2 outputs (identify):
            id_result, vision_result, vision_error, face_visibility,
            motion_result
        Stage 3 outputs (match):
            match_verdict, score_top_n
        Stage 4 outputs (select_best_frame):
            best_frame_path
        Stage 5 outputs (generate_alert):
            alert
        Telemetry (counters updated as a side effect):
            shadow_disagreements, shadow_agreements
    """

    # ---- Inputs (set by the listener, never mutated) -----------------------
    alert_id: str
    camera_name: str
    timestamp: str
    event_type: str
    rtsp_url: str
    output_dir: str
    is_vehicle_event: bool
    known_vehicles: list[dict]
    bot_token: str
    chat_id: str
    api_url: str
    gatekeeper_cameras: frozenset[str]
    # camera_code: canonical CAM{N} code for camera_name, populated once
    # by the listener driver (listener.py:1954). All gatekeeper membership
    # tests in this module compare ctx.camera_code against
    # ctx.gatekeeper_cameras (which is code-keyed), NOT camera_name
    # against gatekeeper_cameras — that comparison silently failed in
    # production because camera_name is the friendly name from the
    # webhook and gatekeeper_cameras contains CAM{N} codes (Phase.167
    # §13.4/§13.5). Phase.168 (2026-08-31): boundary translation done
    # once at construction, not at every membership check.
    camera_code: str = ""

    # NOTE: Phase.168 deliberately does NOT auto-derive camera_code
    # in __post_init__. The listener driver is the only legitimate
    # construction site in production, and it always passes both
    # camera_name AND camera_code explicitly. Auto-derivation would
    # silently mask a class of test fixture bugs where a test mutates
    # ctx.camera_name after construction and forgets to refresh
    # ctx.camera_code — see test_vehicle_event_pipeline_6B105b.
    # non_gatekeeper_ctx for the canonical example.

    # ---- Phase.108a (§11.38.5): gate-aware capture inputs ---------------
    # gate_verdict: populated by listener.py when MOTION_GATE_ENABLED=1 and
    #     the gate ran. None means the gate did not run (legacy path).
    #     Captured by gate_aware_capture in stage 1.
    # capture_source: observability — "gate" (4 frames reused) or "rtsp"
    #     (legacy 6-frame capture). Default "rtsp" so legacy callers/tests
    #     don't need to set it.
    # (Phase.115: legacy_capture_avoided was removed — there's no
    # legacy capture path anymore. capture_source is the only observability
    # field for capture routing.)
    gate_verdict: Any = None  # GateVerdict | None — late import to avoid cycle
    capture_source: str = "rtsp"

    # ---- Stage 1 outputs (capture) ---------------------------------------
    # Phase.115: in-memory frames + crops from the gate (authoritative).
    frames: list = field(default_factory=list)        # list[PIL.Image.Image]
    crop_a: Any = None                                # PIL.Image.Image | None
    crop_b: Any = None                                # PIL.Image.Image | None
    # Disk paths — populated only when GATE_KEEP_DISK_ARTIFACTS=true.
    frame_paths: list[str] = field(default_factory=list)

    # ---- Stage 2 outputs (identify) --------------------------------------
    id_result: Any = None
    vision_result: Any = None
    vision_error: Any = None
    face_visibility: bool = False
    motion_result: Any = None
    # Phase.144 (§11.66): path to the gate's pairwise differential
    # image (abs(frame_3 − frame_4) JPEG with bbox overlays). Qwen
    # uses this to disambiguate the moving subject from stationary
    # vehicles in the same frame (e.g. tractor moving in front of
    # parked Sequoia). None when the gate didn't run or didn't write
    # the diff (e.g. GATE_KEEP_DISK_ARTIFACTS=false).
    pairwise_diff_path: str | None = None

    # ---- Stage 3 outputs (match) -----------------------------------------
    match_verdict: Any = None  # MatchVerdict | NoMatch | None
    score_top_n: list = field(default_factory=list)
    # Phase.121 (2026-08-22): list of MatchTelegramInput the match
    # loop built (one per matched vehicle). Used by emit_result_stage
    # to replace ctx.alert with the match body before append_alert().
    match_alerts: list = field(default_factory=list)

    # ---- Stage 4 outputs (select_best_frame) -----------------------------
    best_frame_path: str = ""

    # ---- Stage 5 outputs (generate_alert) --------------------------------
    alert: dict = field(default_factory=dict)

    # ---- Telemetry --------------------------------------------------------
    shadow_disagreements: int = 0
    shadow_agreements: int = 0

    # ---- Class-level constants --------------------------------------------
    GATEKEEPER_VEHICLE_EVENT: ClassVar[bool] = True
