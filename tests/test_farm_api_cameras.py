"""Tests for infra.farm_api.cameras.

STATUS: V2-068b1
RUN WITH: pytest tests/test_farm_api_cameras.py -v
"""

import os
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test fixture — create a fake camera-creds.env and a fake FRAMES_DIR,
# then reload infra.camera_creds (which caches at import time) with the
# test data. The reloader also clears any prior import of farm_api.cameras
# so it picks up the fresh camera_creds on reimport.
# ---------------------------------------------------------------------------

_TEST_CREDS = textwrap.dedent("""\
# Test camera credentials
FRONT_IP=192.168.1.39
FRONT_RTSP_URL=rtsp://admin:pass@192.168.1.39:554/stream
BACK_IP=192.168.1.85
BACK_RTSP_URL=rtsp://admin:pass@192.168.1.85:554/stream
""")


@pytest.fixture()
def _test_env(tmp_path: Path):
    """Set up test creds file, test frames dir, and reload camera_creds."""
    # 1. Write fake camera-creds.env
    creds_file = tmp_path / "camera-creds.env"
    creds_file.write_text(_TEST_CREDS)

    # 2. Write fake frames for FRONT camera
    frames_dir = tmp_path / "data" / "frames" / "Front Door Outside" / "test-alert-id"
    frames_dir.mkdir(parents=True)
    fake_frame = frames_dir / "frame_001.png"
    fake_frame.write_bytes(b"fake png data")

    # 3. Set env overrides
    old_creds = os.environ.get("FARM_CAMERA_CREDS_FILE")
    old_data = os.environ.get("FARMSURV_DATA_DIR")
    os.environ["FARM_CAMERA_CREDS_FILE"] = str(creds_file)
    os.environ["FARMSURV_DATA_DIR"] = str(tmp_path / "data")

    # 4. Clear cached modules so they re-read at import time
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.camera_creds", "infra.farm_api")):
            del sys.modules[mod_name]

    yield tmp_path

    # Restore
    if old_creds is not None:
        os.environ["FARM_CAMERA_CREDS_FILE"] = old_creds
    else:
        os.environ.pop("FARM_CAMERA_CREDS_FILE", None)
    if old_data is not None:
        os.environ["FARMSURV_DATA_DIR"] = old_data
    else:
        os.environ.pop("FARMSURV_DATA_DIR", None)


class TestListCameras:
    """test_list_cameras_shape: list_cameras() returns CameraInfo objects with expected fields."""

    def test_list_cameras_shape(self, _test_env):
        from infra.farm_api.cameras import CameraInfo, list_cameras

        cameras = list_cameras()

        assert isinstance(cameras, list)
        assert len(cameras) >= 2  # FRONT and BACK at minimum

        for cam in cameras:
            assert isinstance(cam, CameraInfo)
            assert isinstance(cam.camera_id, str)
            assert isinstance(cam.display_name, str)
            assert isinstance(cam.model, str)
            assert isinstance(cam.rtsp_url_redacted, str)
            assert isinstance(cam.ip_redacted, str)
            assert cam.camera_id in ("FRONT", "BACK")
            assert cam.display_name in ("Front Door Outside", "Back Door Inside")


class TestCameraHealth:
    """test_get_camera_health_unknown_id_returns_none:
    get_camera_health returns None for unknown camera IDs."""

    def test_get_camera_health_unknown_id_returns_none(self, _test_env):
        from infra.farm_api.cameras import get_camera_health

        result = get_camera_health("UNKNOWN_CAM")
        assert result is None


class TestCameraSnapshot:
    """test_get_camera_snapshot_returns_existing_frame:
    get_camera_snapshot returns a Path when frames exist for a camera."""

    def test_get_camera_snapshot_returns_existing_frame(self, _test_env, tmp_path: Path):
        from infra.farm_api.cameras import get_camera_snapshot

        result = get_camera_snapshot("FRONT")
        assert result is not None
        assert result.name == "frame_001.png"


class TestCameraInfoRedaction:
    """test_camera_info_redacts_ip: CameraInfo rtsp_url_redacted and ip_redacted
    replace 192.168.x.x with [REDACTED-LAN-IP]."""

    def test_camera_info_redacts_ip(self, _test_env):
        from infra.farm_api.cameras import list_cameras

        cameras = list_cameras()
        assert len(cameras) > 0

        for cam in cameras:
            assert "192.168." not in cam.rtsp_url_redacted
            assert "192.168." not in cam.ip_redacted
            assert "[REDACTED-LAN-IP]" in cam.rtsp_url_redacted
            assert "[REDACTED-LAN-IP]" in cam.ip_redacted
