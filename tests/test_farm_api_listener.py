"""Tests for infra.farm_api.listener.

STATUS: V2-068b6
RUN WITH: pytest tests/test_farm_api_listener.py -v

Test strategy:
    - Patch subprocess.run for launchctl lookup.
    - Create a temporary daemon.log file with known content.
    - Override LOGS_DIR to point at the temp directory.
    - Reload modules to pick up the new LOGS_DIR.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def _test_log_dir(tmp_path: Path):
    """Create a fake logs/ directory with a daemon.log and reload modules."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Save old env
    old_logs_dir = os.environ.get("FARMSURV_LOGS_DIR")

    # Set FARMSURV_LOGS_DIR so LOGS_DIR points at temp
    os.environ["FARMSURV_LOGS_DIR"] = str(tmp_path / "logs")

    # Clear cached modules so they re-read the new LOGS_DIR
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.paths", "infra.farm_api.listener")):
            del sys.modules[mod_name]

    yield tmp_path

    # Restore
    if old_logs_dir is not None:
        os.environ["FARMSURV_LOGS_DIR"] = old_logs_dir
    else:
        os.environ.pop("FARMSURV_LOGS_DIR", None)

    # Re-clear
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.paths", "infra.farm_api.listener")):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# test_get_listener_status_shape
# ---------------------------------------------------------------------------


class TestGetListenerStatusShape:
    """get_listener_status() returns a valid ListenerStatus dataclass."""

    def test_status_shape_no_pid(self, _test_log_dir):
        """When launchctl returns no PID, status shows stopped."""
        from infra.farm_api.listener import ListenerStatus, get_listener_status

        with patch(
            "infra.farm_api.listener._get_listener_pid", return_value=None
        ):
            status = get_listener_status()

        assert isinstance(status, ListenerStatus)
        assert status.pid is None
        assert status.uptime_seconds == 0
        assert status.mode == "stopped"
        assert status.started_at is None
        assert status.last_event_at is None

    def test_status_shape_with_pid(self, _test_log_dir):
        """When PID found with boot log, status shows running with uptime."""
        log_dir = Path(os.environ["FARMSURV_LOGS_DIR"])
        (log_dir / "daemon.log").write_text(
            "2026-09-22 12:04:00,000 INFO daemon: env load complete: total=31\n"
        )

        from infra.farm_api.listener import ListenerStatus, get_listener_status

        with patch(
            "infra.farm_api.listener._get_listener_pid", return_value=50140
        ):
            status = get_listener_status()

        assert isinstance(status, ListenerStatus)
        assert status.pid == 50140
        assert status.mode == "running"
        assert status.uptime_seconds > 0
        assert status.started_at is not None
        assert status.last_event_at is None

    def test_boot_log_parsed(self, _test_log_dir):
        """Boot log line is parsed into started_at timestamp."""
        log_dir = Path(os.environ["FARMSURV_LOGS_DIR"])
        log_file = log_dir / "daemon.log"

        log_file.write_text(
            "2026-09-22 12:04:00,000 INFO daemon: env load complete: total=31 "
            "(telegram_token_len=65)\n"
            "2026-09-22 12:04:01,000 INFO daemon: Booting RTSP readers for 6 camera(s)...\n"
        )

        from infra.farm_api.listener import get_listener_status

        with patch(
            "infra.farm_api.listener._get_listener_pid", return_value=50140
        ):
            status = get_listener_status()

        assert status.started_at is not None
        assert "12:04:00" in status.started_at

    def test_last_event_parsed(self, _test_log_dir):
        """Pipeline event log line is parsed into last_event_at."""
        log_dir = Path(os.environ["FARMSURV_LOGS_DIR"])
        log_file = log_dir / "daemon.log"

        log_file.write_text(
            "2026-09-22 12:04:00,000 INFO daemon: env load complete: total=31\n"
            "2026-09-22 12:05:30,000 INFO daemon: pipeline: dropped alert_id="
            "abc-123 camera=FRONT reason=vm1_unsure\n"
        )

        from infra.farm_api.listener import get_listener_status

        with patch(
            "infra.farm_api.listener._get_listener_pid", return_value=50140
        ):
            status = get_listener_status()

        assert status.last_event_at is not None
        assert "12:05:30" in status.last_event_at

    def test_status_no_log_file(self, _test_log_dir):
        """When no daemon.log exists, started_at and last_event_at are None."""
        # Don't write any log file
        from infra.farm_api.listener import get_listener_status

        with patch(
            "infra.farm_api.listener._get_listener_pid", return_value=50140
        ):
            status = get_listener_status()

        assert status.started_at is None
        assert status.last_event_at is None


