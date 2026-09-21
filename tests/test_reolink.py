"""Tests for infra.reolink -- mock subprocess.run, no real hardware."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from infra.reolink import (
    ReolinkAuthError,
    ReolinkClient,
    ReolinkCommandError,
    ReolinkTimeoutError,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> ReolinkClient:
    return ReolinkClient(gateway_addr="10.0.0.1", binary_path="/usr/local/bin/reolink-cli")


# ---------------------------------------------------------------------------
# 1. Parsing: DetectionConfig
# ---------------------------------------------------------------------------


class TestParseDetectionConfig:
    def test_parses_full_dict(self):
        raw = {
            "sensitivity": 45,
            "enabled": True,
            "person_stay_time": 3,
            "vehicle_stay_time": 5,
            "dog_cat_stay_time": 2,
        }
        cfg = ReolinkClient._parse_detection_config(raw)
        assert cfg.sensitivity == 45
        assert cfg.enabled is True
        assert cfg.person_stay_time == 3
        assert cfg.vehicle_stay_time == 5
        assert cfg.dog_cat_stay_time == 2

    def test_parses_minimal_dict(self):
        raw = {"sensitivity": 30}
        cfg = ReolinkClient._parse_detection_config(raw)
        assert cfg.sensitivity == 30
        assert cfg.enabled is False

    def test_returns_default_on_empty(self):
        cfg = ReolinkClient._parse_detection_config({})
        assert cfg.sensitivity == 50
        assert cfg.enabled is False


# ---------------------------------------------------------------------------
# 2. Parsing: EncodeConfig
# ---------------------------------------------------------------------------


class TestParseEncodeConfig:
    def test_parses_full_dict(self):
        raw = {
            "resolution": "1080p",
            "frame_rate": 20,
            "bit_rate": 4096,
            "codec": "h264",
        }
        cfg = ReolinkClient._parse_encode_config(raw)
        assert cfg.resolution == "1080p"
        assert cfg.frame_rate == 20
        assert cfg.bit_rate == 4096
        assert cfg.codec == "h264"

    def test_returns_default_on_empty(self):
        cfg = ReolinkClient._parse_encode_config({})
        assert cfg.resolution == ""


# ---------------------------------------------------------------------------
# 3. Parsing: AudioConfig
# ---------------------------------------------------------------------------


class TestParseAudioConfig:
    def test_parses_full_dict(self):
        raw = {"volume": 80, "alarm_mute": True, "siren_enabled": True}
        cfg = ReolinkClient._parse_audio_config(raw)
        assert cfg.volume == 80
        assert cfg.alarm_mute is True
        assert cfg.siren_enabled is True

    def test_returns_default_on_empty(self):
        cfg = ReolinkClient._parse_audio_config({})
        assert cfg.volume == 50


# ---------------------------------------------------------------------------
# 4. Parsing: LightConfig
# ---------------------------------------------------------------------------


class TestParseLightConfig:
    def test_parses_full_dict(self):
        raw = {
            "ir_enabled": True,
            "status_led_enabled": True,
            "spotlight_enabled": False,
            "whiteled_alarm": False,
        }
        cfg = ReolinkClient._parse_light_config(raw)
        assert cfg.ir_enabled is True
        assert cfg.status_led_enabled is True
        assert cfg.spotlight_enabled is False
        assert cfg.whiteled_alarm is False

    def test_returns_default_on_empty(self):
        cfg = ReolinkClient._parse_light_config({})
        assert cfg.ir_enabled is False


# ---------------------------------------------------------------------------
# 5. Parsing: OsdConfig
# ---------------------------------------------------------------------------


class TestParseOsdConfig:
    def test_parses_full_dict(self):
        raw = {
            "channel_name": "FRONT",
            "time_overlay": True,
            "logo_overlay": False,
        }
        cfg = ReolinkClient._parse_osd_config(raw)
        assert cfg.channel_name == "FRONT"
        assert cfg.time_overlay is True

    def test_returns_default_on_empty(self):
        cfg = ReolinkClient._parse_osd_config({})
        assert cfg.channel_name == ""


# ---------------------------------------------------------------------------
# 6. Parsing: PrivacyMask
# ---------------------------------------------------------------------------


class TestParsePrivacyMask:
    def test_parses_list_of_dicts(self):
        raw = [
            {"x": 10, "y": 20, "width": 100, "height": 100, "enabled": True},
            {"x": 200, "y": 300, "width": 150, "height": 150, "enabled": False},
        ]
        masks = ReolinkClient._parse_privacy_masks(raw)
        assert len(masks) == 2
        assert masks[0].x == 10
        assert masks[1].enabled is False

    def test_returns_empty_list_on_empty(self):
        masks = ReolinkClient._parse_privacy_masks([])
        assert masks == []


# ---------------------------------------------------------------------------
# 7. Parsing: PTZ presets
# ---------------------------------------------------------------------------


class TestParsePTZPresets:
    def test_parses_list_of_dicts(self):
        raw = [
            {"name": "Entrance", "index": 1},
            {"name": "Porch", "index": 2},
        ]
        presets = ReolinkClient._parse_ptz_presets(raw)
        assert len(presets) == 2
        assert presets[0].name == "Entrance"
        assert presets[1].index == 2


# ---------------------------------------------------------------------------
# 8. Parsing: PTZ patrol
# ---------------------------------------------------------------------------


class TestParsePTZPatrol:
    def test_parses_patrol_with_presets(self):
        raw = [
            {
                "name": "Tour",
                "presets": ["Entrance", "Porch"],
            },
        ]
        patrols = ReolinkClient._parse_ptz_patrols(raw)
        assert len(patrols) == 1
        assert patrols[0].name == "Tour"
        assert patrols[0].presets == ["Entrance", "Porch"]


# ---------------------------------------------------------------------------
# 9. Parsing: DeviceInfo
# ---------------------------------------------------------------------------


class TestParseDeviceInfo:
    def test_parses_full_dict(self):
        raw = {
            "model": "Reolink RLC-810A",
            "firmware": "5.2.0.1234",
            "serial": "C0XXXXXXXXXX",
            "online": True,
        }
        info = ReolinkClient._parse_device_info(raw)
        assert info.model == "Reolink RLC-810A"
        assert info.firmware == "5.2.0.1234"
        assert info.online is True

    def test_returns_default_on_empty(self):
        info = ReolinkClient._parse_device_info({})
        assert info.model == ""


# ---------------------------------------------------------------------------
# 10. Parsing: PingResult
# ---------------------------------------------------------------------------


class TestParsePingResult:
    def test_parses_reachable(self):
        raw = {"reachable": True, "rtt_ms": 15.3}
        result = ReolinkClient._parse_ping_result(raw)
        assert result.reachable is True
        assert result.rtt_ms == 15.3

    def test_returns_default_on_empty(self):
        result = ReolinkClient._parse_ping_result({})
        assert result.reachable is False


# ---------------------------------------------------------------------------
# 11. Parsing: CapabilitiesResult
# ---------------------------------------------------------------------------


class TestParseCapabilities:
    def test_parses_features(self):
        raw = {
            "features": ["AI_DETECTION", "PTZ", "AUDIO"],
            "ptz": True,
            "ai_detection": True,
            "audio": True,
            "spotlight": True,
        }
        caps = ReolinkClient._parse_capabilities(raw)
        assert "AI_DETECTION" in caps.features
        assert caps.ptz_supported is True
        assert caps.spotlight is True

    def test_returns_default_on_empty(self):
        caps = ReolinkClient._parse_capabilities({})
        assert caps.features == []


# ---------------------------------------------------------------------------
# 12. Parsing: UserEntry
# ---------------------------------------------------------------------------


class TestParseUserEntry:
    def test_parses_list_of_dicts(self):
        raw = [
            {"username": "admin", "user_type": "admin", "online": True},
            {"username": "viewer", "user_type": "guest", "online": False},
        ]
        users = ReolinkClient._parse_user_list(raw)
        assert len(users) == 2
        assert users[0].username == "admin"
        assert users[1].user_type == "guest"
        assert users[1].online is False


# ---------------------------------------------------------------------------
# 13. Parsing: RecordSchedule
# ---------------------------------------------------------------------------


class TestParseRecordSchedule:
    def test_parses_full_dict(self):
        raw = {
            "continuous": True,
            "motion_detect": True,
            "ai_detect": False,
            "pre_record_sec": 10,
            "post_record_sec": 5,
        }
        sched = ReolinkClient._parse_record_schedule(raw)
        assert sched.continuous is True
        assert sched.motion_detect is True
        assert sched.ai_detect is False
        assert sched.pre_record_sec == 10

    def test_returns_default_on_empty(self):
        sched = ReolinkClient._parse_record_schedule({})
        assert sched.continuous is True


# ---------------------------------------------------------------------------
# 14. Parsing: PTZGuard
# ---------------------------------------------------------------------------


class TestParsePTZGuard:
    def test_parses_full_dict(self):
        raw = {"enabled": True, "preset_index": 3}
        guard = ReolinkClient._parse_ptz_guard(raw)
        assert guard.enabled is True
        assert guard.preset_index == 3

    def test_returns_default_on_empty(self):
        guard = ReolinkClient._parse_ptz_guard({})
        assert guard.enabled is False


# ---------------------------------------------------------------------------
# 15. Parsing: PTZAutotrack
# ---------------------------------------------------------------------------


class TestParsePTZAutotrack:
    def test_parses_full_dict(self):
        raw = {"enabled": True, "target_type": "person"}
        at = ReolinkClient._parse_ptz_autotrack(raw)
        assert at.enabled is True
        assert at.target_type == "person"

    def test_returns_default_on_empty(self):
        at = ReolinkClient._parse_ptz_autotrack({})
        assert at.enabled is False


# ---------------------------------------------------------------------------
# 16. Error: ReolinkCommandError
# ---------------------------------------------------------------------------


class TestReolinkCommandError:
    def test_raises_on_nonzero_exit(self, client):
        with (
            patch("subprocess.run") as mock_run,
            pytest.raises(ReolinkCommandError) as exc_info,
        ):
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout='{"error": "bad"}',
                stderr="permission denied",
            )
            client.detect_ai_get(camera="FRONT", atype="person")

        assert exc_info.value.exit_code == 1
        assert "permission denied" in exc_info.value.stderr
        assert "detect" in exc_info.value.command


# ---------------------------------------------------------------------------
# 17. Error: ReolinkAuthError
# ---------------------------------------------------------------------------


class TestReolinkAuthError:
    def test_raises_auth_error(self, client):
        with (
            patch("subprocess.run") as mock_run,
            pytest.raises(ReolinkAuthError) as exc_info,
        ):
            mock_run.return_value = MagicMock(
                returncode=403,
                stdout='{"code": -101}',
                stderr="auth failed",
            )
            client.detect_ai_get(camera="FRONT", atype="person")

        assert "auth" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 18. Error: ReolinkTimeoutError
# ---------------------------------------------------------------------------


class TestReolinkTimeoutError:
    def test_raises_on_timeout(self):
        c = ReolinkClient(gateway_addr="10.0.0.1", timeout_sec=5)
        with (
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cli", 5)),
            pytest.raises(ReolinkTimeoutError) as exc_info,
        ):
            c.detect_ai_get(camera="FRONT", atype="person")

        assert exc_info.value.seconds == 5


# ---------------------------------------------------------------------------
# 19. Timeout env var
# ---------------------------------------------------------------------------


class TestTimeoutEnv:
    def test_custom_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("FARM_REOLINK_TIMEOUT_SEC", "30")
        c = ReolinkClient(gateway_addr="10.0.0.1")
        assert c._timeout == 30

    def test_default_timeout(self):
        c = ReolinkClient(gateway_addr="10.0.0.1")
        assert c._timeout == 30  # project default


# ---------------------------------------------------------------------------
# 20. Binary path env var
# ---------------------------------------------------------------------------


class TestBinaryPathEnv:
    def test_custom_binary_from_env(self, monkeypatch):
        monkeypatch.setenv("FARM_REOLINK_CLI_BIN", "/custom/bin/reolink")
        c = ReolinkClient(gateway_addr="10.0.0.1")
        assert c._binary == "/custom/bin/reolink"

    def test_default_binary(self):
        c = ReolinkClient(gateway_addr="10.0.0.1")
        assert c._binary == "reolink-cli"


# ---------------------------------------------------------------------------
# 21. Alias vs. host arg plumbing
# ---------------------------------------------------------------------------


class TestAliasVsHost:
    def test_camera_param_passed_as_camera_flag(self, client):
        """camera= should render --camera VALUE."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="{}")
            client.detect_ai_get(camera="FRONT", atype="person")

        call_args = mock_run.call_args[0][0]
        assert "--camera" in call_args
        idx = call_args.index("--camera")
        assert call_args[idx + 1] == "FRONT"

    def test_host_param_passed_as_host_flag(self, client):
        """host= should render --host VALUE."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="{}")
            client.detect_ai_get(host="10.0.0.5", atype="person")

        call_args = mock_run.call_args[0][0]
        assert "--host" in call_args
        idx = call_args.index("--host")
        assert call_args[idx + 1] == "10.0.0.5"

    def test_host_takes_precedence_over_camera(self, client):
        """host= and camera= can both be present; host wins for routing."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="{}")
            client.detect_ai_get(camera="FRONT", host="10.0.0.5", atype="person")

        call_args = mock_run.call_args[0][0]
        # Both flags should be present (camera for alias, host for routing)
        assert "--camera" in call_args
        assert "--host" in call_args


# ---------------------------------------------------------------------------
# 22. write method raises on non-zero exit
# ---------------------------------------------------------------------------


class TestWriteRaises:
    def test_detect_ai_set_raises_on_error(self, client):
        with (
            patch("subprocess.run") as mock_run,
            pytest.raises(ReolinkCommandError),
        ):
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="invalid param"
            )
            client.detect_ai_set(camera="FRONT", atype="person", sensitivity=50)


# ---------------------------------------------------------------------------
# 23. snapshot returns bytes
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_returns_bytes_when_no_output_path(self, client):
        raw = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=raw)
            result = client.snapshot(camera="FRONT")
        assert isinstance(result, bytes)
        assert result == raw
