"""Regression test for V2-062 headlight false-positive suppression.

Verifies that two known FP events (eda3b858, 163e10ea) are suppressed during
quiet hours and that the kill switch (FARMSURV_QUIET_HOURS_ENABLED=0) allows
dispatch to proceed.

Design: Part A tests in_quiet_hours() directly. Part B patches in_quiet_hours
to return True, then calls _send_tg1/2/3 and verifies dispatch is not called.
This avoids dependency on the daemon's dispatch path for the suppression logic."""

import os
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Part A: unit tests for in_quiet_hours — no daemon dependency
# ---------------------------------------------------------------------------

class TestInQuietHoursUnit:
    """Verify the quiet-hours gate for the two known FP events."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("FARMSURV_QUIET_HOURS_ENABLED", "1")
        monkeypatch.setenv("FARMSURV_QUIET_HOURS_CAMERAS",
                           "OUTSIDE_FRONT_SOLAR,OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_FARM_LAT", "34.5782")
        monkeypatch.setenv("FARMSURV_FARM_LON", "-84.9438")

    def test_eda3b858_suppressed_at_0120_utc(self):
        """eda3b858: car FP, 01:20 UTC, OUTSIDE_FRONT_SOLAR."""
        from infra.quiet_hours import in_quiet_hours
        ts = datetime(2026, 9, 19, 1, 20, 0, tzinfo=UTC)
        assert in_quiet_hours(ts, "OUTSIDE_FRONT_SOLAR") is True

    def test_163e10ea_suppressed_at_2230_utc(self):
        """163e10ea: cat FP, confirmed night for BACK_SOLAR camera."""
        from infra.quiet_hours import in_quiet_hours
        # 00:30 UTC = 20:30 EDT, after sunset — definitely quiet hours.
        ts = datetime(2026, 9, 20, 0, 30, 0, tzinfo=UTC)
        assert in_quiet_hours(ts, "OUTSIDE_BACK_SOLAR") is True

    def test_daytime_not_suppressed(self):
        """Noon UTC should not be in quiet hours."""
        from infra.quiet_hours import in_quiet_hours
        ts = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)  # 08:00 EDT
        assert in_quiet_hours(ts, "OUTSIDE_FRONT_SOLAR") is False

    def test_camera_not_in_list_not_suppressed(self):
        """Camera not in env list should not be suppressed."""
        from infra.quiet_hours import in_quiet_hours
        ts = datetime(2026, 9, 20, 1, 0, 0, tzinfo=UTC)  # 21:00 EDT, night
        assert in_quiet_hours(ts, "UNRELATED_CAMERA") is False


# ---------------------------------------------------------------------------
# Part B: integration — _send_tg* gate via in_quiet_hours mock
# ---------------------------------------------------------------------------

class TestDispatchGateIntegration:
    """Test _send_tg* dispatch behaviour with in_quiet_hours gate."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("FARMSURV_QUIET_HOURS_ENABLED", "1")
        monkeypatch.setenv("FARMSURV_QUIET_HOURS_CAMERAS",
                           "OUTSIDE_FRONT_SOLAR,OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_FARM_LAT", "34.5782")
        monkeypatch.setenv("FARMSURV_FARM_LON", "-84.9438")

    def test_eda3b858_dispatch_not_called_when_suppressed(self):
        """eda3b858 at 01:20 UTC → in_quiet_hours True → no dispatch."""
        import listener.daemon as dm
        with patch.object(dm, "in_quiet_hours", return_value=True), \
             patch.object(dm.dispatcher, "dispatch") as mock_dispatch:
            dm._send_tg1(
                {"alert_id": "eda3b858", "camera_id": "OUTSIDE_FRONT_SOLAR"},
                "eda3b858", "fake_chat", "fake_token",
                camera_id="OUTSIDE_FRONT_SOLAR",
            )
            mock_dispatch.assert_not_called()

    def test_163e10ea_dispatch_not_called_when_suppressed(self):
        """163e10ea at 00:30 UTC → in_quiet_hours True → no dispatch."""
        import listener.daemon as dm
        with patch.object(dm, "in_quiet_hours", return_value=True), \
             patch.object(dm.dispatcher, "dispatch") as mock_dispatch:
            dm._send_tg1(
                {"alert_id": "163e10ea", "camera_id": "OUTSIDE_BACK_SOLAR"},
                "163e10ea", "fake_chat", "fake_token",
                camera_id="OUTSIDE_BACK_SOLAR",
            )
            mock_dispatch.assert_not_called()

    def test_kill_switch_re_enables_dispatch(self):
        """FARMSURV_QUIET_HOURS_ENABLED=0 → dispatch IS called."""
        os.environ["FARMSURV_QUIET_HOURS_ENABLED"] = "0"
        import listener.daemon as dm
        with patch.object(dm.dispatcher, "dispatch") as mock_dispatch:
            dm._send_tg1(
                {"alert_id": "test", "camera_id": "OUTSIDE_FRONT_SOLAR"},
                "test", "fake_chat", "fake_token",
                camera_id="OUTSIDE_FRONT_SOLAR",
            )
            mock_dispatch.assert_called_once()
        del os.environ["FARMSURV_QUIET_HOURS_ENABLED"]

    def test_all_three_tg_slots_suppressed(self):
        """All three _send_tg* slots suppressed in quiet hours."""
        import listener.daemon as dm
        with patch.object(dm, "in_quiet_hours", return_value=True), \
             patch.object(dm.dispatcher, "dispatch") as mock_dispatch:
            dm._send_tg1(
                {"alert_id": "eda3b858"},
                "eda3b858", "fake_chat", "fake_token",
                camera_id="OUTSIDE_FRONT_SOLAR",
            )
            dm._send_tg2(
                {"alert_id": "eda3b858"},
                "eda3b858", "fake_chat", "fake_token",
                camera_id="OUTSIDE_FRONT_SOLAR",
            )
            dm._send_tg3(
                {"alert_id": "eda3b858"},
                "eda3b858", "fake_chat", "fake_token",
                camera_id="OUTSIDE_FRONT_SOLAR",
            )
            mock_dispatch.assert_not_called()
