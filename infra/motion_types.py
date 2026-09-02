"""
motion_types.py — Shared dataclasses for motion detection outputs.

STATUS: stable
THREAD SAFETY: thread-safe (plain dataclasses, no shared state)

This module is the single source of truth for the MovingObject / MotionResult
shapes. Both the legacy pairwise-diff detector (infra.motion_detector) and the
refactor's gate-driven detector (vehicle_position.motion_detector_impl) produce
and consume these types. Field shapes are kept field-compatible across all
three historical copies (infra, vehicle_position, and the vehicle_position
adapter) so a value constructed by any of them can be consumed by any other.

INPUTS:
    - MovingObject construction: per-frame bbox/center/area arrays + scalars
    - MotionResult construction: list[MovingObject], primary MovingObject,
      optional crop_paths, motion metadata

OUTPUTS:
    - MovingObject dataclass with per-frame arrays + scalar metrics
    - MotionResult dataclass with moving_objects, primary, crop_paths,
      motion metadata
    - to_dict() serializers for forensic JSON persistence

PUBLIC API:
    MovingObject
        Per-frame arrays + scalar metrics for one tracked subject.
    MotionResult
        Aggregate output of a motion-detection pass over a frame burst.

DOES NOT DO:
    - Load or process frames — that's owned by the detector modules
    - Persist JSON — callers write via to_dict() themselves
    - Run vision or matching — pure data containers

WHY HERE:
    Phase §11.90 (2026-09-01) consolidated three historical copies of these
    dataclasses into this single source-of-truth module. The legacy pairwise
    detector (infra.motion_detector), the refactor gate-driven detector
    (vehicle_position.motion_detector_impl), and the adapter module
    (vehicle_position.motion_detector) all carry re-export shims so existing
    import paths continue to work.

CALLED BY:
    - infra.motion_detector — re-exports for back-compat
    - vehicle_position.motion_detector_impl — direct construction
    - vehicle_position.motion_detector — re-exports for back-compat
    - telegram_formatter.vehicle_alert — type-hints + field access
    - telegram_formatter.composite_telegram — type-hints
    - scripts/probe_enriched_alert.py — synthetic fixture construction

CALLS INTO:
    - dataclasses: asdict, dataclass, field

RELATED:
    - infra.frame_diff.pairwise_diff — produces per-frame masks that feed
      into the legacy detector
    - vehicle_position.build_motion_result_from_gate — produces MotionResult
      from the motion-gate's in-memory outputs (no frame resize)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class MovingObject:
    bbox_per_frame: list[tuple[int, int, int, int]] = field(default_factory=list)
    center_per_frame: list[tuple[int, int]] = field(default_factory=list)
    area_per_frame: list[int] = field(default_factory=list)
    trajectory: list[str] = field(default_factory=list)
    avg_area: int = 0
    frames_seen: int = 0
    total_motion_pixels: int = 0
    position_change_max: int = 0
    best_crop_path: str | None = None
    crop_paths: list[str] = field(default_factory=list)  # Phase 6B.65: top-N crops

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MotionResult:
    moving_objects: list[MovingObject] = field(default_factory=list)
    primary_moving_object: MovingObject | None = None
    best_crop_path: str | None = None
    crop_paths: list[str] = field(default_factory=list)  # Phase 6B.65: top-N crops
    no_motion_detected: bool = True
    reference_method: str = "median"
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