"""vehicle_position — gate-driven motion result builder + dataclasses.

No vision calls. No matching. No Telegram.
"""

from .motion_detector import (
    BoundingBox,
    MotionDetectorConfig,
    MovingObject,
    PositionResult,
    build_motion_result_from_gate,
)

__all__ = [
    "BoundingBox",
    "MotionDetectorConfig",
    "MovingObject",
    "PositionResult",
    "build_motion_result_from_gate",
]