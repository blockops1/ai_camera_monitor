"""
classify_validator.py — §11.115.2 validate Qwen call 1 response.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; pure functions)

INPUTS:
  - fn `validate_classify_response(raw_text)`
        raw_text: the string Qwen returned (may include ```json fences,
        may be malformed, may be empty).

OUTPUTS:
  - ClassifyResult dataclass with 4 fields:
        label          ClassLabel (never None — falls back to OTHER)
        confidence     float, clamped to [0.0, 1.0]
        reasoning      str (may be empty)
        fallback_used  bool — True if we had to fall back to OTHER

PUBLIC API:
  - ClassifyResult                    dataclass
  - validate_classify_response(text)  -> ClassifyResult

DOES NOT DO:
  - Does NOT call Qwen. (See infra.classify_prompt for the prompt.)
  - Does NOT retry on failure — caller's responsibility to retry the Qwen
    call before invoking this if it wants to handle transport errors.
  - Does NOT log. (Caller should log when fallback_used is True.)

CALLED BY:
  - listener.single_pipeline.run() — after Qwen call 1 returns.

RELATED:
  - PLAN.md §11.115 — design rationale (shared classify first, then diverge).
  - infra.classify_schema — ClassLabel enum + VALID_CLASSES set.

Design notes:
  - On ANY failure (no JSON, missing class, unknown class), fall back to
    OTHER with fallback_used=True. Caller can decide whether to log,
    retry, or send anyway.
  - Strip ```json ... ``` fences before parsing. Qwen almost always wraps
    responses in markdown code fences.
  - Confidence outside [0.0, 1.0] is CLAMPED, not fallback. The class is
    the high-stakes decision; confidence is metadata.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from infra.classify_schema import VALID_CLASSES, ClassLabel


# ============================================================================
# Result dataclass
# ============================================================================
@dataclass(frozen=True)
class ClassifyResult:
    """Parsed + validated classify response.

    Never None fields. On parse failure, label=OTHER, confidence=0.0,
    reasoning="", fallback_used=True.
    """

    label: ClassLabel
    confidence: float
    reasoning: str
    fallback_used: bool


# ============================================================================
# Internal helpers
# ============================================================================
_JSON_FENCE_RE = re.compile(
    r"^```(?:json)?\s*\n?(.*?)\n?\s*```$",
    re.DOTALL | re.IGNORECASE,
)


def _strip_code_fences(text: str) -> str:
    """Strip ```json ... ``` fences if present. Idempotent."""
    text = text.strip()
    m = _JSON_FENCE_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


def _clamp_confidence(value: object) -> float:
    """Coerce confidence to float, clamp to [0.0, 1.0]. Default 0.0."""
    if isinstance(value, bool):
        # bool is subclass of int — treat True as 1.0, False as 0.0
        return float(value)
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    return 0.0


# ============================================================================
# Public API
# ============================================================================
def validate_classify_response(raw_text: str) -> ClassifyResult:
    """Parse + validate a Qwen call 1 response.

    Args:
        raw_text: raw response from Qwen3-VL. May be JSON, may be wrapped
            in ```json``` fences, may be malformed prose, may be empty.

    Returns:
        ClassifyResult. Never None. On any parse failure, returns
        ClassifyResult(label=OTHER, confidence=0.0, reasoning="",
        fallback_used=True).

    Notes:
        - Strips ```json fences before parsing.
        - Unknown class values fall back to OTHER with fallback_used=True.
        - Missing `class` key falls back to OTHER.
        - Missing `confidence` defaults to 0.0 (no fallback).
        - Confidence outside [0, 1] is CLAMPED, not fallback.
    """
    if not raw_text or not raw_text.strip():
        return ClassifyResult(
            label=ClassLabel.OTHER, confidence=0.0, reasoning="", fallback_used=True
        )

    stripped = _strip_code_fences(raw_text)

    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return ClassifyResult(
            label=ClassLabel.OTHER, confidence=0.0, reasoning="", fallback_used=True
        )

    if not isinstance(obj, dict):
        return ClassifyResult(
            label=ClassLabel.OTHER, confidence=0.0, reasoning="", fallback_used=True
        )

    # Extract + validate class
    raw_class = obj.get("class")
    if isinstance(raw_class, str) and raw_class in VALID_CLASSES:
        label = ClassLabel(raw_class)
        fallback = False
    else:
        label = ClassLabel.OTHER
        fallback = True

    # Extract confidence (clamp, no fallback)
    confidence = _clamp_confidence(obj.get("confidence"))

    # Extract reasoning (string or "")
    raw_reasoning = obj.get("reasoning", "")
    reasoning = raw_reasoning if isinstance(raw_reasoning, str) else ""

    return ClassifyResult(
        label=label, confidence=confidence, reasoning=reasoning, fallback_used=fallback
    )
