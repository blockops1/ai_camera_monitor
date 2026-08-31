"""The orchestrator.

Runs the full alert pipeline:

    1. motion detection on captured frames
    2. crop extraction (top-N from motion detector)
    3. vehicle identification (vision model on crops)
    4. signature extraction (pure)
    5. matching against known_vehicles
    6. telegram body construction (motion + match OR motion + no-match)

Pure orchestration. Each step is delegated to its domain module.
No I/O except the calls to detect_motion and call_vision (which are
adapters). The orchestrator returns a structured PipelineResult
that the caller (listener) can use to send Telegrams.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from known_vehicles.store import KnownVehicleStore

# Phase.105 (2026-08-20) — match step delegates to the legacy
# 15-dim scorer via the bridge adapter. Modular vehicle_matcher.match_signature
# is the 4-dim path, materially worse on color normalization, type-group flex,
# and negative-mismatch penalties (see scripts/probe_matcher_comparison.py
# from Phase.103). The adapter wraps infra.vehicle_matcher.match_vehicle_scored
# and presents its output in the MatchVerdict | NoMatch shape the telegram
# formatters already consume.
from pipeline._legacy_match_adapter import (
    match_with_legacy,
    score_top_n_with_legacy,
)
from telegram_formatter.match_telegram import (
    MatchTelegramInput,
    build_match_telegram_body,
)
from telegram_formatter.motion_telegram import (
    MotionTelegramInput,
    build_motion_telegram_body,
)
from telegram_formatter.no_match_telegram import (
    NoMatchTelegramInput,
    build_no_match_telegram_body,
)
from vehicle_identifier.identifier import (
    IdentifierResult,
    identify_from_crops,
)
from vehicle_identifier.vision_client import VisionError, VisionResult
from vehicle_matcher import MatchVerdict, NoMatch  # match-fix-2026-08-18
from vehicle_position.crop_extractor import (
    crop_paths_for_identifier,
    primary_avg_area,
    primary_trajectory,
)
from vehicle_position.motion_detector import (
    PositionResult,
)
from vehicle_position.motion_detector import (
    build_motion_result_from_gate as build_motion_result_from_gate_refactor,
)

# --- Configuration ---------------------------------------------------------


@dataclass(frozen=True)
class PipelineConfig:
    """All tunables for the orchestrator.

    Attributes:
        match_threshold:  Confidence threshold for accepting a match.
        gap_threshold:    Gap threshold for rejecting ambiguous matches.
        top_n_crops:      Max crops to feed to the identifier.
        top_n_no_match:   Max candidates to show in the no-match Telegram.
        vision_api_url:   Optional override for the vision API URL.
                          None means use the identifier's default.
        vision_timeout:   Optional override for the vision client timeout.
        identifier_top_n: Override for the identifier's top-N crop cap.
                          None means use the identifier's default.
    """
    match_threshold: float = 0.6
    gap_threshold: float = 0.15
    top_n_crops: int = 3
    top_n_no_match: int = 3
    vision_api_url: str | None = None
    vision_timeout: float | None = None
    identifier_top_n: int | None = None


# --- Result -----------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult:
    """What the orchestrator returns.

    Attributes:
        motion_telegram_body:  The first Telegram body. Always present.
        second_telegram_body:  The second Telegram body (match or no-match).
                               None if we couldn't even produce a match verdict.
        position:              The PositionResult from the motion detector.
        identifier_result:     The IdentifierResult from the identifier.
        match_verdict:         MatchVerdict | NoMatch | None.
        elapsed_ms:            Total wall-clock time.
        alert_id:              Echo of the alert id (for debugging).
    """
    motion_telegram_body: str
    second_telegram_body: str | None
    position: PositionResult
    identifier_result: IdentifierResult
    match_verdict: Any  # MatchVerdict | NoMatch | None — using Any to avoid
                        # the frozen dataclass needing a Union type field.
    elapsed_ms: float
    alert_id: str


# --- Orchestrator -----------------------------------------------------------


def run_pipeline(
    output_dir: str | Path,
    alert_id: str,
    camera_name: str,
    captured_at_iso: str,
    known_vehicles: KnownVehicleStore,
    bbox_a: tuple[int, int, int, int] | None = None,
    bbox_b: tuple[int, int, int, int] | None = None,
    crop_paths: list[str] | None = None,
    crop_a: object | None = None,    # PIL.Image | None — gate's pre-cropped bbox_a
    crop_b: object | None = None,    # PIL.Image | None — gate's pre-cropped bbox_b
    frames: list | None = None,      # list[PIL.Image] — gate's 4 frames (in-memory)
    frame_paths: list[str | Path] | None = None,  # legacy: disk paths (backward-compat)
    config: PipelineConfig | None = None,
) -> PipelineResult:
    """Run the full alert pipeline.

    Steps:
      1. Motion detection (gate-driven, Phase.115).
      2. Vehicle identification (vision model on the gate's crops).
      3. Signature extraction (inside the identifier).
      4. Match against known_vehicles.
      5. Build Telegram bodies.

    Args:
        frame_paths: 4 gate frames @ native resolution. Kept for backward
            compatibility (when caller has paths but not PIL images). If
            `frames` is provided, takes precedence; otherwise the impl
            derives frame dims from frames[2] internally.
        output_dir: where the gate saved crops.
        alert_id: namespace for output files and the result.
        camera_name: human-readable camera name for the Telegram.
        captured_at_iso: ISO-8601 timestamp string.
        known_vehicles: the in-memory store.
        bbox_a: gate's diff(frame_2, frame_3) bbox in native coords, or None.
        bbox_b: gate's diff(frame_3, frame_4) bbox in native coords, or None.
        crop_paths: gate's crop paths (crop_a, crop_b) for vision input.
        crop_a: PIL.Image of bbox_a crop (gate's pre-cropped).
        crop_b: PIL.Image of bbox_b crop (gate's pre-cropped).
        frames: list of 4 PIL.Image frames from the gate verdict (in-memory).
        config: optional PipelineConfig (defaults applied if None).

    Returns:
        PipelineResult with Telegram bodies and structured results.

    Note: even if every step fails, the orchestrator returns a
    PipelineResult. The motion_telegram_body is always populated.
    """
    import time
    t0 = time.perf_counter()

    if config is None:
        config = PipelineConfig()

    # ------------------------------------------------------------------
    # Step 1: motion detection (gate-driven, Phase.115)
    # ------------------------------------------------------------------
    # Phase.115 (§11.46.6): prefer in-memory frames if provided,
    # else fall back to paths (legacy orchestrator callers may still
    # pass paths). crop_paths is optional (postmortem convenience).
    if crop_paths is None:
        crop_paths = []
    position = build_motion_result_from_gate_refactor(
        frames=list(frames) if frames is not None else [],
        crop_a=crop_a,
        crop_b=crop_b,
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        alert_id=alert_id,
        crop_paths=list(crop_paths),
    )

    crop_paths_for_vision = crop_paths_for_identifier(position, top_n=config.top_n_crops)
    trajectory = primary_trajectory(position)
    area = primary_avg_area(position)

    # ------------------------------------------------------------------
    # Step 2: identification
    # ------------------------------------------------------------------
    identifier_kwargs: dict[str, Any] = {
        "crop_paths": crop_paths_for_vision,
        "camera_name": camera_name,
        "captured_at": captured_at_iso,
    }
    if config.vision_api_url is not None:
        identifier_kwargs["api_url"] = config.vision_api_url
    if config.vision_timeout is not None:
        identifier_kwargs["timeout_seconds"] = config.vision_timeout
    if config.identifier_top_n is not None:
        identifier_kwargs["top_n_crops"] = config.identifier_top_n

    # Phase.144 (§11.66): identify_from_crops now accepts a
    # `pairwise_diff_path` kwarg. Callers (e.g. live listener) build
    # identifier_kwargs directly and can include it; when they don't,
    # the identifier falls back to streak crops only (back-compat).

    identifier_result = identify_from_crops(**identifier_kwargs)

    # ------------------------------------------------------------------
    # Step 3: matching
    # ------------------------------------------------------------------
    # Phase.105 (2026-08-20): use the legacy 15-dim scorer via the
    # adapter instead of vehicle_matcher.match_signature (4-dim). The
    # adapter signature is (signature_dict, known_vehicles_list); the
    # ScoringSpec is read internally by the adapter from
    # infra.matcher_spec.load_spec().
    signature = identifier_result.signature
    known = known_vehicles.all()

    if not signature:
        # Empty signature → can't match. Use NoMatch.
        match_verdict: Any = NoMatch(
            reason="empty_signature",
            top_candidates=[],
        )
    else:
        match_verdict = match_with_legacy(
            signature=signature,
            known_vehicles=known,
            confidence_threshold=config.match_threshold,
            gap_threshold=config.gap_threshold,
        )

    # ------------------------------------------------------------------
    # Step 4: Telegram body construction
    # ------------------------------------------------------------------
    vision_dict = _vision_result_to_dict(identifier_result)

    motion_input = MotionTelegramInput(
        camera_name=camera_name,
        captured_at_iso=captured_at_iso,
        trajectory=trajectory,
        avg_area=area,
        vision_result=vision_dict,
        crop_paths=list(crop_paths_for_vision),
        alert_id=alert_id,
    )
    motion_body = build_motion_telegram_body(motion_input)

    second_body: str | None = None
    if isinstance(match_verdict, MatchVerdict):
        match_input = MatchTelegramInput(
            camera_name=camera_name,
            captured_at_iso=captured_at_iso,
            verdict=match_verdict,
            match_threshold=config.match_threshold,
            gap_threshold=config.gap_threshold,
            alert_id=alert_id,
        )
        second_body = build_match_telegram_body(match_input)
    elif isinstance(match_verdict, NoMatch):
        # Get top-N candidates with breakdowns from score_top_n_with_legacy.
        # Phase.105: legacy 15-dim scorer for parity with the match step.
        top_n = score_top_n_with_legacy(
            signature=signature,
            known_vehicles=known,
            n=config.top_n_no_match,
        )
        no_match_input = NoMatchTelegramInput(
            camera_name=camera_name,
            captured_at_iso=captured_at_iso,
            no_match=match_verdict,
            top_n_breakdowns=top_n,
            match_threshold=config.match_threshold,
            gap_threshold=config.gap_threshold,
            alert_id=alert_id,
        )
        second_body = build_no_match_telegram_body(no_match_input)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return PipelineResult(
        motion_telegram_body=motion_body,
        second_telegram_body=second_body,
        position=position,
        identifier_result=identifier_result,
        match_verdict=match_verdict,
        elapsed_ms=elapsed_ms,
        alert_id=alert_id,
    )


def _vision_result_to_dict(identifier_result: IdentifierResult) -> dict[str, Any] | None:
    """Convert an IdentifierResult's vision_result to a plain dict.

    The vision_result is either a VisionResult (with .content) or a
    VisionError (with .kind/message). Both have to_dict() / a sensible
    dict form. We canonicalize here.
    """
    vr = identifier_result.vision_result
    if vr is None:
        return None
    if isinstance(vr, VisionResult):
        return vr.to_dict()
    if isinstance(vr, VisionError):
        return {"error": {"kind": vr.kind, "message": vr.message}}
    return None
