"""
test_pipeline.py — Tests for listener.pipeline (stages 1-12).

Tests behaviors across all stages:
  1. run returns 'suppressed' when cooldown fires (early exit).
  2. run returns 'dropped' when gate suppresses.
  3. run returns 'ok' with gate, vm1, tg1, vm2, tg2, match, tg3 when flow completes.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from infra.alert_artifacts import AlertArtifacts
from infra.gate import GateVerdict
from listener.pipeline import run


def _make_gate_verdict(
    classification="vehicle",
    class_label="car",
    confidence=0.85,
    reason="high_conf_vehicle",
    pairwise_diff_path=None,
):
    """Build a mock GateVerdict for pipeline tests."""
    v = MagicMock(spec=GateVerdict)
    v.classification = classification
    v.class_label = class_label
    v.confidence = confidence
    v.reason = reason
    v.crop_a = None
    v.crop_b = None
    v.pairwise_diff_path = pairwise_diff_path
    v.frames = []
    v.is_none = lambda: classification == "none"
    return v


def _make_artifacts():
    """Build a mock AlertArtifacts for pipeline tests."""
    a = MagicMock(spec=AlertArtifacts)
    a.crop_a_path = "/mock/a.png"
    a.crop_b_path = "/mock/b.png"
    return a


def _make_alert(**kwargs):
    """Build a sample alert dict."""
    return {
        "id": "evt-test-001",
        "camera_id": "CAM1",
        "camera_label": "Front Gate",
        "classification": kwargs.get("classification", "vehicle"),
        "frames": kwargs.get(
            "frames", ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg", "/tmp/f4.jpg"]
        ),
    }


class TestPipelineRun:
    """Tests for run() — stages 1-7."""

    def test_run_suppressed_by_cooldown(self):
        """run returns 'suppressed' when cooldown.should_suppress returns True."""
        alert = _make_alert()

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = True

        with patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown):
            result = run(alert)

        assert result["status"] == "suppressed"
        assert result["camera_id"] == "CAM1"
        assert result["classification"] == "vehicle"

    def test_run_dropped_by_gate(self):
        """run returns 'dropped' when gate returns classification='none'."""
        alert = _make_alert()
        gate_v = _make_gate_verdict(classification="none", reason="no_object_detected")

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

    def test_run_ok_with_full_flow(self, tmp_path):
        """run returns 'ok' with gate, vm1, tg1, vm2, tg2, match, tg3."""
        alert = _make_alert()
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "Detected: vehicle", "photos": []}
        vm2_result = {"class_confirmed": "vehicle", "distinctive_features": []}
        tg2 = {"caption": "Camera: Front Gate", "photos": []}
        match_result = {"matched": False}
        tg3 = {"caption": "Status: unrecognized vehicle", "photos": []}

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
        ):
            result = run(alert)

        assert result["status"] == "ok"
        assert result["camera_id"] == "CAM1"
        assert result["classification"] == "vehicle"
        assert "frames" in result
        assert "gate" in result
        assert "vm1_result" in result
        assert "tg1" in result
        assert "vm2_result" in result
        assert "tg2" in result
        assert "match_result" in result
        assert "tg3" in result
        assert result["vm1_result"] == vm1_result
        assert result["tg1"] == tg1
        assert result["vm2_result"] == vm2_result
        assert result["tg2"] == tg2
        assert result["match_result"] == match_result
        assert result["tg3"] == tg3

    def test_run_raises_on_empty_frames(self):
        """run raises RuntimeError when alert has no frames key."""
        alert = {"camera_id": "CAM2", "classification": "person"}

        with pytest.raises(RuntimeError, match="no frames captured from camera CAM2"):
            run(alert)

    def test_run_raises_on_missing_pairwise_diff(self):
        """run raises RuntimeError when gate produces no pairwise_diff."""
        alert = _make_alert()
        gate_v = _make_gate_verdict(pairwise_diff_path=None)

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        with (
            pytest.raises(
                RuntimeError, match="gate produced no pairwise_diff for alert evt-test-001"
            ),
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()),
            patch("listener.pipeline.verify_class", return_value={"class": "vehicle"}),
        ):
            run(alert)

    def test_record_hit_called_on_success(self, tmp_path):
        """record_hit is called on the cooldown when the pipeline succeeds."""
        alert = _make_alert()
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "test", "photos": []}
        vm2_result = {"class_confirmed": "vehicle", "distinctive_features": []}
        tg2 = {"caption": "test", "photos": []}
        tg3 = {"caption": "test", "photos": []}

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
        ):
            run(alert)

        mock_cooldown.record_hit.assert_called_once_with("CAM1", "vehicle")

    def test_prepare_alert_artifacts_called_with_camera_and_alert_id(self, tmp_path):
        """prepare_alert_artifacts receives gate_verdict, frames, and output_dir from run()."""
        from unittest.mock import MagicMock

        import infra.paths as _paths_mod

        orig_project_root = _paths_mod.PROJECT_ROOT
        _paths_mod.PROJECT_ROOT = str(tmp_path)

        try:
            alert = _make_alert()
            alert["camera_id"] = "CAM3"
            alert["id"] = "alert-abc123"
            diff_path = str(tmp_path / "diff.png")
            Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
            gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

            mock_cooldown = MagicMock()
            mock_cooldown.should_suppress.return_value = False

            vm1_result = {"class": "vehicle", "confidence": 0.92}
            tg1 = {"caption": "Detected: vehicle", "photos": []}
            vm2_result = {"class_confirmed": "vehicle", "distinctive_features": []}
            tg2 = {"caption": "Camera: Front Gate", "photos": []}
            tg3 = {"caption": "Status: unrecognized", "photos": []}

            expected_output_dir = (tmp_path / "data" / "frames" / "CAM3" / "alert-abc123")

            with (
                patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
                patch("listener.pipeline.run_gate", return_value=gate_v),
                patch(
                    "listener.pipeline.prepare_alert_artifacts",
                    return_value=_make_artifacts(),
                ) as mock_artifacts,
                patch("listener.pipeline.verify_class", return_value=vm1_result),
                patch("listener.pipeline.build_alert_message", return_value=tg1),
                patch("listener.pipeline.detail_class", return_value=vm2_result),
                patch("listener.pipeline.build_detail_message", return_value=tg2),
                patch("listener.pipeline._load_candidates", return_value=[]),
                patch("listener.pipeline.build_match_message", return_value=tg3),
            ):
                run(alert)

            mock_artifacts.assert_called_once()
            call_kwargs = mock_artifacts.call_args
            assert call_kwargs.kwargs.get("gate_verdict") == gate_v
            actual = os.path.normpath(str(call_kwargs.kwargs.get("output_dir")))
            expected = os.path.normpath(str(expected_output_dir))
            assert actual == expected
        finally:
            _paths_mod.PROJECT_ROOT = orig_project_root
