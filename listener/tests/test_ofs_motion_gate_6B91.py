"""Phase 6B.91 (2026-08-18) — OFS motion gate prose-OR fallback tests.

Pins the Tesla drive-by failure mode (15:08 EDT alert f7da4c42:
detector said no motion, vision correctly identified a blue sedan
moving, alert suppressed as L0).

The fix: detector wins when it sees motion; fall back to
prose-implies-motion per vehicle ONLY when (a) detector missed AND
(b) vision saw vehicles in scene. Otherwise (no vehicles in scene
or detector sees motion and vision disagrees) the gate stays strict.

These tests verify the LISTENER gate logic by re-implementing the
relevant slice and exercising it against the canonical failure cases.
"""
from __future__ import annotations

# Mirror the listener's gate constants + helper. Keep in sync with
# listener/listener.py around line 3386.
_MOTION_KEYWORDS = (
    "moving", "driving", "drives", "approaching",
    "entering", "passing", "crossing",
)


def _prose_implies_motion(v: dict) -> bool:
    reasoning = (
        v.get("motion_reasoning")
        or v.get("description")
        or v.get("caption")
        or ""
    ).lower()
    return any(kw in reasoning for kw in _MOTION_KEYWORDS)


def _new_gate(
    vehicles_in_scene: list,
    detector_finds_motion: bool,
) -> list:
    """Mirror of listener/listener.py 3425-3447 (post-6B.91)."""
    use_prose_fallback = (
        not detector_finds_motion
        and bool(vehicles_in_scene)
    )
    if not (detector_finds_motion or use_prose_fallback):
        return []
    return [
        v for v in vehicles_in_scene
        if detector_finds_motion or _prose_implies_motion(v)
    ]


class TestOFSMotionGate6B91:
    """Pin the failure mode + regression-protection."""

    def test_case1_tesla_driveby_detector_missed_vision_moving(self):
        """The actual 15:08 EDT failure: detector=missed, vision=moving."""
        v = [{"color": "blue", "body_style_hint": "sedan",
              "description": "A blue sedan is moving across the frame on a gravel road."}]
        result = _new_gate(v, detector_finds_motion=False)
        assert len(result) == 1, (
            "Tesla case: detector missed but vision says moving — "
            "prose fallback MUST fire the alert"
        )

    def test_case2_parked_truck_detector_missed_vision_stationary(self):
        """6B.71's false-positive concern: parked car, no motion in prose."""
        v = [{"color": "white", "body_style_hint": "truck",
              "description": "A white pickup truck is parked on the gravel lot."}]
        result = _new_gate(v, detector_finds_motion=False)
        assert len(result) == 0, (
            "Parked truck: detector missed AND vision stationary — "
            "no alert (false-positive prevention preserved)"
        )

    def test_case3_detector_sees_motion_wins_always(self):
        """6B.71's detector-truth preference: when detector fires, gate passes through."""
        v = [{"color": "red", "description": "A red car is driving through."}]
        result = _new_gate(v, detector_finds_motion=True)
        assert len(result) == 1

    def test_case4_empty_vision_no_vehicles(self):
        """No vehicles in scene → no alert (independent of detector)."""
        assert _new_gate([], detector_finds_motion=False) == []
        assert _new_gate([], detector_finds_motion=True) == []

    def test_case5_detector_sees_motion_multiple_parked_cars_in_prose(self):
        """Detector wins, even if prose says all parked. Detector is authoritative."""
        v = [
            {"color": "white", "description": "A white car parked."},
            {"color": "black", "description": "A black car parked."},
        ]
        result = _new_gate(v, detector_finds_motion=True)
        assert len(result) == 2

    def test_case6_detector_missed_mixed_prose_one_moving_one_parked(self):
        """Detector missed: alert only fires for the one with moving prose."""
        v = [
            {"color": "white", "description": "A white car parked on the driveway."},
            {"color": "red", "description": "A red car is moving across the gravel road."},
        ]
        result = _new_gate(v, detector_finds_motion=False)
        assert len(result) == 1
        assert result[0]["color"] == "red"

    def test_case7_motion_reasoning_field_used_when_description_empty(self):
        """vision may put motion prose in motion_reasoning, not description."""
        v = [{"color": "blue",
              "motion_reasoning": "The vehicle is moving toward the camera.",
              "description": ""}]
        result = _new_gate(v, detector_finds_motion=False)
        assert len(result) == 1

    def test_case8_caption_field_used_as_final_fallback(self):
        """If only caption has motion prose, still fires."""
        v = [{"color": "silver", "caption": "silver SUV driving down driveway"}]
        result = _new_gate(v, detector_finds_motion=False)
        assert len(result) == 1

    def test_case9_passing_keyword_triggers_fallback(self):
        """Each motion keyword should trigger."""
        for kw in ("moving", "driving", "drives", "approaching",
                   "entering", "passing", "crossing"):
            v = [{"color": "x", "description": f"vehicle is {kw} across"}]
            assert len(_new_gate(v, detector_finds_motion=False)) == 1, kw