"""
test_pipeline_cooldown.py — Tests for infra.pipeline_cooldown.

Tests three behaviors:
  1. should_suppress returns True within window, False after.
  2. clear() resets state so should_suppress returns False after record.
  3. animal classification defaults to 0 window (never suppressed).
"""

from infra.pipeline_cooldown import PipelineCooldown


class TestPipelineCooldown:
    """End-to-end tests for PipelineCooldown."""

    def test_suppresses_within_window(self):
        """should_suppress returns True within window, False after."""
        pc = PipelineCooldown()

        # First call: miss — should return False and record.
        assert pc.should_suppress("CAM1", "vehicle") is False

        # Second call within 60s window: hit — should return True.
        assert pc.should_suppress("CAM1", "vehicle") is True

        # Different camera: miss.
        assert pc.should_suppress("CAM2", "vehicle") is False

        # Different classification: miss.
        assert pc.should_suppress("CAM1", "person") is False

        # Different classification AND camera: miss.
        assert pc.should_suppress("CAM2", "person") is False

    def test_record_hit_then_clear(self):
        """record_hit records a timestamp; clear() resets so suppression is False."""
        pc = PipelineCooldown()

        pc.record_hit("CAM1", "vehicle")
        assert pc.should_suppress("CAM1", "vehicle") is True

        pc.clear()
        assert pc.should_suppress("CAM1", "vehicle") is False

    def test_animal_default_zero(self):
        """animal classification has window=0, so never suppressed."""
        pc = PipelineCooldown()

        # First call: miss.
        assert pc.should_suppress("CAM1", "animal") is False

        # Second call: still miss because animal window is 0.
        assert pc.should_suppress("CAM1", "animal") is False

        # Third call after record_hit: still miss.
        pc.record_hit("CAM1", "animal")
        assert pc.should_suppress("CAM1", "animal") is False
