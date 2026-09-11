"""
test_drop_after_gate.py — Tests for post-gate drop on classification=none.

Acceptance criteria (US-026b):
  1. run() gets gate_verdict from gate.run_gate().
  2. After the gate returns, pipeline.py checks gate_verdict.classification.
  3. If classification == 'none': return dropped status — no Telegram dispatch,
     no cooldown check, no cascade call.
  4. Dropped webhook is logged at INFO.
  5. The dropped webhook does NOT call should_suppress, record_hit, or counters.
  6. Three test cases:
     (a) classification=person → pipeline continues to stage 3 (ok)
     (b) classification=animal → pipeline continues to stage 3 (ok)
     (c) classification=none → pipeline returns dropped, stage 3 not reached
  7. Single commit: 'post-gate drop on classification=none with log (US-026b)'.
  8. pytest tests/test_drop_after_gate.py -v 100% pass; full suite still green.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
    v = MagicMock()
    v.classification = classification
    v.class_label = class_label  # type: ignore[assignment]
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


def _make_artifacts(crop_a_path="/mock/a.png", crop_b_path="/mock/b.png"):
    """Build a mock AlertArtifacts for pipeline tests."""
    a = MagicMock()
    a.crop_a_path = crop_a_path
    a.crop_b_path = crop_b_path
    return a


def _make_alert(**kwargs):
    """Build a sample alert dict."""
    return {
        "id": kwargs.get("id", "evt-test-001"),
        "camera_id": kwargs.get("camera_id", "CAM1"),
        "camera_label": kwargs.get("camera_label", "Front Gate"),
        "classification": kwargs.get("classification", "vehicle"),
        "frames": kwargs.get(
            "frames",
            ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg", "/tmp/f4.jpg"],
        ),
    }


# ---------------------------------------------------------------------------
# Classification=person → pipeline continues (stage 3 → ok)
# ---------------------------------------------------------------------------


class TestPersonClassification:
    """classification=person should proceed through the pipeline."""

    def test_person_classification_continues(self, tmp_path):
        """classification=person → pipeline returns 'ok' with full flow."""
        alert = _make_alert(classification="person")
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(
            classification="person",
            class_label="person",
            confidence=0.85,
            top_class="person",
            top_confidence=0.85,
            reason="high_conf_person",
            pairwise_diff_path=diff_path,
        )

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        vm1_result = {"class": "person", "confidence": 0.90}
        tg1 = {"caption": "Detected: person", "photos": []}
        vm2_result = {"class_confirmed": "person", "distinctive_features": []}
        tg2 = {"caption": "Camera: Front Gate", "photos": []}
        tg3 = {}

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
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
        ):
            result = run(alert)

        assert result["status"] == "ok"
        assert result["classification"] == "person"
        assert "vm1_result" in result
        assert "tg1" in result
        assert "vm2_result" in result
        assert "tg2" in result
        # record_hit should be called since pipeline succeeded
        mock_cooldown.record_hit.assert_called_once_with("CAM1", "person")


# ---------------------------------------------------------------------------
# Classification=animal → pipeline continues (stage 3 → ok)
# ---------------------------------------------------------------------------


class TestAnimalClassification:
    """classification=animal should proceed through the pipeline."""

    def test_animal_classification_continues(self, tmp_path):
        """classification=animal → pipeline returns 'ok' with full flow."""
        alert = _make_alert(classification="animal")
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(
            classification="animal",
            class_label="dog",
            confidence=0.75,
            top_class="dog",
            top_confidence=0.75,
            reason="animal_classified",
            pairwise_diff_path=diff_path,
        )

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        vm1_result = {"class": "animal", "confidence": 0.80}
        tg1 = {"caption": "Detected: animal", "photos": []}
        vm2_result = {"class_confirmed": "animal", "distinctive_features": []}
        tg2 = {"caption": "Camera: Front Gate", "photos": []}
        tg3 = {}

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
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
        ):
            result = run(alert)

        assert result["status"] == "ok"
        assert result["classification"] == "animal"
        assert "vm1_result" in result
        assert "tg1" in result
        assert "vm2_result" in result
        assert "tg2" in result
        # record_hit should be called since pipeline succeeded
        mock_cooldown.record_hit.assert_called_once_with("CAM1", "animal")


# ---------------------------------------------------------------------------
# Classification=none → pipeline drops (no stage 3, no record_hit)
# ---------------------------------------------------------------------------


class TestNoneClassification:
    """classification=none should drop immediately — no stage 3, no record_hit."""

    def test_none_classification_drops(self):
        """classification=none → pipeline returns dropped, stage 3 never reached."""
        alert = _make_alert()
        gate_v = _make_gate_verdict(
            classification="none",
            class_label=None,  # type: ignore[arg-type]
            confidence=0.0,
            top_class="none",
            top_confidence=0.0,
            reason="no_object_detected",
            pairwise_diff_path=None,
        )

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.prepare_alert_artifacts") as mock_artifacts,
            patch("listener.pipeline.verify_class") as mock_verify,
        ):
            result = run(alert)

        # Should return dropped with the expected shape (no camera_id per spec)
        assert result["status"] == "dropped"
        assert result["reason"] == "no_class"
        assert result["classification"] == "none"

        # Stage 3 functions should NOT have been called
        mock_cooldown.record_hit.assert_not_called()
        mock_artifacts.assert_not_called()
        mock_verify.assert_not_called()

    def test_none_classification_no_stage3_reached(self):
        """Verify that stage 3 functions (verify_class, build_alert_message, etc.)
        are never called when classification=none."""
        alert = _make_alert()
        gate_v = _make_gate_verdict(
            classification="none",
            class_label=None,
            confidence=0.0,
            top_class="unknown",
            top_confidence=0.15,
            reason="high_conf_unknown_not_vehicle",
            pairwise_diff_path=None,
        )

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        # Patch the stage 3 functions — if any of them is called, the test fails
        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.prepare_alert_artifacts") as mock_artifacts,
            patch("listener.pipeline.verify_class") as mock_verify,
            patch("listener.pipeline.build_alert_message") as mock_tg1,
            patch("listener.pipeline.detail_class") as mock_detail,
            patch("listener.pipeline.build_detail_message") as mock_tg2,
            patch("listener.pipeline.build_match_message") as mock_match,
        ):
            result = run(alert)

        assert result["status"] == "dropped"
        assert result["reason"] == "no_class"
        assert result["classification"] == "none"

        # None of the stage 3 functions should have been called
        mock_artifacts.assert_not_called()
        mock_verify.assert_not_called()
        mock_tg1.assert_not_called()
        mock_detail.assert_not_called()
        mock_tg2.assert_not_called()
        mock_match.assert_not_called()
        mock_cooldown.record_hit.assert_not_called()

    def test_none_classification_log_format(self, caplog):
        """The dropped alert is logged at INFO with the expected format."""
        import logging

        caplog.set_level(logging.INFO, logger="listener.pipeline")

        alert = _make_alert(camera_id="BACK_CAM", id="alert-xyz")
        gate_v = _make_gate_verdict(
            classification="none",
            class_label="bird",
            confidence=0.25,
            top_class="bird",
            top_confidence=0.25,
            reason="no_object_detected",
            pairwise_diff_path=None,
        )

        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = False

        with (
            patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
            patch("listener.pipeline.run_gate", return_value=gate_v),
        ):
            result = run(alert)

        assert result["status"] == "dropped"

        # Verify the INFO log was emitted with the expected format
        dropped_logs = [
            record
            for record in caplog.records
            if record.levelname == "INFO"
            and "dropped" in record.message
            and "alert_id=alert-xyz" in record.message
            and "camera=BACK_CAM" in record.message
            and "classification=none" in record.message
            and "top_class='bird'" in record.message
            and "top_confidence=0.25" in record.message
            and "reason=no_class" in record.message
        ]
        assert len(dropped_logs) >= 1, (
            f"Expected INFO log with dropped fields, got: {[r.message for r in caplog.records]}"
        )
