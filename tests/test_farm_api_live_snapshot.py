"""test_farm_api_live_snapshot — Unit tests for cameras.get_live_snapshot().

Strategy:
    - Patch ReolinkClient at infra.reolink to return fake JPEG bytes.
    - Verify the snapshot file is written with a .jpg suffix under
      data/live_snapshots/<PREFIX>/.
    - Verify unknown cameras return None.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _live_snapshot_env(tmp_path: Path):
    """Redirect DATA_DIR so snapshot files land in tmp_path."""
    old_data = os.environ.get("FARMSURV_DATA_DIR")
    os.environ["FARMSURV_DATA_DIR"] = str(tmp_path / "data")

    for mod_name in list(sys.modules.keys()):
        if mod_name == "infra" or mod_name.startswith("infra."):
            del sys.modules[mod_name]

    yield tmp_path

    if old_data is not None:
        os.environ["FARMSURV_DATA_DIR"] = old_data
    else:
        os.environ.pop("FARMSURV_DATA_DIR", None)
    for mod_name in list(sys.modules.keys()):
        if mod_name == "infra" or mod_name.startswith("infra."):
            del sys.modules[mod_name]


def _make_mock_client(fake_jpeg: bytes) -> MagicMock:
    """Create a ReolinkClient mock that writes the fake JPEG to disk."""
    mock = MagicMock()

    def side_effect(*args, **kwargs):
        output_path = kwargs.get("output_path")
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(fake_jpeg)
        return fake_jpeg

    mock.snapshot.side_effect = side_effect
    return mock


class TestGetLiveSnapshot:
    """Unit tests for get_live_snapshot with a mocked ReolinkClient."""

    def test_snapshot_returns_path_with_jpg_suffix(self, _live_snapshot_env, tmp_path):
        """Snapshot writes a .jpg file and returns its path."""
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        mock_client = _make_mock_client(fake_jpeg)

        mock_cam = {
            "prefix": "FRONT",
            "gateway": "100.64.0.2",
            "ip": os.environ.get("FARM_TEST_FRONT_IP", "10.0.0.39"),
        }

        with (
            patch("infra.farm_api.cameras.get_camera", return_value=mock_cam),
            patch(
                "infra.reolink.ReolinkClient", return_value=mock_client
            ),
        ):
            from infra.farm_api.cameras import get_live_snapshot

            result = get_live_snapshot("FRONT")

            assert result is not None
            assert isinstance(result, str)
            snap_file = Path(result)
            assert snap_file.exists()
            assert snap_file.suffix == ".jpg"
            assert snap_file.read_bytes() == fake_jpeg

    def test_snapshot_unknown_camera_returns_none(self, _live_snapshot_env):
        """Unknown camera_id returns None without calling ReolinkClient."""
        with patch("infra.farm_api.cameras.get_camera", return_value=None):
            from infra.farm_api.cameras import get_live_snapshot

            result = get_live_snapshot("UNKNOWN_CAM")

            assert result is None

    def test_snapshot_client_instantiated_with_gateway(self, _live_snapshot_env):
        """ReolinkClient is instantiated with the camera's gateway address."""
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100

        mock_client = _make_mock_client(fake_jpeg)
        mock_cam = {
            "prefix": "BACK",
            "gateway": "100.64.0.2",
            "ip": os.environ.get("FARM_TEST_BACK_IP", "10.0.0.85"),
        }

        with (
            patch("infra.farm_api.cameras.get_camera", return_value=mock_cam),
            patch(
                "infra.reolink.ReolinkClient", return_value=mock_client
            ),
        ):
            from infra.farm_api.cameras import get_live_snapshot

            get_live_snapshot("BACK")

            mock_client.snapshot.assert_called_once()
            call_kwargs = mock_client.snapshot.call_args
            assert call_kwargs.kwargs["camera"] == "BACK"
