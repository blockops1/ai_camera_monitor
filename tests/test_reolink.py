"""Tests for infra.reolink -- mock subprocess.run, no real hardware."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from infra.reolink import (
    DetectionConfig,
    ReolinkAuthError,
    ReolinkClient,
    ReolinkCommandError,
    ReolinkTimeoutError,
    _to_dict,
    _to_list,
)

# ---------------------------------------------------------------------------
# _to_dict / _to_list helpers
# ---------------------------------------------------------------------------


class TestToDict:
    """_to_dict normalizes JsonValue -> dict."""

    def test_dict_passthrough(self):
        data = {"a": 1}
        result = _to_dict(data)
        assert result is data

    def test_list_wraps_in_dict(self):
        data = [{"x": 1}]
        result = _to_dict(data)
        assert result == {"_wrapped_list": data}


class TestToList:
    """_to_list normalizes JsonValue -> list."""

    def test_list_passthrough(self):
        data = [1, 2]
        result = _to_list(data)
        assert result is data

    def test_dict_wraps_in_list(self):
        data = {"x": 1}
        result = _to_list(data)
        assert result == [data]


# ---------------------------------------------------------------------------
# _run_json -- subprocess error handling
# ---------------------------------------------------------------------------


class TestRunJson:
    """ReolinkClient._run_json builds and runs the CLI command."""

    @patch("infra.reolink.subprocess.run")
    def test_returns_parsed_data(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"model": "IPC-H"}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client._run_json("info")
        assert result == {"model": "IPC-H"}

    @patch("infra.reolink.subprocess.run")
    def test_wraps_data_key_in_list_when_expect_list(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"alias": "FRONT"}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client._run_json("device", "list", expect_list=True)
        assert result == [{"alias": "FRONT"}]

    @patch("infra.reolink.subprocess.run")
    def test_list_passthrough(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps([{"a": 1}, {"b": 2}]),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client._run_json("test")
        assert result == [{"a": 1}, {"b": 2}]

    @patch("infra.reolink.subprocess.run")
    def test_raises_timeout_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["reolink-cli", "info"], timeout=30
        )
        client = ReolinkClient()
        with pytest.raises(ReolinkTimeoutError) as exc_info:
            client._run_json("info")
        assert exc_info.value.seconds == 30.0

    @patch("infra.reolink.subprocess.run")
    def test_raises_command_error_on_nonzero_rc(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "unknown subcommand"
        })
        client = ReolinkClient()
        with pytest.raises(ReolinkCommandError) as exc_info:
            client._run_json("invalid")
        assert exc_info.value.exit_code == 1

    @patch("infra.reolink.subprocess.run")
    def test_raises_auth_error_on_auth_failure(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "401 unauthorized"
        })
        client = ReolinkClient()
        with pytest.raises(ReolinkAuthError):
            client._run_json("login")


# ---------------------------------------------------------------------------
# _run_no_output -- subprocess error handling
# ---------------------------------------------------------------------------


class TestRunNoOutput:
    """ReolinkClient._run_no_output raises on errors."""

    @patch("infra.reolink.subprocess.run")
    def test_raises_command_error(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "error"
        })
        client = ReolinkClient()
        with pytest.raises(ReolinkCommandError):
            client._run_no_output("light", "ir", "set", enabled=True)

    @patch("infra.reolink.subprocess.run")
    def test_raises_timeout_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["reolink-cli", "light", "ir", "set"], timeout=30
        )
        client = ReolinkClient()
        with pytest.raises(ReolinkTimeoutError):
            client._run_no_output("light", "ir", "set")


# ---------------------------------------------------------------------------
# _build_args -- command construction
# ---------------------------------------------------------------------------


class TestBuildArgs:
    """ReolinkClient._build_args constructs the CLI command."""

    def test_minimal(self):
        client = ReolinkClient()
        args = client._build_args("detect", "ai", "get")
        assert args == ["reolink-cli", "--output", "json", "detect", "ai", "get"]

    def test_with_gateway(self):
        client = ReolinkClient(gateway_addr="10.0.0.1")
        args = client._build_args("detect", "ai", "get")
        assert "10.0.0.1" in args

    def test_with_camera(self):
        client = ReolinkClient()
        args = client._build_args("detect", "ai", "get", camera="FRONT")
        assert "--camera" in args
        assert "FRONT" in args

    def test_with_host(self):
        client = ReolinkClient()
        args = client._build_args("detect", "ai", "get", host="10.0.0.100")
        assert "--host" in args
        assert "10.0.0.100" in args

    def test_with_flags(self):
        client = ReolinkClient()
        args = client._build_args("detect", "ai", "set", sensitivity=50)
        assert "--sensitivity" in args
        assert "50" in args

    def test_none_values_skipped(self):
        client = ReolinkClient()
        args = client._build_args("detect", "ai", "set", sensitivity=None)
        assert "--sensitivity" not in args

    def test_underscore_to_dash(self):
        client = ReolinkClient()
        args = client._build_args("test", pre_record_sec=5)
        assert "--pre-record-sec" in args
        assert "pre_record_sec" not in args


# ---------------------------------------------------------------------------
# Public API -- detect_ai_*
# ---------------------------------------------------------------------------


class TestDetectAI:
    """detect_ai_get/set/apply flow."""

    @patch("infra.reolink.subprocess.run")
    def test_detect_ai_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"enabled": True, "sensitivity": 75}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.detect_ai_get("person")
        assert result.enabled is True
        assert result.sensitivity == 75

    @patch("infra.reolink.subprocess.run")
    def test_detect_ai_set_calls_command(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })
        client = ReolinkClient()
        client.detect_ai_set("person", sensitivity=60)
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "detect" in call_args
        assert "ai" in call_args
        assert "set" in call_args

    @patch("infra.reolink.subprocess.run")
    def test_detect_ai_apply_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"enabled": True, "sensitivity": 50}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.detect_ai_apply("person")
        assert isinstance(result, DetectionConfig)


# ---------------------------------------------------------------------------
# Public API -- detect_motion_*
# ---------------------------------------------------------------------------


class TestDetectMotion:
    """detect_motion_get/set/apply flow."""

    @patch("infra.reolink.subprocess.run")
    def test_detect_motion_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"enabled": False, "sensitivity": 30}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.detect_motion_get()
        assert result.enabled is False
        assert result.sensitivity == 30


# ---------------------------------------------------------------------------
# Public API -- encode_*
# ---------------------------------------------------------------------------


class TestEncode:
    """encode_get/set flow."""

    @patch("infra.reolink.subprocess.run")
    def test_encode_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"resolution": "1080p", "frame_rate": 20, "bit_rate": 4096}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.encode_get()
        assert result.resolution == "1080p"
        assert result.frame_rate == 20
        assert result.bit_rate == 4096


# ---------------------------------------------------------------------------
# Public API -- audio_*
# ---------------------------------------------------------------------------


class TestAudio:
    """audio_get/set flow."""

    @patch("infra.reolink.subprocess.run")
    def test_audio_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"volume": 80, "alarm_mute": False}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.audio_get()
        assert result.volume == 80
        assert result.alarm_mute is False

    @patch("infra.reolink.subprocess.run")
    def test_audio_set_mute(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })
        client = ReolinkClient()
        client.audio_set(mute=True)
        call_args = mock_run.call_args[0][0]
        assert "mute" in call_args

    @patch("infra.reolink.subprocess.run")
    def test_audio_set_unmute(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })
        client = ReolinkClient()
        client.audio_set(unmute=True)
        call_args = mock_run.call_args[0][0]
        assert "unmute" in call_args

    @patch("infra.reolink.subprocess.run")
    def test_audio_set_volume(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })
        client = ReolinkClient()
        client.audio_set(volume=75)
        call_args = mock_run.call_args[0][0]
        assert "config" in call_args
        assert "set" in call_args


# ---------------------------------------------------------------------------
# Public API -- light_*
# ---------------------------------------------------------------------------


class TestLight:
    """light_get/set flow."""

    @patch("infra.reolink.subprocess.run")
    def test_light_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"enabled": True, "status_led_enabled": False}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.light_get()
        assert result.ir_enabled is True
        assert result.status_led_enabled is False


# ---------------------------------------------------------------------------
# Public API -- image_*
# ---------------------------------------------------------------------------


class TestImage:
    """image_get/set flow."""

    @patch("infra.reolink.subprocess.run")
    def test_image_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"flip": True, "brightness": 60}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.image_get()
        assert result.flip is True
        assert result.brightness == 60


# ---------------------------------------------------------------------------
# Public API -- privacy_*
# ---------------------------------------------------------------------------


class TestPrivacy:
    """privacy_get/set flow."""

    @patch("infra.reolink.subprocess.run")
    def test_privacy_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": [{"x": 10, "y": 20, "width": 100, "height": 50, "enabled": True}]}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.privacy_get()
        assert len(result) == 1
        assert result[0].x == 10
        assert result[0].y == 20


# ---------------------------------------------------------------------------
# Public API -- ptz_*
# ---------------------------------------------------------------------------


class TestPTZ:
    """PTZ operations."""

    @patch("infra.reolink.subprocess.run")
    def test_ptz_move(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })
        client = ReolinkClient()
        client.ptz_move("up")
        call_args = mock_run.call_args[0][0]
        assert "ptz" in call_args
        assert "move" in call_args

    @patch("infra.reolink.subprocess.run")
    def test_ptz_preset_list_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": [{"name": "Home", "index": 1}]}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.ptz_preset_list()
        assert len(result) == 1
        assert result[0].name == "Home"
        assert result[0].index == 1

    @patch("infra.reolink.subprocess.run")
    def test_ptz_patrol_list_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": [{"name": "Patrol1", "presets": ["1", "2"]}]}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.ptz_patrol_list()
        assert len(result) == 1
        assert result[0].name == "Patrol1"
        assert result[0].presets == ["1", "2"]

    @patch("infra.reolink.subprocess.run")
    def test_ptz_autotrack_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"enabled": True, "target_type": "person"}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.ptz_autotrack_get()
        assert result.enabled is True
        assert result.target_type == "person"

    @patch("infra.reolink.subprocess.run")
    def test_ptz_guard_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"enabled": True, "preset_index": 3}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.ptz_guard_get()
        assert result.enabled is True
        assert result.preset_index == 3


# ---------------------------------------------------------------------------
# Public API -- users_*
# ---------------------------------------------------------------------------


class TestUsers:
    """User management."""

    @patch("infra.reolink.subprocess.run")
    def test_users_list_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": [{"username": "admin", "user_type": "admin", "online": True}]}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.users_list()
        assert len(result) == 1
        assert result[0].username == "admin"


# ---------------------------------------------------------------------------
# Public API -- osd_*
# ---------------------------------------------------------------------------


class TestOSD:
    """OSD settings."""

    @patch("infra.reolink.subprocess.run")
    def test_osd_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"channel_name": "Front Gate", "time_overlay": True}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.osd_get()
        assert result.channel_name == "Front Gate"
        assert result.time_overlay is True


# ---------------------------------------------------------------------------
# Public API -- record_*
# ---------------------------------------------------------------------------


class TestRecord:
    """Recording settings."""

    @patch("infra.reolink.subprocess.run")
    def test_record_get_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"continuous": True, "motion_detect": True, "pre_record_sec": 10}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.record_get()
        assert result.continuous is True
        assert result.motion_detect is True
        assert result.pre_record_sec == 10


# ---------------------------------------------------------------------------
# Public API -- events_*
# ---------------------------------------------------------------------------


class TestEvents:
    """Event queries."""

    @patch("infra.reolink.subprocess.run")
    def test_events_recent_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": [{"id": "e1", "type": "motion", "channel": 0, "start_time": "2026-09-21T00:00:00"}]}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.events_recent(limit=5)
        assert len(result) == 1
        assert result[0].event_id == "e1"
        assert result[0].event_type == "motion"


# ---------------------------------------------------------------------------
# Public API -- vod_*
# ---------------------------------------------------------------------------


class TestVOD:
    """VOD recording queries."""

    @patch("infra.reolink.subprocess.run")
    def test_vod_search_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": [{"file_name": "rec.mp4", "start_time": "2026-09-21T00:00:00"}]}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.vod_search(start_time="2026-09-21T00:00:00", end_time="2026-09-21T01:00:00")
        assert len(result) == 1
        assert result[0].file_name == "rec.mp4"


# ---------------------------------------------------------------------------
# Public API -- info / capabilities / ping / device_list
# ---------------------------------------------------------------------------


class TestDeviceInfo:
    """Device info, capabilities, ping, device list."""

    @patch("infra.reolink.subprocess.run")
    def test_info_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"model": "RLC-822A", "firmware": "5.3.0", "serial": "C1234567890", "online": True, "resolution": "4M", "channels": 1}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.info()
        assert result.model == "RLC-822A"
        assert result.firmware == "5.3.0"
        assert result.online is True

    @patch("infra.reolink.subprocess.run")
    def test_capabilities_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"features": ["ptz", "ai_detection"], "ptz": True, "ai_detection": True, "audio": True, "spotlight": False}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.capabilities()
        assert result.features == ["ptz", "ai_detection"]
        assert result.ptz_supported is True

    @patch("infra.reolink.subprocess.run")
    def test_ping_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": {"reachable": True, "rtt_ms": 2.5}}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.ping()
        assert result.reachable is True
        assert result.rtt_ms == 2.5

    @patch("infra.reolink.subprocess.run")
    def test_device_list_parses(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"data": [{"alias": "FRONT", "host": "10.0.0.100", "model": "RLC-822A", "serial": "C123", "online": True}]}),
            "stderr": ""
        })
        client = ReolinkClient()
        result = client.device_list()
        assert len(result) == 1
        assert result[0].alias == "FRONT"


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestClientInit:
    """ReolinkClient.__init__ reads env vars correctly."""

    def test_default_binary(self):
        client = ReolinkClient()
        assert client._binary == "reolink-cli"

    def test_custom_binary(self):
        client = ReolinkClient(binary_path="/usr/local/bin/reolink")
        assert client._binary == "/usr/local/bin/reolink"

    def test_default_gateway(self):
        client = ReolinkClient()
        assert client._gateway == ""

    def test_custom_gateway(self):
        client = ReolinkClient(gateway_addr="10.0.0.1")
        assert client._gateway == "10.0.0.1"

    def test_default_timeout(self):
        client = ReolinkClient()
        assert client._timeout == 30.0

    def test_custom_timeout(self):
        client = ReolinkClient(timeout_sec=60)
        assert client._timeout == 60.0


# ---------------------------------------------------------------------------
# Parsing edge cases
# ---------------------------------------------------------------------------


class TestParseEdgeCases:
    """Parsing handles various JSON shapes."""

    def test_detection_config_empty(self):
        result = ReolinkClient._parse_detection_config({})
        assert result.enabled is False
        assert result.sensitivity == 50

    def test_detection_config_with_defaults(self):
        result = ReolinkClient._parse_detection_config({"enable": 1})
        assert result.enabled is True
        assert result.sensitivity == 50  # default

    def test_parse_device_info_empty(self):
        result = ReolinkClient._parse_device_info({})
        assert result.model == ""

    def test_parse_capabilities_features_from_feature_key(self):
        result = ReolinkClient._parse_capabilities({"feature": "ptz"})
        assert result.features == ["ptz"]

    def test_parse_privacy_masks_empty_list(self):
        result = ReolinkClient._parse_privacy_masks([])
        assert result == []

    def test_parse_event_list_fallback_keys(self):
        result = ReolinkClient._parse_event_list([{"id": "e1", "type": "motion"}])
        assert len(result) == 1
        assert result[0].event_id == "e1"
        assert result[0].event_type == "motion"


# ---------------------------------------------------------------------------
# Snapshot (no-output path)
# ---------------------------------------------------------------------------


class TestSnapshot:
    """ReolinkClient.snapshot returns bytes or writes to file."""

    @patch("infra.reolink.subprocess.run")
    def test_snapshot_returns_bytes(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 0,
            "stdout": b"\xff\xd8\xff\xe0",
            "stderr": b""
        })
        client = ReolinkClient()
        result = client.snapshot()
        assert result == b"\xff\xd8\xff\xe0"

    @patch("infra.reolink.subprocess.run")
    def test_snapshot_raises_on_error(self, mock_run):
        mock_run.return_value = type("Proc", (), {
            "returncode": 1,
            "stdout": b"",
            "stderr": b"error: not found"
        })
        client = ReolinkClient()
        with pytest.raises(ReolinkCommandError):
            client.snapshot()
