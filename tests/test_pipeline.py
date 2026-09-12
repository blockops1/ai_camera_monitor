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
    top_class="car",
    top_confidence=0.85,
    reason="high_conf_vehicle",
    pairwise_diff_path=None,
):
    """Build a mock GateVerdict for pipeline tests."""
    v = MagicMock(spec=GateVerdict)
    v.classification = classification
    v.class_label = class_label
    v.confidence = confidence
    v.top_class = top_class
    v.top_confidence = top_confidence
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
        "id": kwargs.get("id", "evt-test-001"),
        "camera_id": kwargs.get("camera_id", "CAM1"),
        "camera_label": kwargs.get("camera_label", "Front Gate"),
        "classification": kwargs.get("classification", "vehicle"),
        "frames": kwargs.get(
            "frames", ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg", "/tmp/f4.jpg"]
        ),
    }


class TestPipelineRun:
    """Tests for run() — stages 1-7."""

    def test_run_suppressed_by_cooldown(self, tmp_path):
        """run returns 'dropped' with reason='cooldown_active' when cooldown fires."""
        alert = _make_alert()
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        with (
            patch("listener.pipeline.should_suppress", return_value=True),
            patch("listener.pipeline.run_gate", return_value=gate_v),
        ):
            result = run(alert)

        assert result["status"] == "dropped"
        assert result["reason"] == "cooldown_active"
        assert result["classification"] == "vehicle"

    def test_suppressed_after_gate_before_processing(self):
        """Cooldown suppression at stage 7 skips all downstream processing."""
        alert = _make_alert()

        with (
            patch("listener.pipeline.should_suppress", return_value=True),
            patch("listener.pipeline.run_gate") as mock_gate,
            patch("listener.pipeline.prepare_alert_artifacts"),
            patch("listener.pipeline.record_hit") as mock_record,
        ):
            result = run(alert)

        assert result["status"] == "dropped"
        # Gate is called (stage 4), but downstream processing is skipped
        assert mock_gate.call_count == 1
        # No downstream processing
        mock_record.assert_not_called()

    def test_run_dropped_by_gate(self):
        """run returns 'dropped' when gate returns classification='none'."""
        alert = _make_alert()
        gate_v = _make_gate_verdict(classification="none", reason="no_object_detected")

        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
        ):
            result = run(alert)

        assert result["status"] == "dropped"
        assert result["reason"] == "no_class"
        assert result["classification"] == "none"

    def test_run_ok_with_full_flow(self, tmp_path):
        """run returns 'ok' with gate, vm1, tg1, vm2, tg2, match, tg3."""
        alert = _make_alert()
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "Detected: vehicle", "photos": []}
        vm2_result = {
            "class": "vehicle",
            "color": "white",
            "make": "Ford",
            "model": "F-150",
            "body_style_hint": "pickup",
            "vehicle_features": {"wheel_style": "alloy"},
            "confidence": 0.92,
            "notable_details": [],
        }
        tg2 = {"caption": "Camera: Front Gate", "photos": []}
        match_result = {"matched": False}
        tg3 = {"caption": "Status: unrecognized vehicle", "photos": []}

        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts",
                return_value=_make_artifacts(),
            ),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
            patch("listener.pipeline.record_hit") as mock_record,
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

    def test_record_hit_called_on_success(self, tmp_path):
        """record_hit is called with camera_id, classification when the pipeline succeeds."""
        alert = _make_alert()
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "test", "photos": []}
        vm2_result = {
            "class": "vehicle",
            "color": "white",
            "make": "Ford",
            "confidence": 0.92,
            "notable_details": [],
        }
        tg2 = {"caption": "test", "photos": []}
        tg3 = {"caption": "test", "photos": []}

        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts",
                return_value=_make_artifacts(),
            ),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
            patch("listener.pipeline.record_hit") as mock_record,
        ):
            run(alert)

        assert mock_record.call_count == 1
        assert mock_record.call_args[0][:2] == ("CAM1", "vehicle")

    def test_prepare_alert_artifacts_called_with_camera_and_alert_id(self, tmp_path):
        """prepare_alert_artifacts receives gate_verdict, frames, and output_dir from run()."""

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

            vm1_result = {"class": "vehicle", "confidence": 0.92}
            tg1 = {"caption": "Detected: vehicle", "photos": []}
            vm2_result = {
                "class": "vehicle",
                "color": "white",
                "make": "Ford",
                "confidence": 0.92,
                "notable_details": [],
            }
            tg2 = {"caption": "Camera: Front Gate", "photos": []}
            tg3 = {"caption": "Status: unrecognized", "photos": []}

            expected_output_dir = tmp_path / "data" / "frames" / "CAM3" / "alert-abc123"

            with (
                patch("listener.pipeline.should_suppress", return_value=False),
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


class TestPipelineLogLines:
    """AC4/AC1/AC3: Verify proceeded and dropped log lines at stage 7."""

    def test_proceeded_log_line_format(self, caplog):
        """Pipeline emits exactly one 'proceeded' log with top_class and top_confidence."""
        import logging

        caplog.set_level(logging.INFO, logger="listener.pipeline")

        alert = _make_alert(classification="person")
        diff_path = str(Path("/tmp/t"))
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(
            classification="person",
            class_label="person",
            confidence=0.85,
            top_class="person",
            top_confidence=0.85,
            pairwise_diff_path=diff_path,
        )

        vm1_result = {"class": "person", "confidence": 0.90}
        tg1 = {"caption": "test", "photos": []}
        vm2_result = {
            "class": "person",
            "better_crop": "crop_a",
            "confidence": 0.90,
            "notable_details": [],
        }
        tg2 = {"caption": "test", "photos": []}
        tg3 = {}

        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts",
                return_value=_make_artifacts(),
            ),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
            patch("listener.pipeline.record_hit"),
        ):
            run(alert)

        proceeded_logs = [
            record
            for record in caplog.records
            if record.levelname == "INFO"
            and "pipeline: proceeded" in record.message
            and "alert_id=evt-test-001" in record.message
            and "camera=CAM1" in record.message
            and "classification=person" in record.message
            and "top_class='person'" in record.message
            and "top_confidence=0.85" in record.message
        ]
        assert len(proceeded_logs) == 1, (
            f"Expected exactly 1 'proceeded' log line, got {len(proceeded_logs)}"
        )
        # No 'started pipeline' or other extra log lines
        extra_logs = [
            record
            for record in caplog.records
            if record.levelname == "INFO" and "started pipeline" in record.message
        ]
        assert len(extra_logs) == 0, "No 'started pipeline' log line should exist"

    def test_pipeline_run_emits_self_locating_tg2_tg3_captions(self, tmp_path):
        """pipeline.run() emits TG#2 and TG#3 captions with self-locating metadata."""
        alert = {
            "id": "evt-fake-001",
            "camera_id": "CAM1",
            "camera_label": "Front Gate",
            "timestamp": "2026-09-12T19:02:50.000+0000",
            "classification": "vehicle",
            "frames": ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg", "/tmp/f4.jpg"],
        }
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(
            classification="vehicle",
            class_label="car",
            confidence=0.91,
            top_class="car",
            top_confidence=0.91,
            pairwise_diff_path=diff_path,
        )

        a_p = str(tmp_path / "crop_a.png")
        b_p = str(tmp_path / "crop_b.png")
        Path(a_p).write_bytes(b"fake_crop_a")
        Path(b_p).write_bytes(b"fake_crop_b")

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "test", "photos": []}
        vm2_result = {
            "class": "vehicle",
            "color": "white",
            "make": "Ford",
            "confidence": 0.92,
            "notable_details": [],
        }

        mock_artifacts = MagicMock(spec=AlertArtifacts)
        mock_artifacts.crop_a_path = a_p
        mock_artifacts.crop_b_path = b_p

        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts", return_value=mock_artifacts
            ),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.record_hit"),
        ):
            result = run(alert)

        # Verify tg2 and tg3 were populated by the builders
        tg2 = result["tg2"]
        tg3 = result["tg3"]

        # TG#2 caption assertions
        assert "Camera: Front Gate" in tg2["caption"]
        assert "Alert 2 of 3" in tg2["caption"]
        assert "Alert ID: evt-fake-001" in tg2["caption"]
        assert "Timestamp: 2026-09-12T19:02:50.000+0000" in tg2["caption"]

        # TG#3 caption assertions
        assert "Camera: Front Gate" in tg3["caption"]
        assert "Alert 3 of 3" in tg3["caption"]
        assert "Alert ID: evt-fake-001" in tg3["caption"]
        assert "Timestamp: 2026-09-12T19:02:50.000+0000" in tg3["caption"]

    def test_cooldown_dropped_log_line_format(self, caplog):
        """Pipeline emits 'pipeline: dropped' log with classification and reason=cooldown_active."""
        import logging

        caplog.set_level(logging.INFO, logger="listener.pipeline")
        caplog.clear()

        alert = _make_alert(camera_id="CAM_X", id="alert-cooldown-001")

        with (
            patch("listener.pipeline.should_suppress", return_value=True),
            patch("listener.pipeline.run_gate") as mock_gate,
        ):
            mock_gate.return_value = _make_gate_verdict(
                classification="vehicle",
                top_class="car",
                top_confidence=0.85,
            )
            run(alert)

        dropped_logs = [
            record
            for record in caplog.records
            if record.levelname == "INFO"
            and "pipeline: dropped" in record.message
            and "alert_id=alert-cooldown-001" in record.message
            and "camera=CAM_X" in record.message
            and "classification=vehicle" in record.message
            and "reason=cooldown_active" in record.message
        ]
        assert len(dropped_logs) == 1, (
            f"Expected exactly 1 'dropped' cooldown log line, got {len(dropped_logs)}"
        )
