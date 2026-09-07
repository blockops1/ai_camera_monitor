"""
test_pipeline.py — Tests for listener.pipeline (stages 1-7).

Tests behaviors across all stages:
  1. run returns 'suppressed' when cooldown fires (early exit).
  2. run returns 'dropped' when gate suppresses.
  3. run returns 'ok' with gate, vm1, tg1 when flow completes.
"""

from unittest.mock import MagicMock, patch

from listener.pipeline import run
from infra.gate import GateVerdict


def _make_gate_verdict(decision="vehicle", class_label="car", confidence=0.85, reason="high_conf_vehicle"):
    """Build a mock GateVerdict for pipeline tests."""
    v = MagicMock(spec=GateVerdict)
    v.decision = decision
    v.class_label = class_label
    v.confidence = confidence
    v.reason = reason
    v.crop_a = None
    v.crop_b = None
    v.pairwise_diff_path = None
    return v


def _make_alert(**kwargs):
    """Build a sample alert dict."""
    return {
        "id": "evt-test-001",
        "camera_id": "CAM1",
        "camera_label": "Front Gate",
        "classification": kwargs.get("classification", "vehicle"),
        "frames": kwargs.get("frames", ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg", "/tmp/f4.jpg"]),
    }


class TestPipelineRun:
    """Tests for run() — stages 1-7."""

    def test_run_suppressed_by_cooldown(self):
        """run returns 'suppressed' when cooldown.should_suppress returns True."""
        alert = _make_alert()

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = True

        with patch(
            "listener.pipeline.PipelineCooldown", return_value=mock_cooldown
        ):
            result = run(alert)

        assert result["status"] == "suppressed"
        assert result["camera_id"] == "CAM1"
        assert result["classification"] == "vehicle"

    def test_run_dropped_by_gate(self):
        """run returns 'dropped' when gate returns decision='suppress'."""
        alert = _make_alert()
        gate_v = _make_gate_verdict(decision="suppress", reason="no_object_detected")

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
        ):
            result = run(alert)

        assert result["status"] == "dropped"
        assert result["camera_id"] == "CAM1"
        assert result["classification"] == "vehicle"
        assert result["reason"] == "no_object_detected"
        assert "gate" in result

    def test_run_ok_with_full_flow(self):
        """run returns 'ok' with gate, vm1_result, and tg1 when flow completes."""
        alert = _make_alert()
        gate_v = _make_gate_verdict()

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "Detected: vehicle", "photos": []}

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
        ):
            result = run(alert)

        assert result["status"] == "ok"
        assert result["camera_id"] == "CAM1"
        assert result["classification"] == "vehicle"
        assert "frames" in result
        assert "gate" in result
        assert "vm1_result" in result
        assert "tg1" in result
        assert result["vm1_result"] == vm1_result
        assert result["tg1"] == tg1

    def test_run_ok_with_empty_frames(self):
        """run returns 'ok' with empty frames when alert has no frames key."""
        alert = {"camera_id": "CAM2", "classification": "person"}
        gate_v = _make_gate_verdict(class_label="person")

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        vm1_result = {"class": "person", "confidence": 0.78}
        tg1 = {"caption": "Detected: person", "photos": []}

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
        ):
            result = run(alert)

        assert result["status"] == "ok"
        assert result["camera_id"] == "CAM2"
        assert result["classification"] == "person"
        assert result["frames"] == []

    def test_record_hit_called_on_success(self):
        """record_hit is called on the cooldown when the pipeline succeeds."""
        alert = _make_alert()
        gate_v = _make_gate_verdict()

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "test", "photos": []}

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
        ):
            run(alert)

        mock_cooldown.record_hit.assert_called_once_with("CAM1", "vehicle")
