"""test_daily_status_report — E2E-style tests for infra.daily_status_report.

Tests the three fixed code paths:
  1. get_listener_status() port check uses listener PID (not plist label).
  2. get_per_cam_status() uses REOLINK_CLI absolute path.
  3. render_html() cam table rows get correct CSS classes.

All tests mock _run / subprocess at the boundary — no real network calls.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Test fixture IP for FRONT cam mocks. Override with FARM_TEST_FRONT_IP env var.
# Default is RFC 5737 reserved range -- safe for any network.
_TEST_FRONT_IP = os.environ.get("FARM_TEST_FRONT_IP", "10.0.0.39")


@pytest.fixture()
def mock_run():
    """Return a _run mock factory: caller sets return_value or side_effect.

    NOTE: get_per_cam_status uses _run() for lsof calls AND subprocess.run
    directly for reolink-cli calls. Tests that need the reolink path working
    must also patch subprocess.run at infra.daily_status_report.
    """
    mock = MagicMock()
    with patch("infra.daily_status_report._run", mock) as m:
        yield m


# -----------------------------------------------------------------------
# AC1: port check uses listener PID
# -----------------------------------------------------------------------
class TestPortCheckPid:
    def test_port_ok_when_pid_in_lsof_output(self, mock_run):
        """Listener PID is found in lsof output -> port marked up."""
        from infra.daily_status_report import get_listener_status

        pid = 75924
        lsof_out = f"COMMAND  PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\npython3.11  {pid} jill   10u  IPv4 0x1234      0t0 TCP *:8090 (LISTEN)\n"

        def fake_run(cmd, **kw):
            cmd_str = " ".join(cmd)
            if cmd[0] == "launchctl" and cmd[1] == "list":
                return 0, "75924 0 com.farm.surveillance.v2\n", ""
            if cmd[0] == "ps":
                return 0, "01:23:45\n", ""
            if cmd[0] == "lsof":
                # port 8090 -> PID found, port 8091 -> PID found, 8092 -> not found
                if ":8090" in cmd_str or ":8091" in cmd_str:
                    return 0, lsof_out, ""
                return 0, "COMMAND  PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n", ""
            return 1, "", ""

        mock_run.side_effect = fake_run
        result = get_listener_status()

        assert result["pid"] == 75924
        assert result["ports"][8090] is True
        assert result["ports"][8091] is True
        assert result["ports"][8092] is False
        assert result["all_ports_up"] is False

    def test_port_all_down_when_pid_none(self, mock_run):
        """Listener not found (pid=None) -> all ports down."""
        from infra.daily_status_report import get_listener_status

        def fake_run(cmd, **kw):
            return 1, "", ""

        mock_run.side_effect = fake_run
        result = get_listener_status()

        assert result["pid"] is None
        assert result["uptime_s"] is None
        assert result["all_ports_up"] is False


# -----------------------------------------------------------------------
# AC2: reolink-cli uses absolute path
# -----------------------------------------------------------------------
class TestReolinkCliPath:
    def test_cpu_and_encode_populated_when_cli_exists(self, mock_run, tmp_path):
        """reolink-cli call succeeds -> cpu_pct and encode_main populated."""
        from infra.daily_status_report import get_per_cam_status

        cli_path = tmp_path / ".local" / "bin" / "reolink-cli"
        cli_path.parent.mkdir(parents=True, exist_ok=True)
        cli_path.touch()

        def fake_run(cmd, **kw):
            if cmd[0] == "launchctl" and cmd[1] == "list":
                return 0, "75924 0 com.farm.surveillance.v2\n", ""
            if cmd[0] == "lsof" and "-sTCP:ESTABLISHED" in " ".join(cmd):
                return 0, f"python3.11  75924 jill   10u  IPv6 0x1234      0t0 TCP 10.0.0.1:54321->{_TEST_FRONT_IP}:554 (ESTABLISHED)\n", ""
            return 1, "", ""

        mock_run.side_effect = fake_run

        # Build a mock subprocess.run result
        def fake_subprocess_run(cmd, **kw):
            r = MagicMock()
            r.returncode = 0
            if "performance" in " ".join(cmd):
                r.stdout = json.dumps({"data": {"value": {"cpuUsedPercent": 32}}})
            elif "encode" in " ".join(cmd):
                r.stdout = json.dumps({"data": {"main": {"width": 3840, "height": 2160, "fps": "2", "codec": "H.265"}}})
            else:
                r.stdout = ""
            return r

        with (
            patch("infra.daily_status_report.REOLINK_CLI", cli_path),
            patch("infra.daily_status_report._get_shared_cam_password", return_value="testpass"),
            patch("infra.daily_status_report.CAMS", [
                {"alias": "FRONT", "ip": _TEST_FRONT_IP, "label": "Front Door Outside", "model": "RLC-833A"},
            ]),
            patch("infra.daily_status_report.subprocess.run", side_effect=fake_subprocess_run),
        ):
            results = get_per_cam_status()

        assert len(results) == 1
        assert results[0]["rtsp_conns"] == 1
        assert results[0]["cpu_pct"] == 32
        assert results[0]["encode_main"] == "3840x2160@2fps H.265"
        assert results[0]["reachable"] is True

    def test_skip_when_reolink_cli_missing(self, mock_run, tmp_path):
        """reolink-cli doesn't exist -> skip CPU/encode calls, info has nulls."""
        from infra.daily_status_report import get_per_cam_status

        missing_cli = tmp_path / ".local" / "bin" / "reolink-cli-missing"

        mock_run.side_effect = lambda cmd, **kw: (0, "", "")

        with (
            patch("infra.daily_status_report.REOLINK_CLI", missing_cli),
            patch("infra.daily_status_report._get_shared_cam_password", return_value="testpass"),
            patch("infra.daily_status_report.CAMS", [
                {"alias": "FRONT", "ip": _TEST_FRONT_IP, "label": "Front Door Outside", "model": "RLC-833A"},
            ]),
        ):
            results = get_per_cam_status()

        assert len(results) == 1
        assert results[0]["cpu_pct"] is None
        assert results[0]["encode_main"] is None
        assert mock_run.call_count == 2  # launchctl list + lsof, no reolink-cli calls


