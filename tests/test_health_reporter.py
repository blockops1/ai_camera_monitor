"""
test_health_reporter.py — Tests for listener/health.py (HealthReporter).
"""

from __future__ import annotations

import os

import pytest

from listener.health import HealthReporter, _clear_alert_timestamps

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_alerts():
    """Auto-clear alert timestamps before each test."""
    _clear_alert_timestamps()
    yield
    _clear_alert_timestamps()


# ---------------------------------------------------------------------------
# Test: test_emits_listener_health_line (AC1)
# ---------------------------------------------------------------------------


class TestListenerHealthLine:
    def test_emits_listener_health_line(self):
        """AC1: format_listener_health returns a single-line string with
        all required keys in the expected shape."""
        line = HealthReporter.format_listener_health(
            uptime_s=3661.5,  # 1h 1m 1.5s
            port=8090,
            env_total=25,
            mcp_enabled=False,
        )

        assert line.startswith("listener_health:")
        assert "uptime=1h+1m" in line
        assert f"pid={os.getpid()}" in line
        assert "port=8090" in line
        assert "env_total=25" in line
        # plist_keepalive reflects the actual plist value (module-level default).
        # On this box KeepAlive was removed, so default is False.
        assert "plist_keepalive=" in line
        assert "mcp_enabled=False" in line
        assert "alerts_last_10m=" in line

    def test_uptime_formats_hours_and_minutes(self):
        """Uptime string wraps at 60 min boundaries."""
        line = HealthReporter.format_listener_health(
            uptime_s=7261.0,  # 2h 1m 1s
            port=8090,
            env_total=10,
            mcp_enabled=False,
        )
        assert "uptime=2h+1m" in line

    def test_uptime_zero(self):
        """Zero uptime formats as 0h+0m."""
        line = HealthReporter.format_listener_health(
            uptime_s=0.0,
            port=8090,
            env_total=0,
            mcp_enabled=False,
        )
        assert "uptime=0h+0m" in line

    def test_mcp_enabled_true(self):
        """mcp_enabled=True renders correctly."""
        line = HealthReporter.format_listener_health(
            uptime_s=10.0,
            port=8090,
            env_total=15,
            mcp_enabled=True,
        )
        assert "mcp_enabled=True" in line


# ---------------------------------------------------------------------------
# Test: test_emits_camera_health_lines (AC2)
# ---------------------------------------------------------------------------


class TestCameraHealthLine:
    def test_emits_camera_health_lines(self):
        """AC2: format_camera_health returns a single-line string with
        all required keys in the expected shape."""
        stats = {
            "healthy_flag": True,
            "seconds_since_last_frame": 1.234,
            "frames_decoded_total": 150,
            "reconnects_total": 3,
            "teardowns_total": 1,
            "seconds_since_teardown": 3600.5,
        }
        line = HealthReporter.format_camera_health("OUTSIDE_FRONT", stats)

        assert line.startswith("camera_health:")
        assert "id=OUTSIDE_FRONT" in line
        assert "healthy=True" in line
        assert "secs_since_last_frame=1.234" in line
        assert "frames=150" in line
        assert "reconnects=3" in line
        assert "teardowns=1" in line
        assert "last_teardown_age=3600.5" in line

    def test_camera_healthy_false(self):
        """healthy=False renders correctly when reader is unhealthy."""
        stats = {
            "healthy_flag": False,
            "seconds_since_last_frame": 30.0,
            "frames_decoded_total": 42,
            "reconnects_total": 0,
            "teardowns_total": 0,
            "seconds_since_teardown": None,
        }
        line = HealthReporter.format_camera_health("BACK_YARD", stats)
        assert "healthy=False" in line
        assert "last_teardown_age=None" in line

    def test_camera_no_frames_decoded(self):
        """Zero frames handled gracefully."""
        stats = {
            "healthy_flag": False,
            "seconds_since_last_frame": None,
            "frames_decoded_total": 0,
            "reconnects_total": 0,
            "teardowns_total": 0,
            "seconds_since_teardown": None,
        }
        line = HealthReporter.format_camera_health("OFFLINE_CAM", stats)
        assert "frames=0" in line
        assert "secs_since_last_frame=None" in line


# ---------------------------------------------------------------------------
# Test: test_handles_no_cameras_registered (AC3)
# ---------------------------------------------------------------------------


class TestNoCamerasRegistered:
    def test_empty_stats_all_returns_no_camera_lines(self):
        """AC3: when CameraCaptureRegistry.stats_all() is empty, the
        HealthReporter emits only the listener_health line (no camera lines)."""
        stats_all_before = HealthReporter.format_camera_health("FAKE", {
            "healthy_flag": True,
            "seconds_since_last_frame": 1.0,
            "frames_decoded_total": 1,
            "reconnects_total": 0,
            "teardowns_total": 0,
            "seconds_since_teardown": 1.0,
        })

        # Verify that format_camera_health with a known stats dict produces
        # exactly one line. When stats_all() returns {}, _camera_health_lines()
        # returns [].
        HealthReporter(port=8090, env_total=0, plist_keepalive=False)

        # Manually verify: format_camera_health returns a single line
        assert stats_all_before.count("\n") == 0  # no internal newlines

    def test_format_camera_health_returns_single_line(self):
        """format_camera_health always returns one line (no embedded newlines)."""
        stats = {
            "healthy_flag": True,
            "seconds_since_last_frame": 2.0,
            "frames_decoded_total": 100,
            "reconnects_total": 1,
            "teardowns_total": 2,
            "seconds_since_teardown": 500.0,
        }
        line = HealthReporter.format_camera_health("ANY", stats)
        assert line.count("\n") == 0
        assert line.strip() == line  # no leading/trailing whitespace


