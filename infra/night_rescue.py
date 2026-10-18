"""
night_rescue.py — VM1 night-time no_class rescue (US-111a).

STATUS: new (US-111a, 2026-10-18)
THREAD SAFETY: single-threaded, no shared mutable state
IMPORT ORDER: stdlib -> third-party -> local (infra.)

INPUTS:
    - env vars: FARMSURV_NIGHT_RESCUE (0|1), FARMSURV_NIGHT_RESCUE_CONF_FLOOR
    - frames: list[str] paths to frame images
    - bboxes: list[tuple] from motion gate / YOLO
    - camera_id: str
    - alert_id: str
    - timestamp: tz-aware UTC datetime

OUTPUTS:
    - night_rescue() -> dict | None
      Rescue dict with classification info, or None (drop path).

DOES NOT DO:
    - Gate classification (caller decides)
    - Crop generation (delegates to frame_diff.crop_frame_to_bbox)
    - Night window computation (delegates to time_of_day.is_night_at_edt)

CALLED BY:
    - listener/daemon.py _stage_gate (gate_verdict.is_none() branch)

CALLS INTO:
    - infra.frame_diff.crop_frame_to_bbox
    - infra.vision_analyzer.verify_class
    - infra.time_of_day.is_night_at_edt
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

from infra.time_of_day import is_night_at_edt
from infra.frame_diff import crop_frame_to_bbox as _crop_frame_to_bbox
from infra.vision_analyzer import verify_class as _verify_class

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env helpers (read at CALL time, not import time — pattern: frame_diff ~136)
# ---------------------------------------------------------------------------


def rescue_enabled() -> bool:
    """Read FARMSURV_NIGHT_RESCUE at call time.

    1 = ON (apply night rescue on gate-dropped alerts).
    0 = OFF (byte-identical drop path, pre-change behavior).

    Returns:
        True if night rescue is enabled, False otherwise.
    """
    raw = os.environ.get("FARMSURV_NIGHT_RESCUE", "0")
    return raw.strip().lower() in ("1", "on", "true")


def conf_floor() -> float:
    """Read FARMSURV_NIGHT_RESCUE_CONF_FLOOR at call time.

    Default 0.50. Minimum confidence for a rescue match.

    Returns:
        Confidence floor (float).
    """
    raw = os.environ.get("FARMSURV_NIGHT_RESCUE_CONF_FLOOR", "0.50")
    try:
        return float(raw.strip())
    except ValueError:
        return 0.50


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

_NIGHT_RESCUE_CLASSES = frozenset({"vehicle", "person", "animal"})


def night_rescue(
    frames: list[str],
    bboxes: list[tuple[int, int, int, int]],
    camera_id: str,
    alert_id: str,
    timestamp: datetime,
) -> dict | None:
    """Try VM1 rescue for a gate-dropped alert during night hours.

    Guard: enabled AND is_night_at_edt(timestamp) AND usable bbox in bboxes.

    If all guards pass, crops the best bbox via frame_diff, calls verify_class,
    and checks class+confidence against the floor.

    Args:
        frames: Frame file paths.
        bboxes: List of (x, y, w, h) motion bboxes.
        camera_id: Camera identifier.
        alert_id: Alert UUID.
        timestamp: UTC datetime from the webhook.

    Returns:
        Rescue dict on success, None on failure (never raises).
    """
    # Guard 1: feature flag
    if not rescue_enabled():
        return None

    # Guard 2: night check
    if not is_night_at_edt(timestamp):
        return None

    # Guard 3: usable bbox
    if not bboxes:
        return None

    # Pick the largest bbox (same heuristic as cross-diff pick_best_bbox)
    best_bbox = max(bboxes, key=lambda b: b[2] * b[3])

    # Generate crops
    frame_path = frames[0] if frames else None
    if not frame_path:
        return None

    crop_a = _crop_frame_to_bbox(frame_path, best_bbox)
    if crop_a is None:
        return None

    crop_b: str | None = None
    if len(frames) > 1:
        crop_b = _crop_frame_to_bbox(frames[1], best_bbox)

    # If crop_b is None (failed or no second frame), pass crop_a as both.
    if crop_b is None:
        crop_b = crop_a

    # Verify with VM1
    try:
        result = _verify_class(crop_a, crop_b)
    except Exception:
        log.warning(
            f"night_rescue_error: verify_class failed for alert_id={alert_id} "
            f"camera={camera_id}"
        )
        return None

    # Check class and confidence
    classification = result.get("class", "")
    confidence = result.get("confidence", 0.0)

    # Handle enum-string confidence from VM1 schema ("likely" → float)
    if isinstance(confidence, str):
        _CONF_MAP = {
            "unlikely": 0.0,
            "possible": 0.3,
            "likely": 0.7,
            "very_likely": 0.9,
        }
        confidence = _CONF_MAP.get(confidence, 0.0)
    else:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            return None

    floor = conf_floor()

    if classification in _NIGHT_RESCUE_CLASSES and confidence >= floor:
        return {
            "status": "ok",
            "reason": "night_rescue_vm1",
            "classification": classification,
            "class_label": classification,
            "confidence": confidence,
            "gate_verdict": None,
            "gsum": {
                "classification": classification,
                "class_label": classification,
                "confidence": confidence,
                "reason": "night_rescue_vm1",
            },
        }

    return None
