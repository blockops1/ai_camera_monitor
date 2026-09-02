"""
two_call_cascade.py — §11.115.3 shared classify → class-specific cascade.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (pure functions; qwen_fn injected per-call)

INPUTS:
  - fn `run(...)` accepts:
      frame_paths:    [crop_a, crop_b] — exactly 2 paths
      camera_name:    str — context only
      captured_at:    str — ISO timestamp
      qwen_fn:        callable matching vision_analyzer.analyze_frames_queued
                      signature: (frame_paths, camera_name, ...) -> dict
      call2_prompts:  dict[ClassLabel, Callable[[camera, captured_at], str]]
                      maps classified class → factory that renders the
                      call-2 prompt.

OUTPUTS:
  - CascadeResult dataclass:
      classify:         ClassifyResult (validated)
      call2_response:   dict | None  — raw Qwen call 2 response
      call2_was_skipped: bool         — True for `other` or missing factory
      should_send_telegram: bool     — True iff call 2 ran successfully
      should_log:       bool         — True iff call 2 was skipped

PUBLIC API:
  - CascadeResult                dataclass
  - run(...)                     execute the cascade

DOES NOT DO:
  - Build prompts — caller passes a call2_prompts factory dict
  - Decide schema content — infra.classify_schema + class-specific modules
  - Match persons/animals/vehicles to enrolled identities
  - Send Telegram messages — caller reads should_send_telegram flag

CALLED BY:
  - listener.single_pipeline (Phase §11.115) — orchestrator

RELATED:
  - PLAN.md §11.115 — design rationale
  - infra.classify_prompt — call 1 prompt builder
  - infra.classify_validator — call 1 response validator
  - infra.person_prompt / animal_prompt / vehicle_prompt — call 2 builders

Design notes:
  - qwen_fn is injected so this module is testable without a real
    Qwen server. Production wires `infra.vision_analyzer.analyze_frames_queued`.
  - Call 1 always runs. Call 2 runs only if:
      a) classified class is in (VEHICLE, PERSON, ANIMAL) AND
      b) call2_prompts has a factory for that class.
    Otherwise call 2 is skipped, should_send_telegram=False, should_log=True.
  - Both calls receive the SAME [crop_a, crop_b]. Two-crop invariant.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from infra.classify_prompt import build_classify_prompt
from infra.classify_schema import ClassLabel
from infra.classify_validator import ClassifyResult, validate_classify_response


# ============================================================================
# Result dataclass
# ============================================================================
@dataclass(frozen=True)
class CascadeResult:
    """Outcome of a two-call cascade."""

    classify: ClassifyResult
    call2_response: dict | None
    call2_was_skipped: bool
    should_send_telegram: bool
    should_log: bool


# Type alias for the call-2 prompt factory.
Call2PromptFactory = Callable[[str, str], str]


# ============================================================================
# Internal helpers
# ============================================================================
def _default_qwen(
    frame_paths: list[str], camera_name: str, event_hint: str
) -> dict[str, Any]:
    """Default qwen_fn: just returns an error sentinel.

    The cascade requires a qwen_fn be injected. If run() is called
    without one, this raises immediately — better to fail loudly than
    silently call Qwen with default args.
    """
    raise RuntimeError(
        "two_call_cascade.run() requires a qwen_fn injection. "
        "Production wires infra.vision_analyzer.analyze_frames_queued."
    )


# ============================================================================
# Public API
# ============================================================================
def cascade_call1(
    frame_paths: list[str],
    camera_name: str,
    captured_at: str,
    qwen_fn: Callable[..., dict[str, Any]],
) -> ClassifyResult:
    """Run only Qwen call 1 (shared classify).

    Returns:
        ClassifyResult. classify.label is the validated class
        ("person", "animal", "vehicle", "other").
    """
    classify_prompt = build_classify_prompt(camera_name, captured_at)
    call1_response = qwen_fn(
        frame_paths=frame_paths,
        camera_name=camera_name,
        event_hint=classify_prompt,
    )
    raw_text = call1_response.get("raw", "")
    return validate_classify_response(raw_text)


def cascade_call2(
    classify: ClassifyResult,
    frame_paths: list[str],
    camera_name: str,
    captured_at: str,
    qwen_fn: Callable[..., dict[str, Any]],
    call2_prompts: dict[ClassLabel, Call2PromptFactory],
) -> CascadeResult:
    """Run only Qwen call 2 (class-specific) given a classify result.

    Returns:
        CascadeResult with call2_response populated. If classify.label
        is `other` or has no factory, call2_was_skipped=True and
        call2_response=None.
    """
    factory = call2_prompts.get(classify.label)
    if factory is None:
        # Either `other` class, OR missing factory → log only
        return CascadeResult(
            classify=classify,
            call2_response=None,
            call2_was_skipped=True,
            should_send_telegram=False,
            should_log=True,
        )

    call2_prompt = factory(camera_name, captured_at)
    call2_response = qwen_fn(
        frame_paths=frame_paths,
        camera_name=camera_name,
        event_hint=call2_prompt,
    )

    return CascadeResult(
        classify=classify,
        call2_response=call2_response,
        call2_was_skipped=False,
        should_send_telegram=True,
        should_log=False,
    )


def run(
    frame_paths: list[str],
    camera_name: str,
    captured_at: str,
    qwen_fn: Callable[..., dict[str, Any]],
    call2_prompts: dict[ClassLabel, Call2PromptFactory],
) -> CascadeResult:
    """Execute the shared classify → class-specific cascade.

    Thin wrapper around cascade_call1 + cascade_call2. Used by callers
    that don't need to interleave filters between the two calls. The
    `single_pipeline.run` (§11.115.13) calls cascade_call1 + filters +
    cascade_call2 directly so it can drop events between calls.

    Args:
        frame_paths:    [crop_a, crop_b] — exactly 2 paths (two-crop invariant).
        camera_name:    friendly camera name for prompt context.
        captured_at:    ISO-8601 timestamp for prompt context.
        qwen_fn:        callable matching analyze_frames_queued signature:
                            (frame_paths, camera_name, event_hint=None, ...)
                            -> dict (with 'raw' key or matching schema)
        call2_prompts:  factory map: ClassLabel → (camera, captured_at) → str.

    Returns:
        CascadeResult. call2_response is None iff call 2 was skipped.
        should_send_telegram is True iff call 2 ran successfully (vehicle/
        person/animal with a registered factory).
        should_log is True iff call 2 was skipped (other class or missing
        factory).
    """
    classify = cascade_call1(frame_paths, camera_name, captured_at, qwen_fn)
    return cascade_call2(
        classify=classify,
        frame_paths=frame_paths,
        camera_name=camera_name,
        captured_at=captured_at,
        qwen_fn=qwen_fn,
        call2_prompts=call2_prompts,
    )
