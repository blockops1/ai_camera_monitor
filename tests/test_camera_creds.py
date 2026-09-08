"""
test_camera_creds.py — Tests for infra/camera_creds (env parser, lookup, anti-spoof).
"""

import os
import tempfile

from infra.camera_creds import (
    _extract_ip_from_rtsp,
    _parse_env,
    get_all_cameras,
    get_camera,
    validate_source_ip,
)

# --------------------------------------------------------------------------
# _extract_ip_from_rtsp
# --------------------------------------------------------------------------

class TestExtractIpFromRtsp:
    """Tests for _extract_ip_from_rtsp helper."""

    def test_ipv4_basic(self):
        """Extract IP from a standard RTSP URL."""
        url = "rtsp://admin:pass@192.168.1.39:554/h264"
        assert _extract_ip_from_rtsp(url) == "192.168.1.39"

    def test_ipv4_with_port(self):
        """Extract IP when port is present."""
        url = "rtsp://admin:pass@10.0.0.1:554/"
        assert _extract_ip_from_rtsp(url) == "10.0.0.1"

    def test_password_with_at_sign(self):
        """Extract IP when password contains @ (encoded as %40)."""
        url = "rtsp://admin:%40pass@192.168.1.103:554/h264"
        assert _extract_ip_from_rtsp(url) == "192.168.1.103"

    def test_password_with_literal_at(self):
        """Extract IP when password contains literal @."""
        url = "rtsp://admin:p@ss@192.168.1.39:554/"
        assert _extract_ip_from_rtsp(url) == "192.168.1.39"

    def test_no_auth(self):
        """Return None when there is no @ in auth_and_host."""
        url = "rtsp://192.168.1.39:554/"
        assert _extract_ip_from_rtsp(url) is None

    def test_ipv6(self):
        """Extract IP from an IPv6 RTSP URL."""
        url = "rtsp://admin:pass@[::1]:554/"
        assert _extract_ip_from_rtsp(url) == "::1"

    def test_empty_url(self):
        """Return None on empty string."""
        assert _extract_ip_from_rtsp("") is None


# --------------------------------------------------------------------------
# _parse_env
# --------------------------------------------------------------------------

class TestParseEnv:
    """Tests for _parse_env."""

    def test_parses_full_env(self):
        """All 6 cameras are parsed from a standard camera-creds.env."""
        env_content = (
            "FRONT_IP=192.168.1.39\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.168.1.39:554/\n"
            "BACK_IP=192.168.1.85\n"
            "BACK_RTSP_URL=rtsp://admin:pass@192.168.1.85:554/\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(env_content)
            f.flush()
            result = _parse_env(f.name)
        os.unlink(f.name)

        assert len(result) == 2
        assert "Front Door Outside" in result
        assert "Back Door Inside" in result
        assert result["Front Door Outside"]["ip"] == "192.168.1.39"
        assert "rtsp_url" in result["Front Door Outside"]

    def test_skips_comments_and_blanks(self):
        """Lines starting with # and empty lines are ignored."""
        env_content = (
            "# This is a comment\n"
            "\n"
            "FRONT_IP=192.168.1.39\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.168.1.39:554/\n"
            "\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(env_content)
            f.flush()
            result = _parse_env(f.name)
        os.unlink(f.name)
        assert len(result) == 1

    def test_missing_file_returns_empty(self):
        """Parsing a non-existent file returns an empty dict."""
        assert _parse_env("/nonexistent/path.env") == {}


# --------------------------------------------------------------------------
# get_all_cameras
# --------------------------------------------------------------------------

class TestGetAllCameras:
    """Tests for get_all_cameras()."""

    def test_returns_non_empty_with_env_file(self):
        """get_all_cameras returns 6 cameras when camera-creds.env exists."""
        all_cam = get_all_cameras()
        # The v2 camera-creds.env has 6 cameras
        assert len(all_cam) >= 1

    def test_each_camera_has_required_keys(self):
        """Every camera dict has name, ip, rtsp_url, prefix."""
        all_cam = get_all_cameras()
        for name, info in all_cam.items():
            assert "ip" in info, f"{name} missing ip"
            assert "rtsp_url" in info, f"{name} missing rtsp_url"
            assert "prefix" in info, f"{name} missing prefix"


# --------------------------------------------------------------------------
# get_camera
# --------------------------------------------------------------------------

class TestGetCamera:
    """Tests for get_camera()."""

    def test_returns_camera_for_valid_id(self):
        """get_camera('FRONT') returns the FRONT camera dict."""
        cam = get_camera("FRONT")
        assert cam is not None
        assert "ip" in cam
        assert "rtsp_url" in cam

    def test_returns_none_for_unknown_id(self):
        """get_camera('NONEXISTENT') returns None."""
        assert get_camera("NONEXISTENT") is None


# --------------------------------------------------------------------------
# validate_source_ip
# --------------------------------------------------------------------------

class TestValidateSourceIp:
    """Tests for validate_source_ip()."""

    def test_valid_ip_returns_true(self):
        """validate_source_ip returns True when IP matches."""
        # FRONT's IP in camera-creds.env is 192.168.1.39
        assert validate_source_ip("FRONT", "192.168.1.39") is True

    def test_invalid_ip_returns_false(self):
        """validate_source_ip returns False when IP does not match."""
        assert validate_source_ip("FRONT", "1.2.3.4") is False

    def test_unknown_camera_returns_false(self):
        """validate_source_ip returns False for unknown cameras."""
        assert validate_source_ip("NONEXISTENT", "1.2.3.4") is False

    def test_case_insensitive_camera_id(self):
        """validate_source_ip accepts lowercase camera ID."""
        assert validate_source_ip("front", "192.168.1.39") is True
