"""Tests for listener.matcher_adapters — adapter between single_pipeline
and the existing per-class pipelines.

Verifies:
  - Each adapter builds the correct context object.
  - The adapter invokes the correct pipeline entry point.
  - Returns a dict (possibly empty) on success.
  - Returns an error sentinel dict on pipeline exception.
  - Each adapter is reachable via SinglePipelineMatchers.{vehicle,person,animal}.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from listener import matcher_adapters


@pytest.fixture
def matchers():
    """Build a SinglePipelineMatchers with non-empty kwargs."""
    return matcher_adapters.SinglePipelineMatchers(
        alert_frame_dir="/tmp/frames",
        camera_name="Front Porch",
        timestamp="2026-09-02 12:34:56",
        rtsp_url="rtsp://10.0.0.1:554/stream",
        vision_api_url="http://127.0.0.1:8093/v1/chat/completions",
        bot_token="dummy",
        chat_id="-123456789",
        gatekeeper_cameras=frozenset({"CAM1", "CAM2"}),
        camera_code_lookup=lambda name: "CAM1" if name == "Front Porch" else "CAMX",
        known_vehicles=[],
    )


class TestVehicleAdapter:
    """vehicle() calls listener.vehicle_pipeline.process_alert."""

    def test_vehicle_calls_process_alert(self, matchers) -> None:
        with patch("listener.vehicle_pipeline.process_alert") as mock_pa:
            mock_pa.return_value = {"alert_id": "x", "telegram_sent": True}
            result = matchers.vehicle(
                classify=MagicMock(),
                call2_response={"make": "Toyota"},
                crop_a="/tmp/A.jpg",
                crop_b="/tmp/B.jpg",
                alert_id="alert-1",
            )
        mock_pa.assert_called_once()
        assert result["alert_id"] == "x"
        assert result["telegram_sent"] is True

    def test_vehicle_builds_alertcontext_with_camera_code(self, matchers) -> None:
        """AlertContext.camera_code must be set from camera_code_lookup."""
        with patch("listener.vehicle_pipeline.process_alert") as mock_pa:
            mock_pa.return_value = {"alert_id": "x"}
            matchers.vehicle(
                classify=None,
                call2_response=None,
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-1",
            )
        ctx = mock_pa.call_args.args[0]
        assert ctx.camera_code == "CAM1"
        assert ctx.camera_name == "Front Porch"
        assert ctx.alert_id == "alert-1"
        assert ctx.event_type == "vehicle"

    def test_vehicle_returns_empty_dict_when_pipeline_returns_none(self, matchers) -> None:
        with patch("listener.vehicle_pipeline.process_alert") as mock_pa:
            mock_pa.return_value = None
            result = matchers.vehicle(
                classify=None,
                call2_response=None,
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-1",
            )
        assert result == {}

    def test_vehicle_returns_error_dict_on_exception(self, matchers) -> None:
        with patch("listener.vehicle_pipeline.process_alert") as mock_pa:
            mock_pa.side_effect = RuntimeError("kaboom")
            result = matchers.vehicle(
                classify=None,
                call2_response=None,
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-1",
            )
        assert result["alert_id"] == "alert-1"
        assert result["telegram_sent"] is False
        assert "kaboom" in result["error"]


class TestPersonAdapter:
    """person() calls listener.person_event_pipeline.process_person_event."""

    def test_person_calls_process_person_event(self, matchers) -> None:
        with patch("listener.person_event_pipeline.process_person_event") as mock_ppe:
            mock_ppe.return_value = {"alert_id": "p1", "telegram_sent": True}
            result = matchers.person(
                classify=MagicMock(),
                call2_response={"better_crop": "crop_a"},
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-2",
            )
        mock_ppe.assert_called_once()
        assert result["alert_id"] == "p1"

    def test_person_builds_personcontext(self, matchers) -> None:
        with patch("listener.person_event_pipeline.process_person_event") as mock_ppe:
            mock_ppe.return_value = {}
            matchers.person(
                classify=None,
                call2_response=None,
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-2",
            )
        ctx = mock_ppe.call_args.args[0]
        assert ctx.alert_id == "alert-2"
        assert ctx.camera_name == "Front Porch"
        assert ctx.event_type == "person"

    def test_person_returns_error_dict_on_exception(self, matchers) -> None:
        with patch("listener.person_event_pipeline.process_person_event") as mock_ppe:
            mock_ppe.side_effect = ValueError("oops")
            result = matchers.person(
                classify=None,
                call2_response=None,
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-2",
            )
        assert result["telegram_sent"] is False
        assert "oops" in result["error"]


class TestAnimalAdapter:
    """animal() calls listener.animal_event_pipeline.process_animal_event."""

    def test_animal_calls_process_animal_event(self, matchers) -> None:
        with patch("listener.animal_event_pipeline.process_animal_event") as mock_pae:
            mock_pae.return_value = {"alert_id": "a1", "telegram_sent": True}
            result = matchers.animal(
                classify=MagicMock(),
                call2_response={"species": "dog"},
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-3",
            )
        mock_pae.assert_called_once()
        assert result["alert_id"] == "a1"

    def test_animal_builds_animalcontext(self, matchers) -> None:
        with patch("listener.animal_event_pipeline.process_animal_event") as mock_pae:
            mock_pae.return_value = {}
            matchers.animal(
                classify=None,
                call2_response=None,
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-3",
            )
        ctx = mock_pae.call_args.args[0]
        assert ctx.alert_id == "alert-3"
        assert ctx.event_type == "animal"

    def test_animal_returns_error_dict_on_exception(self, matchers) -> None:
        with patch("listener.animal_event_pipeline.process_animal_event") as mock_pae:
            mock_pae.side_effect = OSError("disk full")
            result = matchers.animal(
                classify=None,
                call2_response=None,
                crop_a="/A",
                crop_b="/B",
                alert_id="alert-3",
            )
        assert "disk full" in result["error"]


class TestAdapterSurface:
    """Verify the dataclass exposes the three methods that single_pipeline calls."""

    def test_has_vehicle_method(self, matchers) -> None:
        assert callable(getattr(matchers, "vehicle", None))

    def test_has_person_method(self, matchers) -> None:
        assert callable(getattr(matchers, "person", None))

    def test_has_animal_method(self, matchers) -> None:
        assert callable(getattr(matchers, "animal", None))
