"""test_frame_capture.py -- Tests for infra/frame_capture geometry subscription.

Verifies that PersistentRTSPReader._subscribe_geometry() correctly probes
cv2.VideoCapture for live stream geometry and writes the values into
infra.camera_creds._all_cameras on the matching camera entry.

Note: All IP addresses use the RFC 5737 documentation prefix
(192.0.2.0/24, TEST-NET-1) to avoid leaking production camera
addresses into the source tree. See RFC 5737, section 3.
"""

from __future__ import annotations

import cv2
import importlib
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestSubscribeGeometry(unittest.TestCase):
    """Tests for PersistentRTSPReader._subscribe_geometry()."""

    def _make_cc_module(self, env_content: str):
        """Write a temp env file and return the module, patching the creds path."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False
        ) as f:
            f.write(env_content)
            f.flush()
            env_path = f.name

        old = os.environ.get("FARM_CAMERA_CREDS_FILE")
        os.environ["FARM_CAMERA_CREDS_FILE"] = env_path
        import infra.camera_creds as _cc
        importlib.reload(_cc)

        return _cc, env_path, old

    def _restore_cc(self, _cc, old):
        os.environ.pop("FARM_CAMERA_CREDS_FILE", None)
        if old is not None:
            os.environ["FARM_CAMERA_CREDS_FILE"] = old
        importlib.reload(_cc)

    def setUp(self):
        # Ensure av stub is available (same as conftest).
        import sys
        if "av" not in sys.modules:
            from unittest.mock import MagicMock
            _av_stub = MagicMock()
            _av_stub.__version__ = "15.1.0"
            _av_stub.container = MagicMock()
            _av_stub.container.InputContainer = MagicMock
            sys.modules["av"] = _av_stub

    def _get_cap_prop(self, w: int, h: int, fps: float):
        """Build a side_effect function for cap.get()."""
        def side_effect(prop):
            prop_map = {
                cv2.CAP_PROP_FRAME_WIDTH: w,
                cv2.CAP_PROP_FRAME_HEIGHT: h,
                cv2.CAP_PROP_FPS: fps,
            }
            return prop_map.get(prop, 0.0)
        return side_effect

    # -- AC1: geometry written to camera_creds --
    def test_subscribe_geometry_writes_to_camera_creds(self):
        """Mock cap.get returning (3840, 2160, 2.0); call _subscribe_geometry;
        assert infra.camera_creds.get_frame_geometry(cam_id) has the values."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.get.side_effect = self._get_cap_prop(3840, 2160, 2.0)
            mock_cap.release = MagicMock()

            from infra.frame_capture import PersistentRTSPReader

            reader = PersistentRTSPReader(
                "rtsp://admin:pass@192.0.2.1:554/", camera_id="FRONT"
            )
            with patch("cv2.VideoCapture", return_value=mock_cap):
                reader._camera_id = "FRONT"
                reader._subscribe_geometry()

            # Verify geometry written
            geo = _cc.get_frame_geometry("FRONT")
            self.assertIsNotNone(geo)
            self.assertEqual(geo["width"], 3840)
            self.assertEqual(geo["height"], 2160)
            self.assertEqual(geo["fps"], 2.0)
        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)

    # -- AC2: INFO log emitted --
    def test_subscribe_geometry_logs_info_line(self):
        """Assert the INFO log line is emitted with the correct format string."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.get.side_effect = self._get_cap_prop(3840, 2160, 2.0)

            import logging
            from infra.frame_capture import PersistentRTSPReader, log as fc_log

            reader = PersistentRTSPReader(
                "rtsp://admin:pass@192.0.2.1:554/", camera_id="FRONT"
            )
            reader._camera_id = "FRONT"

            with patch("cv2.VideoCapture", return_value=mock_cap):
                with self.assertLogs(fc_log, level="INFO") as cm:
                    reader._subscribe_geometry()

            self.assertEqual(len(cm.records), 1)
            msg = cm.records[0].getMessage()
            self.assertIn("FRONT", msg)
            self.assertIn("3840x2160", msg)
            self.assertIn("2.0", msg)
            self.assertIn("unknown", msg)
        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)

    # -- AC3: zero dimensions --
    def test_subscribe_geometry_handles_zero_dimensions(self):
        """Mock cap.get returning (0, 0, 0); assert WARNING logged and
        width/height are not written."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
            "FRONT_WIDTH=3840\n"
            "FRONT_HEIGHT=2160\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.get.side_effect = self._get_cap_prop(0, 0, 0.0)

            import logging
            from infra.frame_capture import PersistentRTSPReader, log as fc_log

            reader = PersistentRTSPReader(
                "rtsp://admin:pass@192.0.2.1:554/", camera_id="FRONT"
            )
            reader._camera_id = "FRONT"

            with patch("cv2.VideoCapture", return_value=mock_cap):
                with self.assertLogs(fc_log, level="WARNING") as cm:
                    reader._subscribe_geometry()

            # Should have logged the zero-dim warning
            msgs = [r.getMessage() for r in cm.records]
            self.assertTrue(
                any("could not subscribe geometry" in m for m in msgs),
                f"Expected zero-dim warning, got: {msgs}",
            )

            # width/height should NOT be overwritten -- keep original
            geo = _cc.get_frame_geometry("FRONT")
            self.assertEqual(geo["width"], 3840)
            self.assertEqual(geo["height"], 2160)
        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)

    # -- AC4: substream warning --
    def test_subscribe_geometry_substream_warning(self):
        """Mock cap.get returning (640, 480, 25.0); assert SUBSTREAM WARNING fires."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.get.side_effect = self._get_cap_prop(640, 480, 25.0)

            import logging
            from infra.frame_capture import PersistentRTSPReader, log as fc_log

            reader = PersistentRTSPReader(
                "rtsp://admin:pass@192.0.2.1:554/", camera_id="FRONT"
            )
            reader._camera_id = "FRONT"

            with patch("cv2.VideoCapture", return_value=mock_cap):
                with self.assertLogs(fc_log, level="WARNING") as cm:
                    reader._subscribe_geometry()

            msgs = [r.getMessage() for r in cm.records]
            self.assertTrue(
                any("negotiated substream" in m for m in msgs),
                f"Expected substream warning, got: {msgs}",
            )

            # Geometry should still be written (substream warning doesn't block)
            geo = _cc.get_frame_geometry("FRONT")
            self.assertEqual(geo["width"], 640)
            self.assertEqual(geo["height"], 480)
            self.assertEqual(geo["fps"], 25.0)
        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)

    # -- No camera_id: no-op --
    def test_subscribe_geometry_no_camera_id_is_noop(self):
        """When camera_id is None, _subscribe_geometry returns without
        opening cv2.VideoCapture."""
        from infra.frame_capture import PersistentRTSPReader

        reader = PersistentRTSPReader("rtsp://test:554/")
        # _camera_id is None by default
        with patch("cv2.VideoCapture") as mock_cv2:
            reader._subscribe_geometry()
        mock_cv2.assert_not_called()

    # -- cv2 fails to open --
    def test_subscribe_geometry_cv2_probe_fails(self):
        """When cv2.VideoCapture fails to open, WARNING is logged and
        geometry is not written."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
            "FRONT_WIDTH=3840\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = False

            import logging
            from infra.frame_capture import PersistentRTSPReader, log as fc_log

            reader = PersistentRTSPReader(
                "rtsp://admin:pass@192.0.2.1:554/", camera_id="FRONT"
            )
            reader._camera_id = "FRONT"

            with patch("cv2.VideoCapture", return_value=mock_cap):
                with self.assertLogs(fc_log, level="WARNING") as cm:
                    reader._subscribe_geometry()

            msgs = [r.getMessage() for r in cm.records]
            self.assertTrue(
                any("cv2 probe failed" in m for m in msgs),
                f"Expected cv2 probe warning, got: {msgs}",
            )

            # width should remain at original value, not be overwritten
            geo = _cc.get_frame_geometry("FRONT")
            self.assertEqual(geo["width"], 3840)
        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)

    # -- frame_format from camera_creds --
    def test_subscribe_geometry_includes_frame_format(self):
        """The INFO log includes the frame_format from camera_creds."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
            "FRONT_FRAME_FORMAT=H265\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            mock_cap = MagicMock()
            mock_cap.isOpened.return_value = True
            mock_cap.get.side_effect = self._get_cap_prop(3840, 2160, 2.0)

            import logging
            from infra.frame_capture import PersistentRTSPReader, log as fc_log

            reader = PersistentRTSPReader(
                "rtsp://admin:pass@192.0.2.1:554/", camera_id="FRONT"
            )
            reader._camera_id = "FRONT"

            with patch("cv2.VideoCapture", return_value=mock_cap):
                with self.assertLogs(fc_log, level="INFO") as cm:
                    reader._subscribe_geometry()

            msg = cm.records[0].getMessage()
            self.assertIn("H265", msg)
        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)

    # -- no matching camera_creds entry --
    def test_subscribe_geometry_no_matching_cc_entry(self):
        """When no camera_creds entry matches the camera_id, DEBUG log
        is emitted and no crash occurs."""
        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            from infra.frame_capture import PersistentRTSPReader

            reader = PersistentRTSPReader(
                "rtsp://admin:pass@192.0.2.1:554/", camera_id="UNKNOWN"
            )
            reader._camera_id = "UNKNOWN"

            with patch("cv2.VideoCapture") as mock_cv2:
                with self.assertLogs("frame_capture", level="DEBUG") as cm:
                    reader._subscribe_geometry()

            mock_cv2.assert_not_called()
            msgs = [r.getMessage() for r in cm.records]
            self.assertTrue(
                any("no camera_creds entry" in m for m in msgs),
                f"Expected debug skip, got: {msgs}",
            )
        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)


if __name__ == "__main__":
    unittest.main()