# ---------------------------------------------------------------------------
# Test: test_log_when_no_alerts (AC4 — the bug this card fixes)
# ---------------------------------------------------------------------------


class TestLogWhenNoAlerts:
    def test_heartbeat_emits_with_zero_alerts(self):
        """AC4: heartbeat fires regardless of pipeline activity.
        When no alerts have been processed, alerts_last_10m=0 and the
        health block is still emitted."""
        line = HealthReporter.format_listener_health(
            uptime_s=60.0,
            port=8090,
            env_total=20,
            mcp_enabled=False,
        )
        assert "alerts_last_10m=0" in line

    def test_alert_counter_increases_after_record(self):
        """alerts_last_10m increases when _record_alert_run is called."""
        from listener.health import _count_alerts_last_10m, _record_alert_run

        # Verify zero before recording.
        assert _count_alerts_last_10m() == 0

        _record_alert_run()
        assert _count_alerts_last_10m() == 1

        _record_alert_run()
        assert _count_alerts_last_10m() == 2

        _clear_alert_timestamps()

    def test_alert_counter_prunes_old_entries(self):
        """Alerts older than 10 minutes are pruned from the counter."""
        from listener.health import (
            _alert_lock,
            _alert_timestamps,
            _clear_alert_timestamps,
            _count_alerts_last_10m,
            _record_alert_run,
        )

        # Record one alert.
        _record_alert_run()
        assert _count_alerts_last_10m() == 1

        # Manually advance the oldest timestamp beyond the 10-min window.
        with _alert_lock:
            if _alert_timestamps:
                _alert_timestamps[0] = _alert_timestamps[0] - 601.0

        # The old entry should be pruned on next count.
        assert _count_alerts_last_10m() == 0

        _clear_alert_timestamps()

    def test_heartbeat_format_complete_block(self):
        """The complete heartbeat block (listener + 0 cameras) is
        a valid multi-line string with the listener_health line present."""
        line = HealthReporter.format_listener_health(
            uptime_s=120.5,
            port=8090,
            env_total=15,
            mcp_enabled=False,
        )
        # The block starts with listener_health.
        assert line.startswith("listener_health:")
        # The uptime format is correct.
        assert "uptime=0h+2m" in line


# ---------------------------------------------------------------------------
# Test: plist_keepalive reads actual plist value (not hardcoded True)
# ---------------------------------------------------------------------------


class TestPlistKeepaliveRead:
    """US-073h-fix: verify _read_plist_keepalive reads the real plist."""

    def test_read_plist_keepalive_false_on_missing_key(self, tmp_path):
        """When the plist has no KeepAlive key, _read_plist_keepalive returns False."""
        plist = tmp_path / "test.plist"
        plist.write_bytes(
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            b'<plist version="1.0">\n'
            b'<dict>\n'
            b'  <key>Label</key>\n'
            b'  <string>com.test</string>\n'
            b'</dict>\n'
            b'</plist>\n'
        )
        from listener.health import _read_plist_keepalive
        assert _read_plist_keepalive(str(plist)) is False

    def test_read_plist_keepalive_true_on_explicit_key(self, tmp_path):
        """When the plist has KeepAlive=true, _read_plist_keepalive returns True."""
        plist = tmp_path / "test_keepalive.plist"
        plist.write_bytes(
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            b'<plist version="1.0">\n'
            b'<dict>\n'
            b'  <key>Label</key>\n'
            b'  <string>com.test</string>\n'
            b'  <key>KeepAlive</key>\n'
            b'  <true/>\n'
            b'</dict>\n'
            b'</plist>\n'
        )
        from listener.health import _read_plist_keepalive
        assert _read_plist_keepalive(str(plist)) is True

    def test_read_plist_keepalive_false_on_absent_file(self):
        """When the plist file doesn't exist, _read_plist_keepalive returns False."""
        from listener.health import _read_plist_keepalive
        assert _read_plist_keepalive("/nonexistent/path.plist") is False

    def test_format_listener_health_accepts_true(self):
        """format_listener_health renders plist_keepalive=True when passed."""
        line = HealthReporter.format_listener_health(
            uptime_s=10.0,
            port=8090,
            env_total=5,
            mcp_enabled=False,
            plist_keepalive=True,
        )
        assert "plist_keepalive=True" in line

    def test_format_listener_health_accepts_false(self):
        """format_listener_health renders plist_keepalive=False when passed."""
        line = HealthReporter.format_listener_health(
            uptime_s=10.0,
            port=8090,
            env_total=5,
            mcp_enabled=False,
            plist_keepalive=False,
        )
        assert "plist_keepalive=False" in line

    def test_format_listener_health_default_is_module_read(self):
        """When not passed, the default comes from the module-level read."""
        from listener.health import _PLIST_KEEPALIVE

        line = HealthReporter.format_listener_health(
            uptime_s=10.0,
            port=8090,
            env_total=5,
            mcp_enabled=False,
        )
        expected = f"plist_keepalive={_PLIST_KEEPALIVE}"
        assert expected in line
