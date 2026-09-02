"""
test_gatekeeper_vision_result_coercion.py — Regression test for the
crop-pipeline → gatekeeper motion alert body wiring.

Phase 6B.87 (PLAN.md §11.17) introduced OFG as a second gatekeeper
camera. While verifying, we discovered that the listener's call to
`identify_from_crops()` returns an `IdentifierResult` whose
`vision_result` field is a `VisionResult` dataclass on success, NOT
a dict. The pre-6B.87 code coerced it via
`vision_result = _vr if isinstance(_vr, dict) else {}` — which fell
through to `{}` on success, stripping Qwen's full identification
(make/model/color/vehicle_features/confidence) from the gatekeeper
motion alert body. The body rendered `qwen confidence: (empty)` and
`1. vehicle` with no description, even though the crops pipeline had
succeeded at 0.85-0.95 confidence (verified against on-disk
`raw_vision_crop_*.json` for alert b9bd43e0-bf3a-4b15-bc86-f00a1202ae27).

This test pins down the new behavior: every branch of the coercion
must produce a dict the alert body builder can read. Four branches:

    1. VisionResult (success)  → _vr.to_dict() (full identification)
    2. dict (legacy path)      → _vr (verbatim)
    3. VisionError (failure)   → _vr.to_dict() (failure sentinel)
    4. None (no crops / other) → {}

The test does NOT import listener.py directly. Instead, it replicates
the coercion logic at the listener call site and asserts each branch.
This isolates the test from listener.py's heavy imports (Flask,
launchd, RTSP, vision_queue, etc.) which would require a much
heavier test fixture just to cover one if/elif block. The mirrored
function must be updated whenever the listener's coercion changes.
"""
from __future__ import annotations

from vehicle_identifier.vision_client import VisionError, VisionResult


def _coerce_vision_result(_vr):
    """Mirror of listener/listener.py:2395-2419 as of Phase 6B.87.

    If the listener call site changes, this test must change too.
    The test's purpose is to lock the coercion contract; if the
    contract changes deliberately, both sides change together.
    """
    if isinstance(_vr, VisionResult):
        return _vr.to_dict()
    if isinstance(_vr, dict):
        return _vr
    # VisionError or None
    return _vr.to_dict() if isinstance(_vr, VisionError) else {}


# ---------------------------------------------------------------------------
# Success path (regression: pre-6B.87 silently coerced VisionResult to {})
# ---------------------------------------------------------------------------


def test_vision_result_success_unwraps_to_full_identification_dict():
    """When identify_from_crops returns a VisionResult (the success
    case), the coercion must surface Qwen's full identification fields
    so the gatekeeper motion alert body can render make/model/
    confidence/description — NOT coerce to {} which renders as
    `(empty)` and `1. vehicle`.
    """
    vr = VisionResult(
        content={
            "color": "white",
            "body_style_hint": "pickup",
            "make": "Chevrolet",
            "model": "Silverado 1500",
            "vehicle_features": {
                "wheel_style": "steel",
                "wheel_arch": "rounded",
                "front_grille_style": "horizontal slats",
            },
            "description": "A white Chevrolet Silverado 1500 pickup.",
            "confidence": 0.95,
        },
        elapsed_ms=6782.9,
        raw_text="<raw qwen json>",
    )

    out = _coerce_vision_result(vr)

    # The full identification must survive the coercion.
    assert isinstance(out, dict)
    assert out["make"] == "Chevrolet"
    assert out["model"] == "Silverado 1500"
    assert out["color"] == "white"
    assert out["confidence"] == 0.95
    assert out["description"] == "A white Chevrolet Silverado 1500 pickup."
    assert out["vehicle_features"]["wheel_style"] == "steel"


# ---------------------------------------------------------------------------
# Legacy path (unchanged)
# ---------------------------------------------------------------------------


def test_dict_passthrough_unchanged():
    """Legacy dict-shaped vision_result (e.g. analyze_frames_queued
    output) must pass through verbatim. This is the pre-6B.87 path
    and the new code must not regress it.
    """
    legacy = {
        "verdict": "vehicle",
        "description": "...",
        "vehicles": [{"color": "white", "make": "Toyota"}],
        "confidence": 0.7,
    }
    out = _coerce_vision_result(legacy)
    assert out is legacy  # same object identity, not a copy


# ---------------------------------------------------------------------------
# Failure path (VisionError → failure sentinel dict, NOT {})
# ---------------------------------------------------------------------------


def test_vision_error_preserves_failure_sentinel():
    """When vision fails, identify_from_crops returns VisionError.
    Coercing it to {} would silently swallow the failure (downstream
    would render `Vision: (empty)` instead of the structured error).
    The new code must surface VisionError.to_dict() — the failure
    sentinel — so downstream code can detect the failure.
    """
    err = VisionError(
        kind="timeout",
        message="qwen api timed out after 30s",
        elapsed_ms=30000.0,
    )
    out = _coerce_vision_result(err)
    assert isinstance(out, dict)
    # Failure sentinel shape — downstream uses these keys to detect failure.
    assert out["objects_detected"] == ["error"]
    assert out["error"]["kind"] == "timeout"
    assert "timed out" in out["error"]["message"]


# ---------------------------------------------------------------------------
# Empty path (None → {}, no crash)
# ---------------------------------------------------------------------------


def test_none_coerces_to_empty_dict():
    """When identify_from_crops sees no crops (no_motion fallback),
    vision_result is None. Coercion must produce {} without crashing,
    matching pre-6B.87 behavior for this branch.
    """
    out = _coerce_vision_result(None)
    assert out == {}


# ---------------------------------------------------------------------------
# The actual regression case (replay of b9bd43e0)
# ---------------------------------------------------------------------------


def test_replay_of_b9bd43e0_silverado_identification_survives_coercion():
    """Replay the alert that surfaced this bug. The on-disk record
    `raw_vision_crop_0.json` for alert b9bd43e0-bf3a-4b15-bc86-
    f00a1202ae27 shows Qwen returned Chevrolet Silverado 1500 at
    0.95 confidence. Pre-6B.87 the alert body showed `1. vehicle`
    with no description. After 6B.87, the Silverado identification
    must survive the coercion.
    """
    # Crop 0 from raw_vision_crop_0.json for b9bd43e0
    vr = VisionResult(
        content={
            "color": "white",
            "body_style_hint": "pickup",
            "make": "Chevrolet",
            "model": "Silverado 1500",
            "vehicle_features": {
                "wheel_style": "steel",
                "wheel_arch": "rounded",
                "wheel_color": "black",
                "roofline_style": "straight",
                "front_grille_style": "horizontal slats",
                "headlight_signature": "rectangular",
                "rear_lights_signature": None,
                "tailgate_type": "standard",
                "badge_text_readable": False,
                "window_tint": "none",
                "cab_marker_lights": False,
                "bed_cover": "none",
            },
            "description": (
                "A white Chevrolet Silverado 1500 pickup truck with a "
                "steel wheel, rounded wheel arches, horizontal slat "
                "grille, and rectangular headlights. No bed cover or "
                "visible badge text. Windows are clear with no tint."
            ),
            "confidence": 0.95,
        },
        elapsed_ms=6782.9,
        raw_text='{"color": "white", ..., "confidence": 0.95}',
    )

    out = _coerce_vision_result(vr)

    # These are the exact strings the alert body needs to render.
    assert "Chevrolet" in str(out.get("make", ""))
    assert "Silverado" in str(out.get("model", ""))
    assert out.get("confidence") == 0.95
    assert "Chevrolet Silverado 1500" in out.get("description", "")