# -----------------------------------------------------------------------
# AC3: cam row CSS classes correct when data is valid
# -----------------------------------------------------------------------
class TestCamCssClass:
    def test_cam_ok_when_conns_and_cpu_valid(self):
        """Reachable cam with conns>=1, cpu<70 -> cam-ok class."""
        from infra.daily_status_report import render_html

        report = {
            "generated_at": "2026-10-12T08:00:00",
            "listener": {"pid": 75924, "uptime_s": 46800, "ports": {8090: True, 8091: True, 8092: True}, "all_ports_up": True},
            "cams": [
                {"alias": "FRONT", "ip": _TEST_FRONT_IP, "label": "Front Door Outside", "model": "RLC-833A",
                 "rtsp_conns": 1, "cpu_pct": 32, "encode_main": "3840x2160@2fps H.265", "reachable": True},
            ],
            "webhooks_24h": {"FRONT": 5, "BACK": 2, "OFG": 1, "OFP": 0, "OFS": 0, "OBS": 0, "other": 1},
            "errors_24h": 3,
            "disk": {"free_gb": 200.5, "daemon_log_mb": 45.2},
        }

        html = render_html(report)

        # The <tr> row should have cam-ok + cam-ok classes (light-green)
        assert '<tr class="cam-ok cam-ok">' in html

    def test_cam_crit_when_rtsp_zero(self):
        """RTSP conns=0 -> cam-crit class."""
        from infra.daily_status_report import render_html

        report = {
            "generated_at": "2026-10-12T08:00:00",
            "listener": {"pid": 75924, "uptime_s": 46800, "ports": {8090: True, 8091: True, 8092: True}, "all_ports_up": True},
            "cams": [
                {"alias": "FRONT", "ip": _TEST_FRONT_IP, "label": "Front Door Outside", "model": "RLC-833A",
                 "rtsp_conns": 0, "cpu_pct": 32, "encode_main": "3840x2160@2fps H.265", "reachable": True},
            ],
            "webhooks_24h": {"FRONT": 5, "BACK": 2, "OFG": 1, "OFP": 0, "OFS": 0, "OBS": 0, "other": 1},
            "errors_24h": 3,
            "disk": {"free_gb": 200.5, "daemon_log_mb": 45.2},
        }

        html = render_html(report)
        assert 'class="cam-crit cam-ok"' in html

    def test_cam_unknown_when_cpu_null(self):
        """cpu_pct=None -> cam-unknown class for CPU column."""
        from infra.daily_status_report import render_html

        report = {
            "generated_at": "2026-10-12T08:00:00",
            "listener": {"pid": 75924, "uptime_s": 46800, "ports": {8090: True, 8091: True, 8092: True}, "all_ports_up": True},
            "cams": [
                {"alias": "FRONT", "ip": _TEST_FRONT_IP, "label": "Front Door Outside", "model": "RLC-833A",
                 "rtsp_conns": 1, "cpu_pct": None, "encode_main": None, "reachable": False},
            ],
            "webhooks_24h": {"FRONT": 5, "BACK": 2, "OFG": 1, "OFP": 0, "OFS": 0, "OBS": 0, "other": 1},
            "errors_24h": 3,
            "disk": {"free_gb": 200.5, "daemon_log_mb": 45.2},
        }

        html = render_html(report)
        assert "cam-unknown" in html
