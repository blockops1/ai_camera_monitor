"""
single_pipeline.py — §11.115.5 single-pipeline orchestrator.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: per-call instances; not thread-safe by itself (the caller
               runs one alert at a time via _BoundedWebhookExecutor).

INPUTS:
  - fn `run(...)` — all 12 args are injected dependencies + alert context.
    See signature below.

OUTPUTS:
  - PipelineResult dataclass:
      alert_id:              str
      classify_label:        ClassLabel | None (None if dropped at RTSP gate)
      classify_fallback_used: bool
      skipped_reason:        str | None ("no_rtsp" | None for now)
      sent_telegram:         bool
      log_only:              bool

PUBLIC API:
  - PipelineResult           dataclass
  - run(alert_id, camera_name, captured_at, frame_paths, event_type,
        has_rtsp_fn, frame_diff_fn, qwen_fn, matchers, telegram_fn, log_fn)
        -> PipelineResult

DOES NOT DO:
  - Receive webhooks (that's listener.listener.receive_alert)
  - Run the RTSP stream (that's infra.persistent_rtsp)
  - Build prompts (that's classify_prompt / person_prompt / etc.)
  - Validate Qwen responses (that's classify_validator / vision_response)
  - Match persons/animals/vehicles to enrolled identities
    (that's infra.{person,animal,vehicle}_matcher — invoked via `matchers` arg)
  - Send Telegram messages directly (delegates to telegram_fn)
  - Persist audit logs (delegates to log_fn)

CALLED BY:
  - listener.listener.receive_alert — replaces _process_alert + dispatch.

RELATED:
  - PLAN.md §11.115 — design rationale.
  - infra.two_call_cascade — shared classify + class-specific cascade.
  - infra.{person,animal,vehicle,classify}_prompt — prompt builders.
  - infra.classify_schema — ClassLabel enum.
  - infra.cameras.has_rtsp — RTSP-presence filter.
  - infra.frame_diff — pairwise diff (frame_diff_fn arg).
  - infra.vision_analyzer.analyze_frames_queued — production qwen_fn.

Design notes:
  - All side effects (Qwen, Telegram, logs) are passed in as callables.
    This makes the orchestrator testable without a live server.
  - Two-crop invariant: crop_a + crop_b are produced ONCE by frame_diff_fn
    and passed by reference to every subsequent step (Qwen call 1,
    Qwen call 2, matcher, Telegram). No new crops ever created.
  - `other` class → log only, NO Telegram. This is maintainer's 2026-09-02 PM
    directive ("we're just gonna log for now. No telegram.").
  - Telegram always sends BOTH crops (maintainer: "I always want both crops
    sent in the telegram body"). Crop selection for face recognition
    is independent of Telegram body.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from infra.classify_schema import ClassLabel
from infra.two_call_cascade import cascade_call1, cascade_call2
from listener.pipeline_filters import (
    ANIMAL_COOLDOWN_SECONDS,
    PERSON_COOLDOWN_SECONDS,
    VEHICLE_CAMERAS_ALLOWLIST,
    PipelineCooldown,
    is_vehicle_allowed,
)


# ============================================================================
# PipelineResult
# ============================================================================
@dataclass(frozen=True)
class PipelineResult:
    """Observable outcome of a single-pipeline run.

    All fields are populated even when the pipeline skips early
    (e.g. no_rtsp) — callers can log result.skipped_reason.
    """

    alert_id: str
    classify_label: ClassLabel | None
    classify_fallback_used: bool
    skipped_reason: str | None
    sent_telegram: bool
    log_only: bool


# Type aliases for injected dependencies.
HasRtspFn = Callable[[str], bool]
FrameDiffFn = Callable[[list[str]], tuple[str, str]]
QwenFn = Callable[..., dict[str, Any]]
TelegramFn = Callable[..., bool | None]
LogFn = Callable[..., None]


# ============================================================================
# Public API
# ============================================================================
def run(
    alert_id: str,
    camera_name: str,
    camera_code: str,
    captured_at: str,
    frame_paths: list[str],
    event_type: str,
    *,
    has_rtsp_fn: HasRtspFn,
    frame_diff_fn: FrameDiffFn,
    qwen_fn: QwenFn,
    matchers: Any,
    telegram_fn: TelegramFn,
    log_fn: LogFn,
    cooldown: PipelineCooldown,
) -> PipelineResult:
    """Execute the full §11.115 single pipeline for one alert.

    Args:
        alert_id:     UUID for this alert (logging + audit).
        camera_name:  friendly camera name (e.g. "Front Porch").
        camera_code:  CAM{N} code resolved via infra.cameras.by_code().
                      Used by the vehicle camera allowlist filter.
        captured_at:  ISO-8601 timestamp of the captured frames.
        frame_paths:  list of original frame paths (3-4 typically).
        event_type:   event string from the webhook ("motion", "person", ...).
        has_rtsp_fn:  callable(camera_name) -> bool. RTSP-presence filter.
        frame_diff_fn: callable(frame_paths) -> (crop_a, crop_b). The
                       pairwise-diff produces the two crops.
        qwen_fn:      callable(frame_paths, camera_name, event_hint) -> dict.
                       Production wires infra.vision_analyzer.analyze_frames_queued.
        matchers:     object exposing .vehicle / .person / .animal callables
                       (each: (classify, call2_response, crop_a, crop_b) -> dict).
        telegram_fn:  callable(crop_a, crop_b, classify, call2_response).
        log_fn:       callable(message, **context).
        cooldown:     PipelineCooldown instance (§11.115.13). Pre-cascade
                       throttle for person + animal (property-wide, 15 min),
                       and post-matcher hit recording. Single instance is
                       shared across all events; pass the same object
                       every call.

    Returns:
        PipelineResult. classify_label is None iff the alert was dropped
        at the RTSP gate. sent_telegram is True iff a Telegram message was
        sent (vehicle/person/animal class with successful call 2).
        log_only is True iff the alert was processed but did not warrant
        a Telegram (other class, cooldown suppressed, vehicle camera
        not in allowlist, or Qwen-call-1 fallback).
    """
    # ----- Stage 1: RTSP-presence filter -----
    if not has_rtsp_fn(camera_name):
        log_fn(
            "single_pipeline: skipped (no RTSP)",
            alert_id=alert_id,
            camera=camera_name,
            event=event_type,
        )
        return PipelineResult(
            alert_id=alert_id,
            classify_label=None,
            classify_fallback_used=False,
            skipped_reason="no_rtsp",
            sent_telegram=False,
            log_only=False,
        )

    # ----- Stage 2: pairwise diff → crop_a + crop_b -----
    crop_a, crop_b = frame_diff_fn(frame_paths)

    # ----- Stage 3: Qwen call 1 (shared classify) -----
    classify = cascade_call1(
        frame_paths=[crop_a, crop_b],
        camera_name=camera_name,
        captured_at=captured_at,
        qwen_fn=qwen_fn,
    )

    # ----- Stage 4: pre-cascade filters (§11.115.13) -----
    # Two filters run between call 1 and call 2:
    #   1. PipelineCooldown — property-wide per-class throttle for
    #      person + animal (15 min window).
    #   2. Vehicle camera allowlist — only OFS + OBS get vehicle
    #      matching; other cameras drop vehicle events silently.
    # Both are log-and-return so the listener log records why each
    # suppressed event was dropped.
    class_label_value = classify.label.value
    should_suppress, cooldown_reason = cooldown.should_suppress(class_label_value)
    if should_suppress:
        log_fn(
            "single_pipeline: cooldown suppressed",
            alert_id=alert_id,
            camera=camera_name,
            event=event_type,
            class_label=class_label_value,
            reason=cooldown_reason,
        )
        return PipelineResult(
            alert_id=alert_id,
            classify_label=classify.label,
            classify_fallback_used=classify.fallback_used,
            skipped_reason=f"cooldown:{cooldown_reason}",
            sent_telegram=False,
            log_only=True,
        )

    if class_label_value == ClassLabel.VEHICLE.value and not is_vehicle_allowed(
        camera_name
    ):
        log_fn(
            "single_pipeline: vehicle_camera_drop (camera not in allowlist)",
            alert_id=alert_id,
            camera=camera_name,
            camera_code=camera_code,
            allowlist=sorted(VEHICLE_CAMERAS_ALLOWLIST),
            event=event_type,
        )
        return PipelineResult(
            alert_id=alert_id,
            classify_label=classify.label,
            classify_fallback_used=classify.fallback_used,
            skipped_reason="vehicle_camera_not_allowed",
            sent_telegram=False,
            log_only=True,
        )

    # ----- Stage 5: Qwen call 2 (class-specific cascade) -----
    cascade = cascade_call2(
        classify=classify,
        frame_paths=[crop_a, crop_b],
        camera_name=camera_name,
        captured_at=captured_at,
        qwen_fn=qwen_fn,
        call2_prompts={
            ClassLabel.VEHICLE: lambda cam, ts: __import__(
                "infra.vehicle_prompt", fromlist=["build_vehicle_prompt"]
            ).build_vehicle_prompt(cam, ts),
            ClassLabel.PERSON: lambda cam, ts: __import__(
                "infra.person_prompt", fromlist=["build_person_prompt"]
            ).build_person_prompt(cam, ts),
            ClassLabel.ANIMAL: lambda cam, ts: __import__(
                "infra.animal_prompt", fromlist=["build_animal_prompt"]
            ).build_animal_prompt(cam, ts),
        },
    )

    # ----- Stage 6: post-cascade (log-only for other / missing factory) -----
    if cascade.call2_was_skipped:
        log_fn(
            "single_pipeline: log only (other class or call 2 skipped)",
            alert_id=alert_id,
            camera=camera_name,
            classify_label=cascade.classify.label.value,
            classify_fallback=cascade.classify.fallback_used,
            reasoning=cascade.classify.reasoning,
        )
        return PipelineResult(
            alert_id=alert_id,
            classify_label=cascade.classify.label,
            classify_fallback_used=cascade.classify.fallback_used,
            skipped_reason=None,
            sent_telegram=False,
            log_only=True,
        )

    # ----- Stage 7: class-specific matcher -----
    matcher_fn = _matcher_for(cascade.classify.label, matchers)
    if matcher_fn is None:
        # Should not happen if cascade.call2_was_skipped is False,
        # but be defensive.
        log_fn(
            "single_pipeline: log only (no matcher for class)",
            alert_id=alert_id,
            camera=camera_name,
            classify_label=cascade.classify.label.value,
        )
        return PipelineResult(
            alert_id=alert_id,
            classify_label=cascade.classify.label,
            classify_fallback_used=cascade.classify.fallback_used,
            skipped_reason=None,
            sent_telegram=False,
            log_only=True,
        )

    _match_result = matcher_fn(
        cascade.classify,
        cascade.call2_response,
        crop_a,
        crop_b,
        alert_id=alert_id,
    )

    # ----- Stage 8: cooldown record_hit (§11.115.13) -----
    matcher_hit = bool(_match_result.get("matched", False))
    if cooldown.record_hit(class_label_value, matcher_hit=matcher_hit):
        log_fn(
            "single_pipeline: cooldown hit recorded",
            alert_id=alert_id,
            camera=camera_name,
            class_label=class_label_value,
            matcher_hit=matcher_hit,
            window_seconds=(
                PERSON_COOLDOWN_SECONDS
                if class_label_value == ClassLabel.PERSON.value
                else ANIMAL_COOLDOWN_SECONDS
            ),
        )

    # ----- Stage 9: Telegram (BOTH crops) -----
    telegram_fn(
        crop_a=crop_a,
        crop_b=crop_b,
        classify_label=cascade.classify.label,
        call2_response=cascade.call2_response,
        alert_id=alert_id,
    )

    return PipelineResult(
        alert_id=alert_id,
        classify_label=cascade.classify.label,
        classify_fallback_used=cascade.classify.fallback_used,
        skipped_reason=None,
        sent_telegram=True,
        log_only=False,
    )


# ============================================================================
# Internal helpers
# ============================================================================
def _matcher_for(label: ClassLabel, matchers: Any) -> Callable | None:
    """Return the matcher callable for the given class, or None."""
    if label is ClassLabel.VEHICLE:
        return getattr(matchers, "vehicle", None)
    if label is ClassLabel.PERSON:
        return getattr(matchers, "person", None)
    if label is ClassLabel.ANIMAL:
        return getattr(matchers, "animal", None)
    return None
