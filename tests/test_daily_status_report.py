"""test_daily_status_report — E2E-style tests for infra.daily_status_report.

Tests the three fixed code paths:
  1. get_listener_status() port check uses listener PID (not plist label).
  2. get_per_cam_status() uses REOLINK_CLI absolute path.
  3. render_html() cam table rows get correct CSS classes.

All tests mock _run / subprocess at the boundary — no real network calls.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# infra/__init__ import fix lives in conftest.py (runs before test collection).

# Test fixture IP for FRONT cam mocks. Override with FARM_TEST_FRONT_IP env var.
# Default is RFC 5737 reserved range -- safe for any network.
_TEST_FRONT_IP = os.environ.get("FARM_TEST_FRONT_IP", "10.0.0.21")


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
        lsof_out = f"COMMAND  PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\npython3.11  {pid} farmops   10u  IPv4 0x1234      0t0 TCP *:8090 (LISTEN)\n"

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
                return 0, f"python3.11  75924 farmops   10u  IPv6 0x1234      0t0 TCP 10.0.0.1:54321->{_TEST_FRONT_IP}:554 (ESTABLISHED)\n", ""
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
            patch("infra.daily_status_report.CAMS", return_value=[
                {"alias": "FRONT", "ip": _TEST_FRONT_IP, "label": "Front Door Outside", "model": "RLC-833A"},
            ]),
            patch("infra.daily_status_report._cam_connections_from_heartbeat", return_value={"FRONT": 1}),
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
            patch("infra.daily_status_report.CAMS", return_value=[
                {"alias": "FRONT", "ip": _TEST_FRONT_IP, "label": "Front Door Outside", "model": "RLC-833A"},
            ]),
            patch("infra.daily_status_report._cam_connections_from_heartbeat", return_value={"FRONT": 1}),
        ):
            results = get_per_cam_status()

        assert len(results) == 1
        assert results[0]["cpu_pct"] is None
        assert results[0]["encode_main"] is None
        # _cam_connections_from_heartbeat is patched so _run is never called
        # (the heartbeat path bypasses lsof entirely); reolink-cli missing means
        # CPU/encode calls are skipped too.
        assert mock_run.call_count == 0


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
                {"alias": "FRONT", "ip": _TEST_FRONT_IP, "label": "Front Door Inside", "model": "RLC-833A",
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


# -----------------------------------------------------------------------
# Fixture log path for 24h parsing tests.
# -----------------------------------------------------------------------
_FIXTURE_LOG = os.path.join(
    os.path.dirname(__file__), "fixtures", "daily_status_report.log"
)


# -----------------------------------------------------------------------
# 24h history parsing tests.
# -----------------------------------------------------------------------
# Shared time anchor for 24h parsing tests — fixture data spans 2026-10-17 22:12
# to 2026-10-18 20:12, so a 24h window anchored here captures everything.
_TEST_NOW = dt.datetime(2026, 10, 18, 21, 0, 0)


class TestPerCam24h:
    def test_per_cam_24h_returns_six_cams(self):
        """Fixture log produces data for all 6 aliases."""
        from infra.daily_status_report import get_per_cam_24h

        result = get_per_cam_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)

        assert len(result) == 6
        for alias in ["FRONT", "BACK", "OFG", "OFP", "OFS", "OBS"]:
            assert alias in result

    def test_per_cam_24h_uptime_calculation(self):
        """FRONT has 24 healthy=True out of 24 -> 100%. OFP has 4/5 -> 80%."""
        from infra.daily_status_report import get_per_cam_24h

        result = get_per_cam_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)

        assert result["FRONT"]["uptime_pct"] == 100.0
        assert result["OFP"]["uptime_pct"] == 80.0

    def test_per_cam_24h_reconnects_count(self):
        """OFP has >= 1 reconnect (cumulative delta)."""
        from infra.daily_status_report import get_per_cam_24h

        result = get_per_cam_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)

        assert result["OFP"]["reconnects"] >= 1
        # Other cams have 0 reconnects
        for alias in ["FRONT", "BACK", "OFG", "OFS", "OBS"]:
            assert result[alias]["reconnects"] == 0


class TestVision24h:
    def test_vision_24h_counts(self):
        """Fixture log has 0 vision_analyzer 200 OK + 0 timeouts."""
        from infra.daily_status_report import get_vision_24h

        result = get_vision_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)

        assert isinstance(result, dict)
        assert "ok" in result
        assert "timeout" in result


class TestTelegram24h:
    def test_telegram_24h_counts(self):
        """Fixture log has 2 telegram 200 OK + 2 telegram ERROR lines."""
        from infra.daily_status_report import get_telegram_24h

        result = get_telegram_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)

        assert result["success"] == 2
        assert result["error"] == 2


class TestErrorBreakdown24h:
    def test_error_breakdown_categorizes(self):
        """Fixture log has TimeoutError, VisionAnalyzerError, KeyError."""
        from infra.daily_status_report import get_error_breakdown_24h

        result = get_error_breakdown_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)

        assert "TimeoutError" in result
        assert result["TimeoutError"] >= 2
        # VisionAnalyzerError should be captured
        assert "VisionAnalyzerError" in result
        assert "KeyError" in result


class TestListenerRestartCount24h:
    def test_listener_restart_count(self):
        """Fixture log has 4 lines containing 'bootstrap' (2 restarts x 2 lines each)."""
        from infra.daily_status_report import get_listener_restart_count_24h

        count = get_listener_restart_count_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)

        assert count == 4


# -----------------------------------------------------------------------
# AC5: "Last 24 hours" section appears before current-state section.
# -----------------------------------------------------------------------
class TestHtmlSectionOrder:
    def test_last_24h_before_current_state(self):
        """'Last 24 hours' header appears before 'Current state' in HTML."""
        from infra.daily_status_report import render_html

        report = {
            "generated_at": "2026-10-16T03:00:00",
            "listener": {"pid": 12345, "uptime_s": 10800, "ports": {8090: True, 8091: True, 8092: True}, "all_ports_up": True},
            "cams": [{"alias": "FRONT", "ip": "10.0.0.21", "label": "Front Door Outside", "model": "RLC-833A", "rtsp_conns": 1, "cpu_pct": 32, "encode_main": "3840x2160@2fps H.265", "reachable": True}],
            "webhooks_24h": {"FRONT": 5, "BACK": 2, "OFG": 1, "OFP": 0, "OFS": 0, "OBS": 0, "other": 0},
            "errors_24h": 3,
            "disk": {"free_gb": 200.5, "daemon_log_mb": 45.2},
            "history_24h": {
                "per_cam": {a: {"uptime_pct": 100.0, "reconnects": 0, "frames_processed": 100000} for a in ["FRONT", "BACK", "OFG", "OFP", "OFS", "OBS"]},
                "vision": {"ok": 10, "timeout": 2},
                "telegram": {"success": 5, "error": 0},
                "errors": {},
                "restarts": 1,
            },
        }

        html = render_html(report)

        h24_pos = html.find("Last 24 hours")
        cs_pos = html.find("Current state")
        assert h24_pos != -1, "Last 24 hours section missing"
        assert cs_pos != -1, "Current state section missing"
        assert h24_pos < cs_pos, "Last 24 hours section must appear BEFORE current state"

    def test_html_contains_per_cam_table_in_24h(self):
        """Per-cam table in 24h section has 6 rows."""
        from infra.daily_status_report import render_html

        report = {
            "generated_at": "2026-10-16T03:00:00",
            "listener": {"pid": 12345, "uptime_s": 10800, "ports": {8090: True, 8091: True, 8092: True}, "all_ports_up": True},
            "cams": [{"alias": "FRONT", "ip": "10.0.0.21", "label": "Front Door Outside", "model": "RLC-833A", "rtsp_conns": 1, "cpu_pct": 32, "encode_main": "3840x2160@2fps H.265", "reachable": True}],
            "webhooks_24h": {"FRONT": 5, "BACK": 2, "OFG": 1, "OFP": 0, "OFS": 0, "OBS": 0, "other": 0},
            "errors_24h": 3,
            "disk": {"free_gb": 200.5, "daemon_log_mb": 45.2},
            "history_24h": {
                "per_cam": {a: {"uptime_pct": 99.0, "reconnects": 3, "frames_processed": 150000} for a in ["FRONT", "BACK", "OFG", "OFP", "OFS", "OBS"]},
                "vision": {"ok": 10, "timeout": 2},
                "telegram": {"success": 5, "error": 0},
                "errors": {},
                "restarts": 0,
            },
        }

        html = render_html(report)

        # Each alias should appear in a table row in the 24h section
        for alias in ["FRONT", "BACK", "OFG", "OFP", "OFS", "OBS"]:
            assert f"<td>{alias}</td>" in html

    def test_no_lsof_in_cam_connection_path(self):
        """lsof does not appear as a command in _cam_connections_from_heartbeat."""
        import inspect

        from infra.daily_status_report import _cam_connections_from_heartbeat

        src = inspect.getsource(_cam_connections_from_heartbeat)
        # The docstring may mention lsof in prose; only the code path matters.
        # Check that no subprocess call with 'lsof' exists.
        assert 'lsof"' not in src and "'lsof'" not in src, "_cam_connections_from_heartbeat must not call lsof"

    def test_port_css_uses_ok_bad_based_on_bound_state(self):
        """Port CSS class depends on bound state, not hardcoded to bad."""
        from infra.daily_status_report import render_html

        report = {
            "generated_at": "2026-10-16T03:00:00",
            "listener": {"pid": 12345, "uptime_s": 10800, "ports": {8090: True, 8091: True, 8092: False}, "all_ports_up": False},
            "cams": [],
            "webhooks_24h": {},
            "errors_24h": 0,
            "disk": {"free_gb": 200.5, "daemon_log_mb": 45.2},
            "history_24h": {
                "per_cam": {a: {"uptime_pct": 100.0, "reconnects": 0, "frames_processed": 0} for a in ["FRONT", "BACK", "OFG", "OFP", "OFS", "OBS"]},
                "vision": {"ok": 0, "timeout": 0},
                "telegram": {"success": 0, "error": 0},
                "errors": {},
                "restarts": 0,
            },
        }

        html = render_html(report)

        # Port 8090 is up -> should have port-ok
        assert 'port-ok">:8090' in html
        # Port 8092 is down -> should have port-bad
        assert 'port-bad">:8092' in html


# -----------------------------------------------------------------------
# Untimestamped heartbeat markers (US-115b fix).
# -----------------------------------------------------------------------
class TestUntimestampedHeartbeatMarkers:
    def test_cut_lines_keeps_untimestamped_marker_lines(self):
        """_cut_lines retains untimestamped lines matching heartbeat markers."""
        from infra.daily_status_report import _cut_lines, _HEARTBEAT_MARKERS

        lines = [
            "2026-10-18 20:12:26 INFO some regular line",
            "camera_health: id=FRONT healthy=True secs_since_last_frame=0.3 frames=112000 reconnects=0",
            "listener_health: uptime=24h+5m pid=7669 port=8090",
            "telegram_health: configured=True bot_username=@farmwatch_bot",
            "vision_health: up=True model=qwen3-vl-8b",
            "api_health: port=8092 bound=True",
            "mcp_health: port=8091 bound=True",
            "cleanup_health: alive=True",
            "alerts_last_10m: count=43",
            "TRACE traceback continuation: should be DROPPED",
            "  File '/path/to/module.py', line 42: indented continuation",
        ]

        result = _cut_lines(lines, now=_TEST_NOW)

        # All heartbeat markers kept
        assert any("camera_health:" in l for l in result)
        assert any("listener_health:" in l for l in result)
        assert any("telegram_health:" in l for l in result)
        assert any("vision_health:" in l for l in result)
        assert any("api_health:" in l for l in result)
        assert any("mcp_health:" in l for l in result)
        assert any("cleanup_health:" in l for l in result)
        assert any("alerts_last_10m:" in l for l in result)

        # Non-marker untimestamped lines dropped
        assert not any("TRACE traceback" in l for l in result)
        assert not any("indented continuation" in l for l in result)

        # Timestamped line within window kept
        assert any("some regular line" in l for l in result)

    def test_mixed_fixture_returns_nonzero_heartbeat_counts(self):
        """Fixture with mixed timestamped + untimestamped markers returns > 0."""
        from infra.daily_status_report import get_per_cam_24h, get_vision_24h, get_telegram_24h

        per_cam = get_per_cam_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)
        # At least 4 of 6 aliases should have data (cameras with untimestamped
        # entries at the end of the fixture)
        cams_with_data = sum(
            1 for a in ["FRONT", "BACK", "OFG", "OFP", "OFS", "OBS"]
            if per_cam[a]["frames_processed"] > 0
        )
        assert cams_with_data >= 4, f"Expected >= 4 cams with data, got {cams_with_data}"

        vision = get_vision_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)
        assert isinstance(vision["ok"], int)
        assert isinstance(vision["timeout"], int)

        telegram = get_telegram_24h(path=Path(_FIXTURE_LOG), now=_TEST_NOW)
        assert telegram["success"] > 0
        assert telegram["error"] > 0


class TestPipelineFunnel24h:
    """get_pipeline_funnel_24h reads data/events/*/event.json stage records."""

    def _mk_event(self, root, aid, camera, gate_cls, vm1, vm2, dropped, started_at):
        import json
        d = root / aid
        d.mkdir(parents=True)
        ev = {
            "alert_id": aid,
            "camera_id": camera,
            "started_at": started_at,
            "finished_at": started_at,
            "gate": {"classification": gate_cls},
            "vm1": vm1,
            "vm2": vm2,
            "dropped": dropped,
        }
        (d / "event.json").write_text(json.dumps(ev))

    def test_funnel_stage_counts(self, tmp_path, monkeypatch):
        from infra.daily_status_report import get_pipeline_funnel_24h
        import infra.daily_status_report as R

        monkeypatch.setattr(R, "EVENTS_DIR", tmp_path)
        now = dt.datetime(2026, 10, 23, 8, 0, 0)
        in_ts = "2026-10-23T06:00:00+02:00"   # inside 24h window (local 06:00+02 == 04:00 UTC)
        out_ts = "2026-10-21T06:00:00+02:00"  # outside window

        # OFP: 2 evaluated, 1 gate pass -> vm1+vm2 shipped
        self._mk_event(tmp_path, "a1", "OUTSIDE_FRONT_POWER", "other", None, None, "gate_dropped", in_ts)
        self._mk_event(tmp_path, "a2", "OUTSIDE_FRONT_POWER", "person", {"class": "person"}, {"attributes": {}}, None, in_ts)
        # FRONT: gate pass, VM1 ran, VM2 dropped vm1_unsure
        self._mk_event(tmp_path, "b1", "FRONT", "vehicle", {"class": "car"}, None, "vm1_unsure", in_ts)
        # outside window — must not count
        self._mk_event(tmp_path, "c1", "FRONT", "person", {"class": "person"}, {"attributes": {}}, None, out_ts)
        # unknown camera_id — ignored
        self._mk_event(tmp_path, "d1", "UNKNOWN_CAM", "person", {"class": "person"}, {"attributes": {}}, None, in_ts)

        res = get_pipeline_funnel_24h(now=now)
        assert res["OFP"] == {"evaluated": 2, "gate_pass": 1, "vm1": 1, "vm2": 1, "shipped": 1}
        assert res["FRONT"] == {"evaluated": 1, "gate_pass": 1, "vm1": 1, "vm2": 0, "shipped": 0}
        assert res["BACK"]["evaluated"] == 0

    def test_funnel_missing_dir(self, tmp_path, monkeypatch):
        from infra.daily_status_report import get_pipeline_funnel_24h
        import infra.daily_status_report as R

        monkeypatch.setattr(R, "EVENTS_DIR", tmp_path / "nope")
        res = get_pipeline_funnel_24h(now=dt.datetime(2026, 10, 23, 8, 0, 0))
        assert res["FRONT"] == {"evaluated": 0, "gate_pass": 0, "vm1": 0, "vm2": 0, "shipped": 0}


class TestCutLinesTimestampInheritance:
    """Untimestamped heartbeat lines inherit the preceding header timestamp.

    Regression for the 2026-10-23 defect where continuation lines were kept
    unconditionally, so 'last 24h' parsers silently scanned the whole log tail.
    """

    def test_stale_continuation_dropped(self):
        from infra.daily_status_report import _cut_lines
        now = dt.datetime(2026, 10, 23, 8, 0, 0)
        lines = [
            "2026-10-20 10:00:00,123 INFO listener.health: health: heartbeat:",
            "camera_health: id=FRONT healthy=True secs_since_last_frame=1 frames=99999 reconnects=0",
            "2026-10-23 07:00:00,123 INFO listener.health: health: heartbeat:",
            "camera_health: id=BACK healthy=True secs_since_last_frame=1 frames=12345 reconnects=0",
        ]
        out = _cut_lines(lines, 24, now=now)
        assert any("id=BACK" in l for l in out)
        assert not any("id=FRONT" in l for l in out)

    def test_fresh_continuation_kept(self):
        from infra.daily_status_report import _cut_lines
        now = dt.datetime(2026, 10, 23, 8, 0, 0)
        lines = [
            "2026-10-23 06:00:00,123 INFO listener.health: health: heartbeat:",
            "camera_health: id=FRONT healthy=True secs_since_last_frame=1 frames=99 reconnects=0",
        ]
        out = _cut_lines(lines, 24, now=now)
        assert any("id=FRONT" in l for l in out)

class TestReadLogWindow:
    """Rotation-aware window reader (regression: 2026-10-24 webhook undercount)."""

    def test_walks_rotated_files_to_cover_window(self, tmp_path):
        from infra.daily_status_report import _read_log_window
        now = dt.datetime.now()
        fresh = now.strftime("%Y-%m-%d %H:%M:%S")
        old = (now - dt.timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
        (tmp_path / "daemon.log").write_text(f"{fresh} INFO current: live line\n")
        (tmp_path / "daemon.log.1").write_text(
            f"{old} INFO rotated: old line\n{fresh} INFO rotated tail\n"
        )
        lines = _read_log_window(tmp_path / "daemon.log", hours=24)
        assert any("old line" in l for l in lines)
        assert any("live line" in l for l in lines)

    def test_stops_once_cutoff_covered(self, tmp_path):
        from infra.daily_status_report import _read_log_window
        now = dt.datetime.now()
        fresh = now.strftime("%Y-%m-%d %H:%M:%S")
        old = (now - dt.timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")
        (tmp_path / "daemon.log").write_text(f"{fresh} INFO a\n")
        (tmp_path / "daemon.log.1").write_text(
            f"{old} INFO b\n{fresh} INFO c\n"
        )
        (tmp_path / "daemon.log.2").write_text("NEVER-READ marker line\n")
        lines = _read_log_window(tmp_path / "daemon.log", hours=24)
        # .1 oldest line predates the cutoff -> .2 must not be opened
        assert not any("NEVER-READ" in l for l in lines)
        assert any("INFO b" in l for l in lines)
