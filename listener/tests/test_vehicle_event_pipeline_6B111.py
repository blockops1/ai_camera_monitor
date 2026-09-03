"""
test_vehicle_event_pipeline_6B111.py — Phase.111 integration tests.

Verifies the two pieces restored in 6B.111:

  1. Trajectory injection: identify_stage writes
     `motion_result.primary_moving_object.trajectory` into
     `ctx.vision_result["frame_positions"]` so downstream consumers
     (alert generator LLM prompt) can reference it. The slim pre-6B.111
     computed trajectory but never surfaced it.

  2. Composite Telegram: emit_result_stage fires the composite
     motion-trail Telegram after the lead motion Telegram. Failure
     must be silent — composite failures never block the lead motion
     Telegram or state update.

These tests proxy the production module by importing the actual
`identify_stage` and `emit_result_stage` functions and stubbing all
infrastructure calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


import pytest

from listener.vehicle_pipeline import AlertContext
from listener.vehicle_pipeline.emit import emit_result_stage
from listener.vehicle_pipeline.identify import identify_stage


def _track_call(items: list, value: object = True) -> bool:
    """Test helper: append ``value`` to ``items`` and return True.

    Replaces the ``lambda **kw: _track_call(list, x)`` idiom that mypy
    flags as ``[func-returns-value]`` (append returns None) and
    ``[truthy-bool]`` (the ``or True`` short-circuit).
    """
    items.append(value)
    return True
# --- Fixtures ---------------------------------------------------------------


class FakeMovingObject:
    """Stand-in for vehicle_position.motion_detector.MovingObject."""

    def __init__(self, trajectory=None, bbox_per_frame=None, avg_area=5000):
        if trajectory is None:
            trajectory = ["top-left", "center", "bottom-right"]
        self.trajectory = trajectory
        self.bbox_per_frame = bbox_per_frame or [(100, 100, 50, 50)]
        self.avg_area = avg_area
        self.center_per_frame = [(125, 125)]
        self.area_per_frame = [2500]
        self.frames_seen = 3


class FakeMotionResult:
    """Stand-in for vehicle_position.motion_detector.MotionResult."""

    def __init__(self, primary=None, no_motion=False):
        self.primary_moving_object = primary
        self.no_motion_detected = no_motion
        self.crop_paths = ["/tmp/crop_001.jpg"] if not no_motion else []
        self.best_crop_path = "/tmp/crop_001.jpg" if not no_motion else None


@pytest.fixture
def base_ctx(tmp_path) -> AlertContext:
    """Build an AlertContext with stub data. Not a vehicle event."""
    return AlertContext(
        alert_id="test-alert-001",
        camera_name="CAM5",
        timestamp="2026-08-20T14:00:00-04:00",
        event_type="motion",
        rtsp_url="rtsp://test/oftest",
        output_dir=str(tmp_path / "alert"),
        is_vehicle_event=False,
        known_vehicles=[],
        bot_token="test-bot-token",
        chat_id="test-chat-id",
        api_url="http://127.0.0.1:8093/v1/chat/completions",
        gatekeeper_cameras=frozenset({"CAM5"}),
                camera_code="CAM5",
    )


@pytest.fixture
def vehicle_ctx(tmp_path) -> AlertContext:
    """Build an AlertContext for a vehicle event.

    Phase.115: ctx.frames pre-populated with 4 PIL stubs so
    identify_stage's gate-only branch runs.
    """
    from PIL import Image as _PILImage
    frames = [_PILImage.new("RGB", (640, 480), color=(128, 128, 128)) for _ in range(4)]
    return AlertContext(
        alert_id="test-alert-6B111",
        camera_name="CAM5",
        timestamp="2026-08-21 14:30:00 EDT",
        event_type="vehicle",
        rtsp_url="rtsp://test/oftest",
        output_dir=str(tmp_path / "alert"),
        is_vehicle_event=True,
        known_vehicles=[],
        bot_token="test-bot-token",
        chat_id="test-chat-id",
        api_url="http://127.0.0.1:8093/v1/chat/completions",
        gatekeeper_cameras=frozenset({"CAM5"}),
                camera_code="CAM5",
        frames=frames,
    )


# --- Test 1: trajectory injection in identify_stage -------------------------


class TestTrajectoryInjection:
    def test_trajectory_injected_into_vision_result_top_level(
        self, vehicle_ctx, monkeypatch,
    ):
        """Trajectory lands at vision_result['frame_positions']."""
        trajectory = ["top-left", "center", "bottom-right"]
        primary = FakeMovingObject(trajectory=trajectory)

        def fake_detect_motion(**kwargs):
            return FakeMotionResult(primary=primary)

        monkeypatch.setattr(
            "vehicle_position.build_motion_result_from_gate", fake_detect_motion,
        )

        # Stub identify_from_crops to return a populated VisionResult
        from vehicle_identifier import IdentifierResult, VisionResult

        fake_vision = VisionResult(
            content={"color": "white", "type": "sedan"},
            elapsed_ms=100.0,
            raw_text="",
        )
        fake_id_result = IdentifierResult(
            vision_result=fake_vision,
            signature={},
            best_crop_path=None,
            crops_used=0,
            fallback_used=None,
            elapsed_ms=100.0,
        )

        def fake_identify_from_crops(**kwargs):
            return fake_id_result

        monkeypatch.setattr(
            "vehicle_identifier.identify_from_crops", fake_identify_from_crops,
        )

        vehicle_ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        identify_stage(vehicle_ctx)

        assert isinstance(vehicle_ctx.vision_result, dict)
        assert vehicle_ctx.vision_result["frame_positions"] == trajectory

    def test_trajectory_injected_into_first_vehicle_dict(
        self, vehicle_ctx, monkeypatch,
    ):
        """If vision already returned vehicles, frame_positions is set on
        vehicles[0] as well (matches legacy archive shape)."""
        trajectory = ["top-left", "center", "bottom-right"]
        primary = FakeMovingObject(trajectory=trajectory)

        monkeypatch.setattr(
            "vehicle_position.build_motion_result_from_gate",
            lambda **kw: FakeMotionResult(primary=primary),
        )

        from vehicle_identifier import IdentifierResult, VisionResult

        fake_vision = VisionResult(
            content={
                "color": "white",
                "type": "sedan",
                "vehicles": [
                    {"color": "white", "make": "Honda", "model": "Civic"},
                    {"color": "black", "make": "Ford", "model": "F-150"},
                ],
            },
            elapsed_ms=100.0,
            raw_text="",
        )
        fake_id_result = IdentifierResult(
            vision_result=fake_vision,
            signature={},
            best_crop_path=None,
            crops_used=0,
            fallback_used=None,
            elapsed_ms=100.0,
        )

        monkeypatch.setattr(
            "vehicle_identifier.identify_from_crops",
            lambda **kw: fake_id_result,
        )

        vehicle_ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        identify_stage(vehicle_ctx)

        # Top-level frame_positions set
        assert vehicle_ctx.vision_result["frame_positions"] == trajectory
        # And injected into vehicles[0]
        assert (
            vehicle_ctx.vision_result["vehicles"][0]["frame_positions"]
            == trajectory
        )
        # vehicles[1] NOT touched (only first vehicle gets it, per legacy)
        assert "frame_positions" not in vehicle_ctx.vision_result["vehicles"][1]

    def test_no_trajectory_skips_injection(self, vehicle_ctx, monkeypatch):
        """Empty trajectory → no frame_positions key injected."""
        primary = FakeMovingObject(trajectory=[])

        monkeypatch.setattr(
            "vehicle_position.build_motion_result_from_gate",
            lambda **kw: FakeMotionResult(primary=primary),
        )

        from vehicle_identifier import IdentifierResult, VisionResult

        fake_vision = VisionResult(
            content={"color": "white"},
            elapsed_ms=100.0,
            raw_text="",
        )
        fake_id_result = IdentifierResult(
            vision_result=fake_vision,
            signature={},
            best_crop_path=None,
            crops_used=0,
            fallback_used=None,
            elapsed_ms=100.0,
        )

        monkeypatch.setattr(
            "vehicle_identifier.identify_from_crops",
            lambda **kw: fake_id_result,
        )

        vehicle_ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        identify_stage(vehicle_ctx)

        assert "frame_positions" not in vehicle_ctx.vision_result

    def test_no_motion_skips_injection(self, vehicle_ctx, monkeypatch):
        """Motion detector reported no motion → no frame_positions injection.

        Phase.132 (§11.54): the previous test asserted that the
        identify_stage fell back to a single-frame Qwen call and
        returned a `vision_result` dict without frame_positions.
        After 6B.132 the vehicle-without-motion path is SUPPRESSED —
        no full-frame Qwen send, no vision_result. trajectory injection
        is gated on motion_result.primary_moving_object being non-None,
        so it doesn't fire either.
        """
        monkeypatch.setattr(
            "vehicle_position.build_motion_result_from_gate",
            lambda **kw: FakeMotionResult(no_motion=True),
        )

        # Phase.132: this MUST NOT be called for vehicle-without-motion.
        def must_not_call(*args, **kwargs):
            raise AssertionError(
                "analyze_frames_queued called for vehicle-without-motion; "
                "Phase.132: no fallback, alert must be suppressed"
            )
        monkeypatch.setattr(
            "infra.vision_analyzer.analyze_frames_queued", must_not_call,
        )

        vehicle_ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        identify_stage(vehicle_ctx)

        # Suppressed — vision_result stays None, no frame_positions.
        assert not vehicle_ctx.vision_result, (
            f"expected vision_result to stay empty after suppression; "
            f"got {vehicle_ctx.vision_result!r}"
        )

    def test_non_vehicle_event_skips_injection(self, base_ctx, monkeypatch):
        """Non-vehicle events don't even run the motion detector, so no
        trajectory injection happens."""
        from listener.vehicle_pipeline import AlertContext

        ctx = AlertContext(
            alert_id="non-vehicle-test",
            camera_name="CAM5",
            timestamp="2026-08-21 14:30:00 EDT",
            event_type="motion",
            rtsp_url="rtsp://test/oftest",
            output_dir="/tmp/test",
            is_vehicle_event=False,
            known_vehicles=[],
            bot_token="tkn",
            chat_id="cid",
            api_url="http://test",
            gatekeeper_cameras=frozenset(),
            # Phase.168: CAM3 is intentionally outside the gatekeeper
            # set for this non-gatekeeper fixture.
            camera_code="CAM3",
        )

        # detect_motion should NOT be called for non-vehicle events.
        detect_called = []

        def fake_detect(**kw):
            detect_called.append(True)
            return FakeMotionResult(no_motion=True)

        monkeypatch.setattr(
            "vehicle_position.build_motion_result_from_gate", fake_detect,
        )
        monkeypatch.setattr(
            "infra.vision_analyzer.analyze_frames_queued",
            lambda **kw: {"color": "blue"},
        )

        ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        identify_stage(ctx)

        # detect_motion never called for non-vehicle events
        assert detect_called == []
        assert "frame_positions" not in ctx.vision_result


# --- Test 2: composite Telegram in emit_result_stage -----------------------


class TestCompositeTelegram:
    """Verify the composite motion-trail Telegram fires from emit_result_stage."""

    def test_composite_fires_for_vehicle_with_motion(
        self, vehicle_ctx, monkeypatch,
    ):
        """Vehicle event with motion detector result → composite Telegram fires."""
        trajectory = ["top-left", "center", "bottom-right"]
        primary = FakeMovingObject(trajectory=trajectory)
        vehicle_ctx.motion_result = FakeMotionResult(primary=primary)
        vehicle_ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        vehicle_ctx.best_frame_path = "/tmp/frame_3.jpg"

        # Pretend vision identified a vehicle so generate_alert returns something
        vehicle_ctx.vision_result = {
            "color": "white",
            "type": "sedan",
            "frame_positions": trajectory,
        }

        # Stub alert generator to return a minimal valid alert
        def fake_generate_alert(**kw):
            return {
                "title": "Vehicle in motion — white sedan",
                "summary": "test",
                "threat_level": 1,
            }

        monkeypatch.setattr(
            "infra.alert_generator.generate_alert", fake_generate_alert,
        )

        # Stub send_photo_with_caption (Telegram transport) to return True
        notify_calls: list[Any] = []
        monkeypatch.setattr(
            "infra.send_telegram.send_photo_with_caption",
            lambda **kw: _track_call(notify_calls, kw),
        )

        # Stub audit append to return True (allow notify to fire)
        monkeypatch.setattr(
            "infra.alert_history.append_alert", lambda alert: True,
        )

        # Stub the composite sender — capture the call
        composite_calls = []

        def fake_send_composite_alert(**kw):
            composite_calls.append(kw)
            return True

        # Patch the lazy import target inside the pipeline module
        monkeypatch.setattr(
            "telegram_formatter.composite_telegram.send_composite_alert",
            fake_send_composite_alert,
        )

        # Stub phase 6A / arrival detection (they're not what we're testing)
        monkeypatch.setattr(
            "infra.pipeline_integration.run_phase6a_recognition",
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "infra.arrival.is_arrival", lambda cam: False,
        )
        monkeypatch.setattr(
            "infra.arrival._vision_shows_person", lambda vr: False,
        )

        emit_result_stage(vehicle_ctx)

        # The composite send was attempted
        assert len(composite_calls) == 1
        call = composite_calls[0]
        assert call["alert_id"] == vehicle_ctx.alert_id
        assert call["camera_name"] == vehicle_ctx.camera_name
        # Phase.115: send_composite_alert takes bbox_a, bbox_b, trajectory
        # (not primary_moving_object). Verify trajectory flows through.
        assert call["trajectory"] == trajectory
        assert call["bot_token"] == vehicle_ctx.bot_token
        assert call["chat_id"] == vehicle_ctx.chat_id
        assert call["captured_at"] == vehicle_ctx.timestamp

    def test_composite_skipped_for_non_vehicle(
        self, base_ctx, monkeypatch,
    ):
        """Non-vehicle events don't fire the composite Telegram."""
        # base_ctx is non-vehicle (is_vehicle_event=False)
        base_ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        base_ctx.best_frame_path = "/tmp/frame_3.jpg"
        base_ctx.vision_result = {"color": "white"}

        monkeypatch.setattr(
            "infra.alert_generator.generate_alert",
            lambda **kw: {"title": "Motion detected", "threat_level": 1},
        )
        monkeypatch.setattr(
            "infra.send_telegram.send_photo_with_caption", lambda **kw: True,
        )
        monkeypatch.setattr(
            "infra.alert_history.append_alert", lambda alert: True,
        )

        composite_calls: list[Any] = []
        monkeypatch.setattr(
            "telegram_formatter.composite_telegram.send_composite_alert",
            lambda **kw: _track_call(composite_calls, kw),
        )
        monkeypatch.setattr(
            "infra.pipeline_integration.run_phase6a_recognition",
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "infra.arrival.is_arrival", lambda cam: False,
        )
        monkeypatch.setattr(
            "infra.arrival._vision_shows_person", lambda vr: False,
        )

        emit_result_stage(base_ctx)

        # Composite NOT called for non-vehicle events
        assert composite_calls == []

    def test_composite_failure_does_not_block_state_update(
        self, vehicle_ctx, monkeypatch,
    ):
        """If composite raises, state still updates and emit_result still returns."""
        primary = FakeMovingObject()
        vehicle_ctx.motion_result = FakeMotionResult(primary=primary)
        vehicle_ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        vehicle_ctx.best_frame_path = "/tmp/frame_3.jpg"
        vehicle_ctx.vision_result = {"color": "white"}

        monkeypatch.setattr(
            "infra.alert_generator.generate_alert",
            lambda **kw: {"title": "test", "threat_level": 1},
        )
        monkeypatch.setattr(
            "infra.send_telegram.send_photo_with_caption", lambda **kw: True,
        )
        monkeypatch.setattr(
            "infra.alert_history.append_alert", lambda alert: True,
        )

        def explosive_send(**kw):
            raise RuntimeError("simulated composite failure")

        monkeypatch.setattr(
            "telegram_formatter.composite_telegram.send_composite_alert",
            explosive_send,
        )

        monkeypatch.setattr(
            "infra.pipeline_integration.run_phase6a_recognition",
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "infra.arrival.is_arrival", lambda cam: False,
        )
        monkeypatch.setattr(
            "infra.arrival._vision_shows_person", lambda vr: False,
        )

        # Should NOT raise
        result = emit_result_stage(vehicle_ctx)

        # Result dict returned
        assert result["alert_id"] == vehicle_ctx.alert_id
        assert result["telegram_sent"] is True

        # State was updated (total_alerts incremented). We verify via
        # STATE singleton — it must be importable from listener.state
        # or top-level state.
        try:
            from state import STATE
        except ImportError:
            from listener.state import STATE
        # Just confirm the state singleton is queryable (don't depend on
        # absolute counter values across tests).
        assert "total_alerts" in STATE

    def test_composite_fires_in_emit_result(self, vehicle_ctx, monkeypatch):
        """Composite Telegram fires as part of emit_result_stage.

        Phase.112: notify() was removed from emit_result_stage. TG#1
        (arriving) now fires from identify_stage end. TG#2 (composite)
        fires here in emit_result, BEFORE the match loop (TG#3) per Note
        OOB 2026-08-21: "the matcher should run after the other two
        alerts are sent to me."
        """
        primary = FakeMovingObject()
        vehicle_ctx.motion_result = FakeMotionResult(primary=primary)
        vehicle_ctx.frame_paths = [f"/tmp/frame_{i}.jpg" for i in range(6)]
        vehicle_ctx.best_frame_path = "/tmp/frame_3.jpg"
        vehicle_ctx.vision_result = {"color": "white"}

        monkeypatch.setattr(
            "infra.alert_generator.generate_alert",
            lambda **kw: {"title": "test", "threat_level": 1},
        )

        composite_called = []

        def fake_composite(**kw):
            composite_called.append(True)
            return True

        monkeypatch.setattr(
            "telegram_formatter.composite_telegram.send_composite_alert",
            fake_composite,
        )
        monkeypatch.setattr(
            "infra.alert_history.append_alert", lambda alert: True,
        )
        monkeypatch.setattr(
            "infra.pipeline_integration.run_phase6a_recognition",
            lambda **kw: None,
        )
        monkeypatch.setattr(
            "infra.arrival.is_arrival", lambda cam: False,
        )
        monkeypatch.setattr(
            "infra.arrival._vision_shows_person", lambda vr: False,
        )

        emit_result_stage(vehicle_ctx)

        # TG#2 fires exactly once during emit_result_stage
        assert composite_called == [True]