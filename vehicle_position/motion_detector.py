"""
Motion detection — refactor vocabulary wrapper over the impl.

Phase.115 (2026-08-25): the legacy 6-frame pairwise-diff detector
was removed. This module now re-exports the gate-driven `build_motion_result_from_gate()`
function from the impl, plus the refactor-vocab dataclasses
(MovingObject, PositionResult).

Public API:
    BoundingBox            — (x, y, w, h) named tuple
    MovingObject           — one tracked moving object (4 frames)
    PositionResult         — output of build_motion_result_from_gate()
    MotionDetectorConfig   — reserved for future tunables
    build_motion_result_from_gate() — gate outputs → PositionResult

Phase.115 history: previously this module wrapped `detect_motion()`
which took 6 frame paths and did its own pairwise diff + crop extraction.
The motion gate now owns the diff + crop extraction; this wrapper exposes
the gate-driven builder with the refactor's vocabulary types.

Why vendored: the refactor's pipeline/orchestrator must not import
from src/. The orchestrator lives in the refactor package and only
imports from sibling domains. The impl module is the source of truth
for the dataclasses; this wrapper renames and re-exports them with
the refactor's preferred names.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Re-export the dataclass names from the impl module so callers see
# the refactor vocab. The impl module uses the legacy names
# (MovingObject, MotionResult) for backward compatibility; this
# wrapper exposes PositionResult as an alias.
from .motion_detector_impl import (
    build_motion_result_from_gate as _build_motion_result_from_gate_impl,
)

# --- Refactor vocabulary ----------------------------------------------------


@dataclass
class MovingObject:
    """One tracked moving object. Refactor's vocabulary (same shape as
    the impl's MovingObject)."""
    bbox_per_frame: list[tuple[int, int, int, int]] = field(default_factory=list)
    center_per_frame: list[tuple[int, int]] = field(default_factory=list)
    area_per_frame: list[int] = field(default_factory=list)
    trajectory: list[str] = field(default_factory=list)
    avg_area: int = 0
    frames_seen: int = 0
    total_motion_pixels: int = 0
    position_change_max: int = 0
    best_crop_path: str | None = None
    crop_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PositionResult:
    """Output of build_motion_result_from_gate(). Refactor's vocabulary."""
    moving_objects: list[MovingObject] = field(default_factory=list)
    primary_moving_object: MovingObject | None = None
    best_crop_path: str | None = None
    crop_paths: list[str] = field(default_factory=list)
    no_motion_detected: bool = True
    reference_method: str = "gate"  # Phase.115: always "gate"
    total_motion_pixels: int = 0
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "moving_objects": [m.to_dict() for m in self.moving_objects],
            "primary_moving_object":
                self.primary_moving_object.to_dict()
                if self.primary_moving_object else None,
            "best_crop_path": self.best_crop_path,
            "crop_paths": self.crop_paths,
            "no_motion_detected": self.no_motion_detected,
            "reference_method": self.reference_method,
            "total_motion_pixels": self.total_motion_pixels,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True)
class BoundingBox:
    """A bounding box (x, y, width, height) in pixels."""
    x: int
    y: int
    width: int
    height: int

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class MotionDetectorConfig:
    """Config. Currently passes through to underlying detector tunables.

    Hooks here for future tunables without breaking the public API.
    """


# --- Conversion from impl's types to refactor vocabulary --------------------


def _convert_moving_object(m: Any) -> MovingObject:
    """Map impl's MovingObject dataclass to refactor's."""
    return MovingObject(
        bbox_per_frame=list(m.bbox_per_frame),
        center_per_frame=list(m.center_per_frame),
        area_per_frame=list(m.area_per_frame),
        trajectory=list(m.trajectory),
        avg_area=m.avg_area,
        frames_seen=m.frames_seen,
        total_motion_pixels=getattr(m, "total_motion_pixels", 0),
        position_change_max=m.position_change_max,
        best_crop_path=m.best_crop_path,
        crop_paths=list(m.crop_paths),
    )


def _convert_result(r: Any) -> PositionResult:
    """Map impl's MotionResult to refactor's PositionResult."""
    return PositionResult(
        moving_objects=[_convert_moving_object(m) for m in r.moving_objects],
        primary_moving_object=(
            _convert_moving_object(r.primary_moving_object)
            if r.primary_moving_object else None
        ),
        best_crop_path=r.best_crop_path,
        crop_paths=list(r.crop_paths),
        no_motion_detected=r.no_motion_detected,
        reference_method=r.reference_method,
        total_motion_pixels=r.total_motion_pixels,
        elapsed_ms=r.elapsed_ms,
    )


# --- Public API -------------------------------------------------------------


def build_motion_result_from_gate(
    frames: list,
    crop_a,
    crop_b,
    bbox_a: tuple[int, int, int, int] | None,
    bbox_b: tuple[int, int, int, int] | None,
    alert_id: str,
    crop_paths: list[str] | None = None,
) -> PositionResult:
    """Run gate-driven motion result build. Returns refactor PositionResult.

    Phase.115 (§11.46.6): all inputs are in-memory PIL.Image objects
    (no filesystem reads on the hot path). crop_paths is optional —
    only present when GATE_KEEP_DISK_ARTIFACTS=true.

    Returns a refactor-vocab PositionResult.

    Args:
        frames: 4 PIL.Image frames in capture order (frames[0]..frames[3])
        crop_a: PIL.Image of bbox_a crop (or None if no motion)
        crop_b: PIL.Image of bbox_b crop (or None if no motion)
        bbox_a: diff(frame_2, frame_3) bbox @ native res, or None
        bbox_b: diff(frame_3, frame_4) bbox @ native res, or None
        alert_id: alert UUID (for log lines)
        crop_paths: optional list of 2 disk paths (postmortem convenience)

    Returns:
        PositionResult with primary_moving_object.trajectory = 4 cells,
        crop_paths = [crop_a_path, crop_b_path] (or []), and
        no_motion_detected=True iff both bbox_a and bbox_b are None.
    """
    legacy_result = _build_motion_result_from_gate_impl(
        frames=list(frames),
        crop_a=crop_a,
        crop_b=crop_b,
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        alert_id=alert_id,
        crop_paths=list(crop_paths or []),
    )
    return _convert_result(legacy_result)