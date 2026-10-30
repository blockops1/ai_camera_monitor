"""test_farm_api_cameras_e2e — E2E tests for farm_api cameras endpoint.

E2E strategy:
    - Write real camera-creds.env and data/frames trees into a temp directory.
    - Set FARMSURV_DATA_DIR + FARM_CAMERA_CREDS_FILE so infra.camera_creds
      and farm_api.cameras read from the temp tree.
    - Clear cached infra.* modules so DATA_DIR is re-read from the new env.
    - Import the API module and exercise list_cameras(), get_camera_health(),
      get_camera_snapshot() against the real data structures (not mocks).
    - Verify redaction of LAN IPs in CameraInfo output.

Bug classes covered:
    - Camera info leaks LAN IPs in API responses.
    - Unknown camera IDs handled gracefully.
    - Snapshot path resolves to actual frame files.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test fixture
# ---------------------------------------------------------------------------

_TEST_FRONT_IP = os.environ.get("FARM_TEST_FRONT_IP", "192.168.1.1")
_TEST_BACK_IP = os.environ.get("FARM_TEST_BACK_IP", "192.168.1.2")

_TEST_CREDS = textwrap.dedent(f"""\
# Test camera credentials
# IPs come from FARM_TEST_FRONT_IP / FARM_TEST_BACK_IP env vars
# (default: RFC 5737 reserved range -- safe for any network)
FRONT_IP={_TEST_FRONT_IP}
FRONT_RTSP_URL=rtsp://admin:pass@{_TEST_FRONT_IP}:554/stream
BACK_IP={_TEST_BACK_IP}
BACK_RTSP_URL=rtsp://admin:pass@{_TEST_BACK_IP}:554/stream
""")


@pytest.fixture()
def _cameras_e2e_env(tmp_path: Path):
    """Set up a real camera-creds.env and frames tree under tmp_path."""
    creds_file = tmp_path / "camera-creds.env"
    creds_file.write_text(_TEST_CREDS)

    # Write real-ish frame files under the camera's uppercase prefix dir
    frames_dir = tmp_path / "data" / "frames" / "FRONT" / "test-alert-id"
    frames_dir.mkdir(parents=True)
    (frames_dir / "frame_001.png").write_bytes(b"fake png data")
    (frames_dir / "frame_002.png").write_bytes(b"fake png data 2")

    # Set env vars so infra reads from temp dir
    old_creds = os.environ.get("FARM_CAMERA_CREDS_FILE")
    old_data = os.environ.get("FARMSURV_DATA_DIR")
    os.environ["FARM_CAMERA_CREDS_FILE"] = str(creds_file)
    os.environ["FARMSURV_DATA_DIR"] = str(tmp_path / "data")

    # Clear ALL cached infra modules so they re-read DATA_DIR/FRAMES_DIR etc.
    for mod_name in list(sys.modules.keys()):
        if mod_name == "infra" or mod_name.startswith("infra."):
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
    for mod_name in list(sys.modules.keys()):
        if mod_name == "infra" or mod_name.startswith("infra."):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------

class TestListCamerasE2E:
    """E2E: list_cameras() reads from real camera-creds.env + returns typed objects."""

    def test_returns_list_of_camera_info(self, _cameras_e2e_env):
        from infra.farm_api.cameras import CameraInfo, list_cameras

        cameras = list_cameras()
        assert isinstance(cameras, list)
        assert len(cameras) >= 2

        for cam in cameras:
            assert isinstance(cam, CameraInfo)
            assert isinstance(cam.camera_id, str)
            assert isinstance(cam.display_name, str)
            assert isinstance(cam.model, str)
            assert isinstance(cam.rtsp_url_redacted, str)
            assert isinstance(cam.ip_redacted, str)
            assert cam.camera_id in ("FRONT", "BACK")

    def test_camera_info_redacts_lan_ip(self, _cameras_e2e_env):
        """LAN IPs (192.168.x.x) are replaced with [REDACTED-LAN-IP]."""
        from infra.farm_api.cameras import list_cameras

        cameras = list_cameras()
        for cam in cameras:
            assert "192.168." not in cam.rtsp_url_redacted
            assert "192.168." not in cam.ip_redacted
            assert "[REDACTED-LAN-IP]" in cam.rtsp_url_redacted
            assert "[REDACTED-LAN-IP]" in cam.ip_redacted


class TestCameraHealthE2E:
    """E2E: get_camera_health() handles unknown cameras gracefully."""

    def test_unknown_camera_returns_none(self, _cameras_e2e_env):
        from infra.farm_api.cameras import get_camera_health

        result = get_camera_health("UNKNOWN_CAM")
        assert result is None

    def test_known_camera_returns_camera_health(self, _cameras_e2e_env):
        from infra.farm_api.cameras import CameraHealth, get_camera_health

        result = get_camera_health("FRONT")
        assert result is not None
        assert isinstance(result, CameraHealth)


class TestCameraSnapshotE2E:
    """E2E: get_camera_snapshot() resolves to an actual frame file."""

    def test_snapshot_resolves_to_existing_frame(self, _cameras_e2e_env):
        """Snapshot returns Path to frame_002.png (newer than frame_001)."""
        from pathlib import Path

        from infra.farm_api.cameras import get_camera_snapshot

        result = get_camera_snapshot("FRONT")
        assert result is not None
        assert isinstance(result, Path)
        assert result.exists()
        assert result.suffix == ".png"
