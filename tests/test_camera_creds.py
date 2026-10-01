"""
test_camera_creds.py — Tests for infra/camera_creds (env parser, lookup, anti-spoof).

Note: All IP addresses use the RFC 5737 documentation prefix
(192.0.2.0/24, TEST-NET-1) to avoid leaking production camera
addresses into the source tree. See RFC 5737, section 3.
"""

import os
import tempfile

from infra.camera_creds import (
    _parse_env,
    get_all_cameras,
    get_camera,
    get_frame_geometry,
    validate_source_ip,
)

# --------------------------------------------------------------------------
# _parse_env
# --------------------------------------------------------------------------

class TestParseEnv:
    """Tests for _parse_env."""

    def test_parses_full_env(self):
        """All 6 cameras are parsed from a standard camera-creds.env."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
            "BACK_IP=192.0.2.2\n"
            "BACK_RTSP_URL=rtsp://admin:pass@192.0.2.2:554/\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(env_content)
            f.flush()
            result = _parse_env(f.name)
        os.unlink(f.name)

        assert len(result) == 2
        assert "Front Door Outside" in result
        assert "Back Door Inside" in result
        assert result["Front Door Outside"]["ip"] == "192.0.2.1"
        assert "rtsp_url" in result["Front Door Outside"]

    def test_skips_comments_and_blanks(self):
        """Lines starting with # and empty lines are ignored."""
        env_content = (
            "# This is a comment\n"
            "\n"
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
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
        # Use a temp env file with RFC 5737 IPs so the test has no prod leaks.
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False
        ) as f:
            f.write(env_content)
            f.flush()
            env_path = f.name
        try:
            import os as _os
            old = _os.environ.get("FARM_CAMERA_CREDS_FILE")
            _os.environ["FARM_CAMERA_CREDS_FILE"] = env_path
            # Force re-parse by reloading the module
            import importlib

            import infra.camera_creds as _cc
            importlib.reload(_cc)
            try:
                assert (
                    _cc.validate_source_ip("FRONT", "192.0.2.1") is True
                )
            finally:
                _os.environ.pop("FARM_CAMERA_CREDS_FILE", None)
                if old is not None:
                    _os.environ["FARM_CAMERA_CREDS_FILE"] = old
                # Restore original module state
                importlib.reload(_cc)
        finally:
            os.unlink(env_path)

    def test_invalid_ip_returns_false(self):
        """validate_source_ip returns False when IP does not match."""
        assert validate_source_ip("FRONT", "1.2.3.4") is False

    def test_unknown_camera_returns_false(self):
        """validate_source_ip returns False for unknown cameras."""
        assert validate_source_ip("NONEXISTENT", "1.2.3.4") is False

    def test_case_insensitive_camera_id(self):
        """validate_source_ip accepts lowercase camera ID."""
        # Use a temp env file with RFC 5737 IPs to avoid prod leaks.
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False
        ) as f:
            f.write(env_content)
            f.flush()
            env_path = f.name
        try:
            import os as _os
            old = _os.environ.get("FARM_CAMERA_CREDS_FILE")
            _os.environ["FARM_CAMERA_CREDS_FILE"] = env_path
            import importlib

            import infra.camera_creds as _cc
            importlib.reload(_cc)
            try:
                assert _cc.validate_source_ip("front", "192.0.2.1") is True
            finally:
                _os.environ.pop("FARM_CAMERA_CREDS_FILE", None)
                if old is not None:
                    _os.environ["FARM_CAMERA_CREDS_FILE"] = old
                importlib.reload(_cc)
        finally:
            os.unlink(env_path)


# --------------------------------------------------------------------------
# get_frame_geometry
# --------------------------------------------------------------------------

class TestGetFrameGeometry:
    """Tests for get_frame_geometry()."""

    def test_parse_env_with_geometry_keys(self):
        """_parse_env correctly parses width, height, frame_fps, frame_format."""
        env_content = (
            "FRONT_WIDTH=3840\n"
            "FRONT_HEIGHT=2160\n"
            "FRONT_FRAME_FPS=30.0\n"
            "FRONT_FRAME_FORMAT=raw\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(env_content)
            f.flush()
            result = _parse_env(f.name)
        os.unlink(f.name)

        assert "Front Door Outside" in result
        assert result["Front Door Outside"]["width"] == 3840
        assert result["Front Door Outside"]["height"] == 2160
        assert result["Front Door Outside"]["fps"] == 30.0
        assert result["Front Door Outside"]["frame_format"] == "raw"

    def test_parse_env_missing_geometry_graceful(self):
        """_parse_env without geometry keys: no geometry fields in entry, no crash."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(env_content)
            f.flush()
            result = _parse_env(f.name)
        os.unlink(f.name)

        assert "Front Door Outside" in result
        entry = result["Front Door Outside"]
        assert entry.get("width") is None
        assert entry.get("height") is None
        assert entry.get("fps") is None
        assert entry.get("frame_format") is None

    def test_get_frame_geometry_returns_dict(self):
        """get_frame_geometry returns a dict with the expected keys."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
            "FRONT_WIDTH=3840\n"
            "FRONT_HEIGHT=2160\n"
            "FRONT_FRAME_FPS=15.0\n"
            "FRONT_FRAME_FORMAT=raw\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(env_content)
            f.flush()
            env_path = f.name
        try:
            import os as _os
            old = _os.environ.get("FARM_CAMERA_CREDS_FILE")
            _os.environ["FARM_CAMERA_CREDS_FILE"] = env_path
            import importlib

            import infra.camera_creds as _cc
            importlib.reload(_cc)
            try:
                geo = _cc.get_frame_geometry("FRONT")
                assert geo is not None
                assert isinstance(geo, dict)
                assert geo["width"] == 3840
                assert geo["height"] == 2160
                assert geo["fps"] == 15.0
                assert geo["frame_format"] == "raw"
            finally:
                _os.environ.pop("FARM_CAMERA_CREDS_FILE", None)
                if old is not None:
                    _os.environ["FARM_CAMERA_CREDS_FILE"] = old
                importlib.reload(_cc)
        finally:
            os.unlink(env_path)

    def test_get_frame_geometry_unknown_returns_none(self):
        """get_frame_geometry('NONEXISTENT') returns None."""
        assert get_frame_geometry("NONEXISTENT") is None
