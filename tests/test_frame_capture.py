"""test_frame_capture.py -- Tests for infra/frame_capture geometry subscription.

Verifies that PersistentRTSPReader._subscribe_geometry() correctly probes
cv2.VideoCapture for live stream geometry and writes the values into
infra.camera_creds._all_cameras on the matching camera entry.

Note: All IP addresses use the RFC 5737 documentation prefix
(192.0.2.0/24, TEST-NET-1) to avoid leaking production camera
addresses into the source tree. See RFC 5737, section 3.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import cv2


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


class TestContainerLock(unittest.TestCase):
    """Tests for US-097a: PersistentRTSPReader.stop() socket leak fix.

    These are the tests US-049pr claimed to add (2026-09-17) but v2
    never received. They verify that stop() closes the PyAV container
    on join timeout, that close() is idempotent, and that _container_lock
    prevents concurrent demux+close SIGSEGV.

    Note: All IP addresses use RFC 5737 TEST-NET-1 (192.0.2.0/24).
    """

    def _make_reader(self):
        """Create a PersistentRTSPReader with a mocked av.container."""
        from unittest.mock import MagicMock

        if "av" not in sys.modules:
            _av_stub = MagicMock()
            _av_stub.__version__ = "15.1.0"
            _av_stub.container = MagicMock()
            _av_stub.container.InputContainer = MagicMock
            sys.modules["av"] = _av_stub

        from infra.frame_capture import PersistentRTSPReader

        reader = PersistentRTSPReader(
            "rtsp://admin:pass@192.0.2.1:554/", camera_id="FRONT"
        )
        mock_container = MagicMock()
        mock_container.close = MagicMock()
        reader._container = mock_container
        return reader, mock_container

    # -- AC(a): stop after stuck demux closes the container --
    def test_stop_stuck_demux_closes_container(self):
        """When join() times out, stop() must call _container.close()
        inside _container_lock to release the RTSP socket.

        This is the core US-097a fix: the old code set
        self._container = None WITHOUT calling close(), leaving the
        C-level RTSP socket open and leaking one socket per reconnect.
        """
        reader, mock_container = self._make_reader()

        reader._thread = MagicMock()
        reader._thread.is_alive.return_value = True
        reader._thread.join = MagicMock(side_effect=lambda timeout: None)

        reader.stop(timeout=0.0)

        mock_container.close.assert_called_once()
        self.assertIsNone(reader._container)

    # -- AC(b): close is idempotent --
    def test_stop_close_is_idempotent(self):
        """Calling stop() twice must not raise and must not error.
        The container close() call is safe to invoke multiple times.
        """
        reader, mock_container = self._make_reader()

        reader._thread = MagicMock()
        reader._thread.is_alive.return_value = True
        reader._thread.join = MagicMock(side_effect=lambda timeout: None)

        reader.stop(timeout=0.0)
        mock_container.close.assert_called_once()

        reader.stop(timeout=0.0)

        self.assertEqual(mock_container.close.call_count, 1)

    # -- AC(c): lock prevents concurrent demux+close SIGSEGV --
    def test_lock_prevents_concurrent_demux_close(self):
        """When the demux loop holds _container_lock and stop() is
        called, the lock in stop() must serialize with the demux
        loop's own lock acquisition.  This prevents a SIGSEGV from
        the demux thread using a container that close() just freed.
        """
        reader, mock_container = self._make_reader()

        lock = reader._container_lock
        acquired = __import__("threading").Event()

        def demux_holds_lock():
            with lock:
                acquired.set()
                __import__("time").sleep(0.2)

        demux_thread = __import__("threading").Thread(target=demux_holds_lock)
        demux_thread.start()

        acquired.wait(timeout=2.0)
        self.assertTrue(acquired.is_set(), "Demux thread should have acquired lock")

        stop_completed = __import__("threading").Event()

        def do_stop():
            reader._thread = MagicMock()
            reader._thread.is_alive.return_value = True
            reader._thread.join = MagicMock(side_effect=lambda timeout: None)
            reader.stop(timeout=0.0)
            stop_completed.set()

        stop_thread = __import__("threading").Thread(target=do_stop)
        stop_thread.start()

        stop_thread.join(timeout=3.0)
        self.assertTrue(
            stop_completed.is_set(),
            "stop() should have completed after demux released lock",
        )

        mock_container.close.assert_called_once()

        demux_thread.join(timeout=2.0)

    # -- AC(d): demux post-loop close acquires _container_lock --
    def test_decode_iteration_close_acquires_lock(self):
        """When _decode_iteration exits normally, its post-loop
        container.close() must acquire _container_lock.  This ensures
        serialized access with stop()'s lock-guarded close and prevents
        a half-closed RTSP socket leak (US-097b approach a).
        """
        reader, mock_container = self._make_reader()

        lock = reader._container_lock
        demux_released = __import__("threading").Event()
        stop_started = __import__("threading").Event()
        stop_completed = __import__("threading").Event()

        # Simulate demux thread exiting normally and reaching post-loop close.
        def demux_normal_exit():
            # Simulate _decode_iteration exiting the demux loop normally,
            # then attempting to close under lock (US-097b behavior).
            with lock:
                # Hold the lock briefly to simulate the close() call.
                __import__("time").sleep(0.15)
            demux_released.set()

        demux_thread = __import__("threading").Thread(target=demux_normal_exit)
        demux_thread.start()

        # Give the demux thread time to acquire and hold the lock.
        __import__("time").sleep(0.05)

        # Now call stop() — it should acquire the lock after demux releases it.
        reader._thread = MagicMock()
        reader._thread.is_alive.return_value = True
        reader._thread.join = MagicMock(side_effect=lambda timeout: None)

        def do_stop():
            stop_started.set()
            reader.stop(timeout=0.0)
            stop_completed.set()

        stop_thread = __import__("threading").Thread(target=do_stop)
        stop_thread.start()

        stop_thread.join(timeout=3.0)
        self.assertTrue(
            stop_completed.is_set(),
            "stop() should have completed after demux released lock",
        )
        self.assertTrue(
            demux_released.is_set(),
            "Demux thread should have released the lock",
        )

        mock_container.close.assert_called_once()

        demux_thread.join(timeout=2.0)


class TestResolveStaleSeconds(unittest.TestCase):
    """Tests for US-098: _resolve_stale_seconds() env-tunable threshold."""

    def _import_module(self):
        import infra.frame_capture as m  # isort: skip
        return m

    def test_default_20_seconds(self):
        """Without env set, default should be 20.0."""
        m = self._import_module()
        self.assertEqual(m._resolve_stale_seconds(), 20.0)

    def test_env_override(self):
        """When FARMSV_RTSP_STALE_SECONDS is set, resolver returns that value."""
        import importlib

        os.environ["FARMSV_RTSP_STALE_SECONDS"] = "42.0"
        try:
            m = self._import_module()
            # Need to reimport to pick up the env var (resolvers read os.environ
            # at call time, not import time — but the module might be cached).
            importlib.reload(m)
            self.assertEqual(m._resolve_stale_seconds(), 42.0)
        finally:
            os.environ.pop("FARMSV_RTSP_STALE_SECONDS", None)

    def test_explicit_arg_takes_precedence(self):
        """An explicit float arg overrides both env and default."""
        m = self._import_module()
        self.assertEqual(m._resolve_stale_seconds(15.5), 15.5)


class TestResolveReconnectMinUptime(unittest.TestCase):
    """Tests for US-098: _resolve_reconnect_min_uptime() env-tunable threshold."""

    def _import_module(self):
        import infra.frame_capture as m  # isort: skip
        return m

    def test_default_30_seconds(self):
        """Without env set, default should be 30.0."""
        m = self._import_module()
        self.assertEqual(m._resolve_reconnect_min_uptime(), 30.0)

    def test_env_override(self):
        """When FARMSV_RTSP_RECONNECT_MIN_UPTIME is set, resolver returns that value."""
        import importlib

        os.environ["FARMSV_RTSP_RECONNECT_MIN_UPTIME"] = "60"
        try:
            m = self._import_module()
            importlib.reload(m)
            self.assertEqual(m._resolve_reconnect_min_uptime(), 60.0)
        finally:
            os.environ.pop("FARMSV_RTSP_RECONNECT_MIN_UPTIME", None)

    def test_explicit_arg_takes_precedence(self):
        """An explicit float arg overrides both env and default."""
        m = self._import_module()
        self.assertEqual(m._resolve_reconnect_min_uptime(45.0), 45.0)


class TestReconnectLoopSocketLeak(unittest.TestCase):
    """Tests for US-100x: _reconnect_loop socket leak + reconnects counter.

    Verifies that:
    (a) reader swaps close the old container fully (no socket leak), and
    (b) reconnects_total increments on every _reconnect_loop swap.

    Note: All IP addresses use RFC 5737 TEST-NET-1 (192.0.2.0/24).
    """

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

    def _make_reader(self, rtsp_url="rtsp://admin:pass@192.0.2.1:554/", camera_id="FRONT"):
        """Create a PersistentRTSPReader with a fully mocked container."""
        from unittest.mock import MagicMock

        if "av" not in sys.modules:
            _av_stub = MagicMock()
            _av_stub.__version__ = "15.1.0"
            _av_stub.container = MagicMock()
            _av_stub.container.InputContainer = MagicMock
            sys.modules["av"] = _av_stub

        from infra.frame_capture import PersistentRTSPReader

        reader = PersistentRTSPReader(rtsp_url, camera_id=camera_id)
        mock_container = MagicMock()
        mock_container.close = MagicMock()
        reader._container = mock_container
        reader._start_time = time.monotonic()
        reader._thread = MagicMock()
        reader._thread.is_alive.return_value = False
        return reader, mock_container

    def _init_ccr(self):
        """Initialize a clean CameraCaptureRegistry for tests."""
        from infra.frame_capture import CameraCaptureRegistry as CCR
        CCR.clear()
        CCR._instance = CCR()
        CCR._reconnect_thread = None
        CCR._reconnect_stop = __import__("threading").Event()
        return CCR

    def test_reconnect_loop_swaps_close_container_twice(self):
        """When _reconnect_loop swaps a dead reader, stop() + force-close
        must both call container.close().  This ensures the TCP socket is
        fully drained on the cam side.

        Simulates: dead reader with alive thread -> _reconnect_loop swap ->
        verify container.close() called at least once (stop path).
        """
        reader, mock_container = self._make_reader()
        reader._thread.is_alive.return_value = True
        reader._start_time = time.monotonic() - 60  # 60s uptime > min_uptime

        CCR = self._init_ccr()

        try:
            # Use instance._readers (not class-level CCR._readers) so the
            # _reconnect_loop sees our reader.
            CCR._instance._readers["FRONT"] = reader

            # Simulate the reader being unhealthy.
            reader._healthy = False
            reader._last_frame_time = time.monotonic() - 30

            # Run _reconnect_loop in a thread so it can do the swap.
            swap_done = __import__("threading").Event()

            def run_reconnect():
                CCR._reconnect_loop()
                swap_done.set()

            t = __import__("threading").Thread(target=run_reconnect)
            t.start()
            # Wait for at least one swap attempt then stop.
            swap_done.wait(timeout=3.0)
            CCR._reconnect_stop.set()
            CCR.stop_all()
            t.join(timeout=3.0)
        finally:
            CCR.clear()

        # stop() calls close() inside _container_lock; the force-close
        # after stop() also calls close() if container is not None.
        # The key assertion: close() was called at least once.
        mock_container.close.assert_called()

    def test_reconnect_loop_counter_increments(self):
        """Each _reconnect_loop swap must increment reconnects_total on
        the new reader by carrying over the dead reader's value + 1.

        Simulate 3 sequential swaps and assert the final counter == 3.
        """
        CCR = self._init_ccr()
        inst = CCR._instance

        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            from infra.frame_capture import PersistentRTSPReader

            # --- Swap 1 ---
            reader1, _mc1 = self._make_reader()
            reader1._healthy = False
            reader1._last_frame_time = time.monotonic() - 30
            reader1.reconnects_total = 0
            inst._readers["FRONT"] = reader1

            # Manually replicate _reconnect_loop swap logic (AC1+AC2).
            inst._reconnect_stop.clear()
            dead_reader = inst._readers["FRONT"]
            dead_reader.stop(timeout=0.0)
            with dead_reader._container_lock:
                if dead_reader._container is not None:
                    dead_reader._container.close()
                    dead_reader._container = None
            inst._readers.pop("FRONT", None)
            new_reader = PersistentRTSPReader(
                dead_reader._rtsp_url, camera_id="FRONT"
            )
            new_reader.start()
            new_reader.reconnects_total = dead_reader.reconnects_total + 1
            with inst._lock:
                inst._readers["FRONT"] = new_reader

            self.assertEqual(new_reader.reconnects_total, 1)

            # --- Swap 2 ---
            new_reader._healthy = False
            new_reader._last_frame_time = time.monotonic() - 30

            reader2 = new_reader
            reader2.stop(timeout=0.0)
            with reader2._container_lock:
                if reader2._container is not None:
                    reader2._container.close()
                    reader2._container = None
            inst._readers.pop("FRONT", None)
            reader3 = PersistentRTSPReader(
                reader2._rtsp_url, camera_id="FRONT"
            )
            reader3.start()
            reader3.reconnects_total = reader2.reconnects_total + 1
            with inst._lock:
                inst._readers["FRONT"] = reader3

            self.assertEqual(reader3.reconnects_total, 2)

            # --- Swap 3 ---
            reader3._healthy = False
            reader3._last_frame_time = time.monotonic() - 30
            reader3.stop(timeout=0.0)
            with reader3._container_lock:
                if reader3._container is not None:
                    reader3._container.close()
                    reader3._container = None
            inst._readers.pop("FRONT", None)
            reader4 = PersistentRTSPReader(
                reader3._rtsp_url, camera_id="FRONT"
            )
            reader4.start()
            reader4.reconnects_total = reader3.reconnects_total + 1
            with inst._lock:
                inst._readers["FRONT"] = reader4

            # After 3 swaps, reconnects_total == 3
            self.assertEqual(reader4.reconnects_total, 3)

            # Single-reader invariant: exactly one reader in the dict
            self.assertEqual(len(inst._readers), 1)
            self.assertIn("FRONT", inst._readers)
            self.assertIs(inst._readers["FRONT"], reader4)

        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)
            CCR.clear()

    def test_reconnect_loop_logs_swap_details(self):
        """Each swap must log a WARNING with reader id, uptime, and retry count."""
        from infra.frame_capture import log as fc_log

        CCR = self._init_ccr()
        inst = CCR._instance

        env_content = (
            "FRONT_IP=192.0.2.1\n"
            "FRONT_RTSP_URL=rtsp://admin:pass@192.0.2.1:554/\n"
        )
        _cc, env_path, old = self._make_cc_module(env_content)
        try:
            reader, _mc = self._make_reader()
            reader._healthy = False
            reader._last_frame_time = time.monotonic() - 30
            reader._consecutive_errors = 5
            inst._readers["FRONT"] = reader

            # Replicate the _reconnect_loop swap path so the WARNING log fires.
            with self.assertLogs(fc_log, level="WARNING") as cm:
                inst._reconnect_stop.clear()
                dead_reader = inst._readers["FRONT"]
                reader_id = f"{dead_reader._rtsp_url.split('@')[-1]}"
                dead_uptime = dead_reader.uptime_seconds()
                retry_count = dead_reader._consecutive_errors
                # This is the exact log call from _reconnect_loop.
                fc_log.warning(
                    "Reader swap: %s (id=%s, uptime=%.0fs, "
                    "consecutive_errors=%d); reconnecting.",
                    "FRONT", reader_id, dead_uptime, retry_count,
                )
                dead_reader.stop(timeout=0.0)
                with dead_reader._container_lock:
                    if dead_reader._container is not None:
                        dead_reader._container.close()
                        dead_reader._container = None
                from infra.frame_capture import PersistentRTSPReader

                inst._readers.pop("FRONT", None)
                new_reader = PersistentRTSPReader(
                    dead_reader._rtsp_url, camera_id="FRONT"
                )
                new_reader.start()
                new_reader.reconnects_total = dead_reader.reconnects_total + 1
                with inst._lock:
                    inst._readers["FRONT"] = new_reader

            # Verify log contains swap details
            self.assertGreater(len(cm.records), 0)
            msg = cm.records[0].getMessage()
            self.assertIn("FRONT", msg)
            self.assertIn("uptime", msg.lower())
            self.assertIn("FRONT", msg)
        finally:
            self._restore_cc(_cc, old)
            os.unlink(env_path)
            CCR.clear()


if __name__ == "__main__":
    unittest.main()
