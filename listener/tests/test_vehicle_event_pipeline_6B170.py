"""
test_vehicle_event_pipeline_6B170.py — Phase.170 regression test (2026-09-01).

Bug fixed: match_stage() ignored Qwen3-VL's `confidence` field entirely.
The matcher only scored (color, type, make, model, vehicle_features)
against known vehicles, so a vehicle event with vision confidence=0.0
could still produce a Jayco match when color/type aligned with a known
vehicle. Symptom: live alert b136f261 at 2026-08-31 21:28:27 EDT,
Outside Front Solar/CAM5 — Qwen returned:

  {
    "color": "white",
    "body_style_hint": "trailer",
    "description": "A large, white, boxy object moving across the frame,
      consistent with a small enclosed trailer or cargo box.
      No vehicle features like wheels, lights, or badges are visible.",
    "confidence": 0.0
  }

The matcher scored 2.00 (color=white + type=trailer matched Jayco Jay
Feather, gap 0.50 over v_white_box_trailer). Telegram #3 "Match — Jayco
Jay Feather" was sent. False positive.

Fix: match_stage() suppresses the match-alert path when
vision_result["confidence"] < VISION_CONFIDENCE_FLOOR (0.3). The
matcher itself still runs in case future work needs the score;
the suppression only short-circuits the Telegram chain.

This test pins:
  1. confidence=0.0 with valid signature  → match_verdict=NoMatch(reason="low_vision_confidence")
  2. confidence<0.3 with valid signature  → same suppression
  3. confidence=0.3 (boundary)            → matcher runs normally (>= floor)
  4. confidence=0.7 with valid signature  → matcher runs normally
  5. confidence=None (legacy / missing)    → matcher runs (don't break old fixtures)
  6. confidence as string "0.5"            → matcher runs (defensive: don't crash on bad type)
  7. NoMatch top_candidates list is empty (no reason to spend score_top_n on suppression)
  8. Suppression log line includes confidence and floor
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

from listener.vehicle_pipeline import AlertContext, VISION_CONFIDENCE_FLOOR
from listener.vehicle_pipeline.match import match_stage
from vehicle_matcher.matcher import NoMatch

# --- helpers ---------------------------------------------------------------

GATEKEEPER_FRIENDLY = "Outside Front Solar"
GATEKEEPER_CODE = "CAM5"
GATEKEEPER_SET = frozenset({"CAM1", "CAM2", "CAM3", "CAM4", "CAM5", "CAM6"})


def _make_ctx(
    *,
    vision_confidence,
    vision_result_extra: dict | None = None,
    camera_name: str = GATEKEEPER_FRIENDLY,
    camera_code: str = GATEKEEPER_CODE,
    is_vehicle_event: bool = True,
    known_vehicles: list | None = None,
) -> AlertContext:
    """Build the smallest AlertContext that exercises match_stage's
    confidence floor. vision_result is a flat top-level shape (the legacy
    single-vehicle schema that b136f261 produced)."""
    vr: dict = {
        "color": "white",
        "body_style_hint": "trailer",
        "make": None,
        "model": None,
        "vehicle_features": {
            "wheel_style": None,
            "wheel_arch": None,
            "wheel_color": None,
            "roofline_style": "boxy",
            "front_grille_style": None,
            "headlight_signature": None,
            "rear_lights_signature": None,
            "tailgate_type": None,
            "badge_text_readable": None,
            "window_tint": None,
            "cab_marker_lights": None,
            "bed_cover": None,
        },
        "description": "test",
    }
    # confidence: only set when not None (legacy fixtures had no field)
    if vision_confidence is not None:
        vr["confidence"] = vision_confidence
    if vision_result_extra:
        vr.update(vision_result_extra)
    return AlertContext(
        alert_id="6B170-test",
        camera_name=camera_name,
        camera_code=camera_code,
        timestamp="2026-08-31 21:28:27 EDT",
        event_type="vehicle" if is_vehicle_event else "person",
        rtsp_url="rtsp://test/ofs",
        output_dir="/tmp/6B170-test",
        is_vehicle_event=is_vehicle_event,
        vision_result=vr,
        known_vehicles=known_vehicles or [],
        bot_token="test-token",
        chat_id="test-chat",
        api_url="http://127.0.0.1:8093/v1/chat/completions",
        gatekeeper_cameras=GATEKEEPER_SET,
    )


# --- Test 1: confidence floor constant ------------------------------------


class TestVisionConfidenceFloorConstant:
    """VISION_CONFIDENCE_FLOOR must exist, be float, and be < 0.6 (the
    matcher's match-score threshold). The floor is intentionally lower
    than the match-score threshold so Qwen's confidence gates the EARLIER
    decision (whether to attempt a match at all) while the matcher
    threshold gates whether the match is good enough to alert on."""

    def test_floor_is_defined(self):
        assert hasattr(
            __import__("listener.vehicle_pipeline", fromlist=["VISION_CONFIDENCE_FLOOR"]),
            "VISION_CONFIDENCE_FLOOR",
        ), "VISION_CONFIDENCE_FLOOR must be a module-level constant"

    def test_floor_is_float(self):
        assert isinstance(VISION_CONFIDENCE_FLOOR, float), (
            f"VISION_CONFIDENCE_FLOOR must be float, got "
            f"{type(VISION_CONFIDENCE_FLOOR).__name__}"
        )

    def test_floor_value(self):
        # Pinned — change deliberately requires updating this test.
        assert VISION_CONFIDENCE_FLOOR == 0.3, (
            f"VISION_CONFIDENCE_FLOOR changed from 0.3 to "
            f"{VISION_CONFIDENCE_FLOOR}; update this test or revert."
        )

    def test_floor_below_match_threshold(self):
        # The matcher's match_score_threshold is 0.6 (infra/vehicle_matcher.py).
        # Floor must be below it so the two thresholds don't collide.
        assert VISION_CONFIDENCE_FLOOR < 0.6, (
            f"Floor {VISION_CONFIDENCE_FLOOR} must be below match-score "
            f"threshold 0.6 — otherwise the floor never fires."
        )


# --- Test 2: suppression behavior -----------------------------------------


class TestMatchStageSuppressesLowConfidence:
    """Below floor → NoMatch(low_vision_confidence). Matcher does NOT run."""

    def test_confidence_zero_suppressed(self, caplog):
        """b136f261 reproduction: confidence=0.0 with valid signature
        must NOT reach the matcher."""
        ctx = _make_ctx(vision_confidence=0.0)
        with caplog.at_level(logging.INFO, logger="listener.vehicle_pipeline.match"):
            match_stage(ctx)
        assert ctx.match_verdict is not None, (
            "match_stage must populate match_verdict even when suppressing"
        )
        assert isinstance(ctx.match_verdict, NoMatch), (
            f"match_verdict must be NoMatch, got {type(ctx.match_verdict).__name__}"
        )
        assert ctx.match_verdict.reason == "low_vision_confidence", (
            f"Suppression reason must be 'low_vision_confidence', got "
            f"{ctx.match_verdict.reason!r}"
        )

    def test_confidence_zero_match_verdict_not_matchverdict(self):
        """Suppress path must NOT construct a MatchVerdict (which would
        indicate the matcher ran)."""
        ctx = _make_ctx(vision_confidence=0.0)
        match_stage(ctx)
        from vehicle_matcher.matcher import MatchVerdict
        assert not isinstance(ctx.match_verdict, MatchVerdict), (
            "Suppression must not produce a MatchVerdict — the matcher "
            "didn't run, so there's no match to verdict."
        )

    def test_confidence_zero_top_candidates_empty(self):
        """NoMatch.top_candidates must be [] on suppression — we don't
        spend score_top_n() cycles on events we're throwing away."""
        ctx = _make_ctx(vision_confidence=0.0)
        match_stage(ctx)
        assert ctx.match_verdict.top_candidates == [], (
            f"Suppression top_candidates must be empty, got "
            f"{ctx.match_verdict.top_candidates!r}"
        )

    def test_confidence_just_below_floor_suppressed(self, caplog):
        """Boundary: 0.29 < 0.30 floor → suppression."""
        ctx = _make_ctx(vision_confidence=0.29)
        with caplog.at_level(logging.INFO, logger="listener.vehicle_pipeline.match"):
            match_stage(ctx)
        assert ctx.match_verdict is not None
        assert isinstance(ctx.match_verdict, NoMatch)
        assert ctx.match_verdict.reason == "low_vision_confidence"

    def test_suppression_log_includes_confidence_and_floor(self, caplog):
        """Forensic log must surface confidence and floor for
        post-incident debugging."""
        ctx = _make_ctx(vision_confidence=0.0)
        with caplog.at_level(logging.INFO, logger="listener.vehicle_pipeline.match"):
            match_stage(ctx)
        suppress_logs = [
            r for r in caplog.records
            if "vision confidence" in r.getMessage()
            and "suppressing" in r.getMessage()
        ]
        assert suppress_logs, (
            f"Expected suppression log line. Got: {[r.getMessage() for r in caplog.records]}"
        )
        msg = suppress_logs[0].getMessage()
        assert "0.00" in msg, f"Log must include confidence=0.00 (got: {msg!r})"
        assert "0.30" in msg, f"Log must include floor=0.30 (got: {msg!r})"

    def test_suppression_does_not_populate_score_top_n(self):
        """We don't run the matcher on suppressed events — score_top_n
        stays empty. Downstream code that reads ctx.score_top_n won't
        see stale data from a previous event."""
        ctx = _make_ctx(vision_confidence=0.0)
        match_stage(ctx)
        assert ctx.score_top_n in (None, [], ()), (
            f"Suppressed event must not populate score_top_n; "
            f"got {ctx.score_top_n!r}"
        )


# --- Test 3: floor-and-above behavior -------------------------------------


class TestMatchStagePassesAtOrAboveFloor:
    """At or above floor → matcher runs (no early suppression)."""

    def test_confidence_at_floor_passes_floor(self):
        """Boundary: 0.30 == floor → not strictly less than, so passes.
        The matcher may or may not produce a match depending on the
        signature. We only assert the floor didn't trigger."""
        ctx = _make_ctx(vision_confidence=VISION_CONFIDENCE_FLOOR)
        match_stage(ctx)
        # Either NoMatch(reason="below_threshold") from the matcher OR
        # a MatchVerdict. NOT NoMatch(reason="low_vision_confidence").
        assert ctx.match_verdict is not None
        if isinstance(ctx.match_verdict, NoMatch):
            assert ctx.match_verdict.reason != "low_vision_confidence", (
                f"At-floor confidence must not trigger floor suppression "
                f"(reason was {ctx.match_verdict.reason!r})"
            )

    def test_confidence_high_passes_floor(self):
        """confidence=0.7 with empty known_vehicles → matcher returns
        no match (NoMatch reason='below_threshold'), NOT 'low_vision_confidence'."""
        ctx = _make_ctx(vision_confidence=0.7)
        match_stage(ctx)
        assert ctx.match_verdict is not None
        if isinstance(ctx.match_verdict, NoMatch):
            assert ctx.match_verdict.reason != "low_vision_confidence", (
                f"High-confidence pass must not trigger floor suppression "
                f"(reason was {ctx.match_verdict.reason!r})"
            )

    def test_confidence_high_produces_match(self):
        """confidence=0.7 with a matching known vehicle produces a real
        MatchVerdict — proves the floor doesn't false-positive on
        legitimate matches."""
        # Build a Jayco-like known vehicle that matches color=white + type=trailer
        ctx = _make_ctx(
            vision_confidence=0.7,
            known_vehicles=[{
                "id": "v_jayco_camper",
                "color": "white",
                "type": "trailer",
                "make": "Jayco",
                "model": "Jay Feather",
                "vehicle_features": {"roofline_style": "boxy"},
            }],
        )
        match_stage(ctx)
        from vehicle_matcher.matcher import MatchVerdict
        assert isinstance(ctx.match_verdict, MatchVerdict), (
            f"High-confidence + matching known vehicle must produce "
            f"MatchVerdict, got {type(ctx.match_verdict).__name__}"
        )


# --- Test 4: legacy / missing / malformed confidence ----------------------


class TestMatchStageHandlesMissingOrMalformedConfidence:
    """Defensive: don't crash on missing, None, or non-numeric confidence."""

    def test_confidence_missing_passes_floor(self):
        """Legacy fixtures may lack the 'confidence' key entirely.
        match_stage must not crash."""
        ctx = _make_ctx(vision_confidence=None)
        # vision_result dict still gets created, just without 'confidence' key
        match_stage(ctx)
        assert ctx.match_verdict is not None
        if isinstance(ctx.match_verdict, NoMatch):
            assert ctx.match_verdict.reason != "low_vision_confidence", (
                "Missing confidence must not be treated as low confidence"
            )

    def test_confidence_as_string_passes_floor(self):
        """Bad data: confidence="0.5" (string). Don't crash; treat as
        non-numeric → pass through to matcher."""
        ctx = _make_ctx(vision_confidence="0.5")
        match_stage(ctx)
        assert ctx.match_verdict is not None
        if isinstance(ctx.match_verdict, NoMatch):
            assert ctx.match_verdict.reason != "low_vision_confidence", (
                "String confidence must not be coerced to 0 and trigger "
                "the floor — defensive fallback: pass through."
            )

    def test_confidence_as_zero_string_passes_floor(self):
        """Specifically test the malicious "0" string: it's truthy as a
        string but == 0 as float. Our isinstance(int,float) check skips
        strings, so we don't accidentally suppress on bad input."""
        ctx = _make_ctx(vision_confidence="0")
        match_stage(ctx)
        assert ctx.match_verdict is not None
        if isinstance(ctx.match_verdict, NoMatch):
            assert ctx.match_verdict.reason != "low_vision_confidence", (
                "String '0' must not trigger floor (defensive against "
                "schema errors)"
            )

    def test_vision_result_empty_dict_passes_floor(self):
        """ctx.vision_result = {} (vision failed). match_stage hits the
        'no signature' branch and returns before the floor check."""
        ctx = _make_ctx(vision_confidence=None)
        ctx.vision_result = {}
        match_stage(ctx)
        # No match_verdict because we returned at the no-signature guard.
        assert ctx.match_verdict is None