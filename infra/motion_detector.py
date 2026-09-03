"""
motion_detector.py — Thin re-export shim for the legacy dataclasses.

STATUS: legacy
THREAD SAFETY: thread-safe (pure re-exports, no state)

The canonical home for `MovingObject` and `MotionResult` is
`infra.motion_types`. This module exists only as a back-compat shim
so existing imports (`from infra.motion_detector import MovingObject, MotionResult`)
continue to work — both in production code (e.g., telegram_formatter/vehicle_alert.py
type hints) and in legacy test fixtures (test_annotate_frame_bboxes.py,
test_format_detector_metadata.py).

HISTORY:
    Originally this module hosted both the 6-frame pairwise-diff detector
    (`detect_motion()`) and the `MovingObject`/`MotionResult` dataclasses.
    Phase.115 (§11.46) removed `detect_motion()` from the live path; the
    dataclasses remained here because they were type-hinted by downstream
    formatters.

    Phase §11.90 (2026-09-01) split this file:
    - MovingObject + MotionResult moved to infra.motion_types (single source
      of truth; duplicates in vehicle_position/motion_detector_impl.py also
      collapsed onto the same canonical home).
    - All 10 dead functions (detect_motion + helpers) were deleted.
    - The legacy test file infra/tests/test_motion_detector.py was deleted
      (it tested the dead functions).

    This shim is the only surviving artifact of the original module. It
    can be retired once the two test files update their imports to
    `from infra.motion_types import MovingObject, MotionResult` (Phase §11.91
    candidate, or follow-on cleanup).

DOES NOT DO:
    - Frame processing or motion detection (the gate owns this)
    - Define any new symbols (everything is a re-export)

PUBLIC API:
    MovingObject      — re-export from infra.motion_types
    MotionResult      — re-export from infra.motion_types

CALLED BY:
    - telegram_formatter.vehicle_alert (type hints + format_detector_metadata_lines)
    - telegram_formatter.composite_telegram (type hints)
    - infra.tests.test_annotate_frame_bboxes (test fixtures)
    - infra.tests.test_format_detector_metadata (test fixtures)
    - scripts.probe_enriched_alert (synthetic fixtures)
    - (Phase.86 onward) The listener pipeline no longer imports from here
"""

from infra.motion_types import MovingObject, MotionResult

__all__ = ["MovingObject", "MotionResult"]