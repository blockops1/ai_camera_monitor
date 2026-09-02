"""§11.115.11 tests: vehicle identify_stage cascade fast-path.

When matcher_adapters.vehicle() pre-populates AlertContext.vision_result
from the cascade's call2_response, identify_stage must skip the
internal identify_from_crops (Qwen) call entirely. The rest of the
pipeline (motion detector, match, alert emit) runs as normal.

These tests pin the contract:
  - vision_result={...cascade_shape...} → identify_from_crops NOT called
  - vision_result=None / empty → identify_from_crops IS called (legacy)
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from listener.vehicle_pipeline.context import AlertContext


def _make_cascade_call2():
    """Sample call2_response from the §11.115.4 vehicle call-2 schema."""
    return {
        "vehicles": [
            {
                "vehicle_id": "v1",
                "color": "blue",
                "body_style_hint": "pickup",
                "make": "Ford",
                "model": "F-150",
                "vehicle_features": {
                    "wheel_style": "alloy",
                    "wheel_arch": "standard",
                    "wheel_color": "silver",
                    "roofline_style": "crew_cab",
                    "front_grille_style": "mesh",
                    "headlight_signature": "LED bar",
                    "rear_lights_signature": "standard",
                    "tailgate_type": "standard",
                    "badge_text_readable": "F-150",
                    "window_tint": "light",
                    "cab_marker_lights": False,
                    "bed_cover": "none",
                },
                "description": "Blue Ford F-150 pickup with chrome trim",
            }
        ],
        "primary_vehicle_index": 0,
        "confidence": 0.88,
        "notable_details": ["chrome trim", "muddy tires"],
    }


def _make_alert_context(
    *,
    vision_result=None,
    motion_result=None,
):
    return AlertContext(
        alert_id="test-alert",
        camera_name="Driveway",
        camera_code="CAM3",
        timestamp="2026-09-02T12:00:00-04:00",
        event_type="vehicle",
        rtsp_url="rtsp://example/stream",
        output_dir="/tmp/test-frames/",
        is_vehicle_event=True,
        known_vehicles=[],
        bot_token="dummy-token",
        chat_id="dummy-chat",
        api_url="http://127.0.0.1:8093/v1/chat/completions",
        gatekeeper_cameras=frozenset(),
        vision_result=vision_result,
        frame_paths=["/tmp/frame_1.jpg", "/tmp/frame_2.jpg"],
        motion_result=motion_result,
    )


def _make_motion_result(no_motion=False):
    """Stub motion_result with crop_paths populated."""
    return SimpleNamespace(
        no_motion_detected=no_motion,
        primary_moving_object=SimpleNamespace(
            trajectory=[[10, 20], [15, 25]],
            avg_area=1000,
        ),
        crop_paths=["/tmp/crop_a.jpg", "/tmp/crop_b.jpg"],
    )


# ---------------------------------------------------------------------------
# §11.115.11: cascade fast-path in identify_stage
# ---------------------------------------------------------------------------


class TestIdentifyStageCascadeFastPath(unittest.TestCase):
    def test_cascade_provided_vision_skips_qwen_call(self):
        """vision_result={vehicles[]} → identify_from_crops NOT called."""
        from listener.vehicle_pipeline import identify as identify_mod

        cascade = _make_cascade_call2()
        ctx = _make_alert_context(
            vision_result=cascade,
            motion_result=_make_motion_result(),
        )

        with patch(
            "vehicle_identifier.identify_from_crops"
        ) as mock_id, patch.object(
            identify_mod, "_coerce_vision_result"
        ) as mock_coerce:
            identify_mod.identify_stage(ctx)

        mock_id.assert_not_called()
        # ctx.vision_result must still be the cascade dict
        self.assertEqual(ctx.vision_result["vehicles"][0]["make"], "Ford")
        self.assertEqual(ctx.vision_result["primary_vehicle_index"], 0)
        # _coerce_vision_result is a no-op when id_result is None
        # (the cascade didn't set id_result), but it should still be
        # called once by identify_stage? Actually no — it's only
        # called inside the legacy identify_from_crops try block.
        # So coercion should NOT be called either.
        mock_coerce.assert_not_called()

    def test_legacy_no_vision_calls_qwen(self):
        """vision_result=None → identify_from_crops IS called (legacy)."""
        from listener.vehicle_pipeline import identify as identify_mod

        ctx = _make_alert_context(
            vision_result=None,
            motion_result=_make_motion_result(),
        )

        with patch(
            "vehicle_identifier.identify_from_crops"
        ) as mock_id:
            # Return a fake IdentifierResult so identify_stage can
            # carry on (motion path needs id_result to exist).
            mock_id.return_value = SimpleNamespace(
                vision_result=_make_cascade_call2(),
                to_dict=lambda: _make_cascade_call2(),
            )
            identify_mod.identify_stage(ctx)

        mock_id.assert_called_once()

    def test_cascade_empty_dict_still_runs_qwen(self):
        """vision_result={} (no vehicles key) → Qwen call still runs."""
        from listener.vehicle_pipeline import identify as identify_mod

        ctx = _make_alert_context(
            vision_result={},  # empty dict, NOT the cascade shape
            motion_result=_make_motion_result(),
        )

        with patch(
            "vehicle_identifier.identify_from_crops"
        ) as mock_id:
            mock_id.return_value = SimpleNamespace(
                vision_result=_make_cascade_call2(),
            )
            identify_mod.identify_stage(ctx)

        # cascade_provided_vision requires "vehicles" key, so Qwen runs
        mock_id.assert_called_once()

    def test_cascade_provided_vision_survives_motion_detector(self):
        """Motion detector still runs even with cascade vision_result."""
        from listener.vehicle_pipeline import identify as identify_mod

        cascade = _make_cascade_call2()
        ctx = _make_alert_context(
            vision_result=cascade,
            motion_result=None,  # will be built by identify_stage
        )

        with patch(
            "vehicle_identifier.identify_from_crops"
        ) as mock_id:
            identify_mod.identify_stage(ctx)

        # Qwen NOT called
        mock_id.assert_not_called()
        # Cascade vision_result preserved
        self.assertEqual(ctx.vision_result["confidence"], 0.88)


# ---------------------------------------------------------------------------
# matcher_adapters.vehicle(): threads call2_response into AlertContext
# ---------------------------------------------------------------------------


class TestMatcherAdapterVehicleThreadsCall2(unittest.TestCase):
    def test_vehicle_adapter_threads_call2_into_vision_result(self):
        """§11.115.11: call2_response → AlertContext.vision_result."""
        from listener.matcher_adapters import SinglePipelineMatchers

        cascade = _make_cascade_call2()
        matchers = SinglePipelineMatchers(
            alert_frame_dir="/tmp/frames",
            camera_name="Driveway",
            timestamp="2026-09-02T12:00:00-04:00",
            rtsp_url="rtsp://example/stream",
            vision_api_url="http://x",
            bot_token="dummy-token",
            chat_id="dummy-chat",
            gatekeeper_cameras=frozenset(),
            camera_code_lookup=lambda _: "CAM3",
        )

        # Capture the ctx that gets passed in to process_alert.
        captured = {}
        def fake_process_alert(ctx):
            captured["ctx"] = ctx
            return {"telegram_sent": True}

        with patch(
            "listener.vehicle_pipeline.process_alert", fake_process_alert
        ):
            matchers.vehicle(
                classify=SimpleNamespace(label="vehicle"),
                call2_response=cascade,
                crop_a="/tmp/ca.jpg",
                crop_b="/tmp/cb.jpg",
                alert_id="abc-123",
            )

        self.assertIs(captured["ctx"].vision_result, cascade)

    def test_vehicle_adapter_no_call2_keeps_vision_result_none(self):
        """§11.115.11: no call2 → vision_result=None (legacy path)."""
        from listener.matcher_adapters import SinglePipelineMatchers

        matchers = SinglePipelineMatchers(
            alert_frame_dir="/tmp/frames",
            camera_name="Driveway",
            timestamp="2026-09-02T12:00:00-04:00",
            rtsp_url="rtsp://example/stream",
            vision_api_url="http://x",
            bot_token="dummy-token",
            chat_id="dummy-chat",
            gatekeeper_cameras=frozenset(),
            camera_code_lookup=lambda _: "CAM3",
        )

        captured = {}
        def fake_process_alert(ctx):
            captured["ctx"] = ctx
            return {"telegram_sent": True}

        with patch(
            "listener.vehicle_pipeline.process_alert", fake_process_alert
        ):
            matchers.vehicle(
                classify=None,
                call2_response=None,
                crop_a="/tmp/ca.jpg",
                crop_b="/tmp/cb.jpg",
                alert_id="abc-123",
            )

        self.assertIsNone(captured["ctx"].vision_result)


if __name__ == "__main__":
    unittest.main()