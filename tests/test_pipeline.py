"""
test_pipeline.py — Tests for listener.pipeline (stages 1-3).

Tests three behaviors:
  1. run is importable and returns the correct shape on success.
  2. run returns 'suppressed' when cooldown says so (via injected cooldown).
  3. run returns 'ok' with frames when cooldown does not fire.
"""

from unittest.mock import MagicMock, patch

from listener.pipeline import run


class TestPipelineRun:
    """Tests for run() — stages 1-3."""

    def test_run_returns_ok_with_frames(self):
        """run returns {'status': 'ok', 'camera_id', 'classification', 'frames'}."""
        alert = {
            "camera_id": "CAM1",
            "classification": "vehicle",
            "frames": ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg", "/tmp/f4.jpg"],
        }
        result = run(alert)
        assert result["status"] == "ok"
        assert result["camera_id"] == "CAM1"
        assert result["classification"] == "vehicle"
        assert len(result["frames"]) == 4

    def test_run_returns_ok_with_empty_frames(self):
        """run returns empty frames list when alert has no frames key."""
        alert = {"camera_id": "CAM2", "classification": "person"}
        result = run(alert)
        assert result["status"] == "ok"
        assert result["frames"] == []

    def test_run_suppressed_by_cooldown(self):
        """run returns 'suppressed' when cooldown.should_suppress returns True."""
        from unittest.mock import MagicMock

        alert = {
            "camera_id": "CAM1",
            "classification": "vehicle",
            "frames": ["/tmp/f1.jpg"],
        }
        # Patch PipelineCooldown to return True (suppressed).
        mock_cooldown = MagicMock()
        mock_cooldown.should_suppress.return_value = True

        with patch(
            "listener.pipeline.PipelineCooldown", return_value=mock_cooldown
        ):
            result = run(alert)

        assert result["status"] == "suppressed"
        assert result["camera_id"] == "CAM1"
        assert result["classification"] == "vehicle"