# ---------------------------------------------------------------------------
# test_get_recent_errors_parses_log
# ---------------------------------------------------------------------------


class TestGetRecentErrorsParsesLog:
    """get_recent_errors() returns ErrorRecord objects from the daemon log."""

    def test_parses_error_lines(self, _test_log_dir):
        """ERROR lines are parsed into ErrorRecord objects."""
        log_dir = Path(os.environ["FARMSURV_LOGS_DIR"])
        log_file = log_dir / "daemon.log"

        log_file.write_text(
            "2026-09-22 20:47:06,413 ERROR frame_capture: "
            "consecutive_reconnect_cap_reached: simulated RTSP failure\n"
            "2026-09-22 20:47:40,535 ERROR telegram_formatter.match_alert: "
            "send_photo failed for /a.jpg\n"
            "2026-09-22 20:47:47,078 ERROR daemon: pipeline.run failed for alert\n"
        )

        from infra.farm_api.listener import ErrorRecord, get_recent_errors

        errors = get_recent_errors(n=20)

        assert len(errors) == 3
        assert all(isinstance(e, ErrorRecord) for e in errors)

        # Newest first (we iterate reversed)
        assert errors[0].source == "daemon"
        assert "pipeline.run failed" in errors[0].message
        assert errors[0].severity == "ERROR"

        assert errors[1].source == "telegram_formatter.match_alert"
        assert "send_photo failed" in errors[1].message

        assert errors[2].source == "frame_capture"
        assert "consecutive_reconnect_cap_reached" in errors[2].message

    def test_limits_to_n(self, _test_log_dir):
        """get_recent_errors(n=2) returns only 2 records."""
        log_dir = Path(os.environ["FARMSURV_LOGS_DIR"])
        log_file = log_dir / "daemon.log"

        log_file.write_text(
            "2026-09-22 20:00:00,000 ERROR src: error1\n"
            "2026-09-22 20:01:00,000 INFO src: info line\n"
            "2026-09-22 20:02:00,000 ERROR src: error2\n"
            "2026-09-22 20:03:00,000 ERROR src: error3\n"
        )

        from infra.farm_api.listener import get_recent_errors

        errors = get_recent_errors(n=2)

        assert len(errors) == 2
        # Newest first
        assert "error3" in errors[0].message
        assert "error2" in errors[1].message

    def test_returns_empty_when_no_errors(self, _test_log_dir):
        """Returns empty list when log has no ERROR lines."""
        log_dir = Path(os.environ["FARMSURV_LOGS_DIR"])
        log_file = log_dir / "daemon.log"

        log_file.write_text(
            "2026-09-22 20:00:00,000 INFO daemon: all good\n"
            "2026-09-22 20:01:00,000 WARNING infra.gate: minor issue\n"
        )

        from infra.farm_api.listener import get_recent_errors

        errors = get_recent_errors()

        assert len(errors) == 0

    def test_returns_empty_when_no_log_file(self, _test_log_dir):
        """Returns empty list when daemon.log does not exist."""
        # Don't write any log file
        from infra.farm_api.listener import get_recent_errors

        errors = get_recent_errors()

        assert len(errors) == 0

    def test_error_record_has_timestamp(self, _test_log_dir):
        """ErrorRecord timestamps are parsed from log lines."""
        log_dir = Path(os.environ["FARMSURV_LOGS_DIR"])
        log_file = log_dir / "daemon.log"

        log_file.write_text(
            "2026-09-22 20:47:06,413 ERROR frame_capture: test error\n"
        )

        from infra.farm_api.listener import get_recent_errors

        errors = get_recent_errors()

        assert len(errors) == 1
        assert "20:47:06" in errors[0].timestamp
        assert errors[0].source == "frame_capture"
        assert errors[0].message == "test error"
        assert errors[0].severity == "ERROR"
