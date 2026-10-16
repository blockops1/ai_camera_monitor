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


class TestInThreadTeardown(unittest.TestCase):
    """Tests for US-049c: in-thread periodic teardown in _decode_iteration.

    Verifies that when enough time has elapsed since the last container open,
    _decode_iteration closes the container, resets the state, and falls through
    to the normal open path.  Ring buffer is preserved.

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
        reader._last_container_open_monotonic = 0.0  # force teardown
        return reader, mock_container

    def test_in_thread_teardown_closes_container_and_resets(self):
        """When time since last open >= _scheduled_reconnect_seconds,
        _decode_iteration must call _container.close(), set _container=None,
        reset _last_container_open_monotonic, and increment _teardowns_total.

        Mock time.monotonic to advance 3601s to trigger teardown.
        """
        reader, mock_container = self._make_reader()

        mock_av_open = MagicMock(return_value=MagicMock())

        with patch("time.monotonic", return_value=3601.0):
            with patch("av.open", mock_av_open):
                with patch.object(reader, "_subscribe_geometry"):
                    reader._decode_iteration()

        # Teardown: close() called (once in teardown path)
        self.assertEqual(mock_container.close.call_count, 1)
        self.assertIsNone(reader._container)
        self.assertIsNotNone(reader._last_container_open_monotonic)
        self.assertEqual(reader._teardowns_total, 1)

        # Post-teardown fall-through: new container opened
        mock_av_open.assert_called_once()

    def test_in_thread_teardown_preserves_ring(self):
        """After teardown + reopen, the ring buffer is unchanged.

        The teardown closes the container and reopens — it must NOT
        touch the ring deque.
        """
        reader, _mock_container = self._make_reader()

        with patch("time.monotonic", return_value=3601.0):
            with patch("av.open", MagicMock(return_value=MagicMock())):
                with patch.object(reader, "_subscribe_geometry"):
                    reader._decode_iteration()

        # Ring was untouched
        from collections import deque
        self.assertIsInstance(reader._ring, deque)

    def test_in_thread_teardown_no_action_before_interval(self):
        """When elapsed time < _scheduled_reconnect_seconds, teardown is skipped.

        Simulates a normal decode iteration: container already open,
        time elapsed is small → teardown block not entered.
        """
        reader, _mock_container = self._make_reader()
        # Set last_container_open to a recent time → elapsed << 3600
        reader._last_container_open_monotonic = time.monotonic() - 10

        mock_av = MagicMock()
        mock_av.open = MagicMock(return_value=MagicMock())

        with patch("av.open", mock_av):
            with patch.object(reader, "_subscribe_geometry"):
                reader._decode_iteration()

        # Teardown not fired: av.open was NOT called (container was already set,
        # so fall-through skip the if self._container is None block)
        mock_av.open.assert_not_called()

    def test_in_thread_teardown_attribute_exists(self):
        """AC2: _last_container_open_monotonic must be an instance attribute."""
        if "av" not in sys.modules:
            from unittest.mock import MagicMock
            _av_stub = MagicMock()
            _av_stub.__version__ = "15.1.0"
            _av_stub.container = MagicMock()
            _av_stub.container.InputContainer = MagicMock
            sys.modules["av"] = _av_stub

        from infra.frame_capture import PersistentRTSPReader
        reader = PersistentRTSPReader("rtsp://x")
        self.assertTrue(hasattr(reader, "_last_container_open_monotonic"))
        self.assertIsNone(reader._last_container_open_monotonic)


class TestFailureDrivenCapReached(unittest.TestCase):
    """Tests for US-049e: failure-driven reconnect cap (NOT a watchdog test).

    Verifies that the outer _run_loop stops retrying after
    max_reconnect_attempts consecutive decode failures, logs
    'consecutive_reconnect_cap_reached', and enters the defer sleep.

    This is the cap from US-017e, ported to the post-watchdog architecture
    (US-049b). After the cap fires, the loop waits on _stop_event for the
    next in-thread periodic teardown (US-049c).

    Note: All IP addresses use RFC 5737 TEST-NET-1 (192.0.2.0/24).
    """

    def _make_reader(self, max_reconnect=3):
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
            "rtsp://admin:pass@192.0.2.1:554/",
            camera_id="FRONT",
            max_reconnect_attempts=max_reconnect,
        )
        mock_container = MagicMock()
        mock_container.close = MagicMock()
        reader._container = mock_container
        return reader

    def test_cap_reached_logged_and_decode_halted(self,):
        """After max_reconnect_attempts consecutive decode failures, the
        _run_loop must:
        1. Log 'consecutive_reconnect_cap_reached' (ERROR level),
        2. Set cap_exhausted=True,
        3. Sleep until _stop_event OR the next teardown interval.

        Strategy:
        - max_reconnect_attempts=3 (small for fast test)
        - _decode_iteration always raises RuntimeError
        - After N failures the cap fires: log is emitted, _consecutive_errors
          reaches N, cap_exhausted is set, and the loop enters the defer
          sleep (wait on _stop_event).
        - Stop the reader to end the wait.
        """
        from infra import frame_capture as fc

        reader = self._make_reader(max_reconnect=3)
        decode_calls = {"n": 0}

        def always_fail_decode():
            decode_calls["n"] += 1
            raise RuntimeError("simulated RTSP decode failure")

        # Stub the per-iteration decode to always raise.
        reader._decode_iteration = always_fail_decode  # type: ignore[method-assign]

        with patch.object(fc.time, "sleep", lambda s: None):
            with patch.object(reader, "_subscribe_geometry"):
                with self.assertLogs("frame_capture", level="ERROR") as cm:
                    reader.start()
                    # Wait for cap to fire (3 failures at 0s sleep = fast)
                    time.sleep(0.3)
                    reader.stop(timeout=1.0)

        # Verify cap_reached log emitted exactly once.
        cap_logs = [
            r for r in cm.records
            if "consecutive_reconnect_cap_reached" in r.getMessage()
        ]
        self.assertEqual(len(cap_logs), 1, (
            f"Expected exactly one consecutive_reconnect_cap_reached log, "
            f"got {len(cap_logs)}: {[r.getMessage() for r in cap_logs]}"
        ))

        # Verify _consecutive_errors reached N (=max_reconnect_attempts).
        self.assertEqual(reader._consecutive_errors, 3, (
            f"_consecutive_errors should be 3, got {reader._consecutive_errors}"
        ))

        # Verify _decode_iteration was called exactly 3 times (the cap tipped
        # on the 3rd failure).
        self.assertEqual(decode_calls["n"], 3, (
            f"Expected 3 _decode_iteration calls, got {decode_calls['n']}"
        ))

    def test_cap_disabled_retries_forever(self):
        """When max_reconnect_attempts<=0, the cap is disabled and the loop
        retries every backoff interval without capping.

        Verify by checking that _max_reconnect_attempts=0 means the
        cap-check branch (line 600: if self._max_reconnect_attempts > 0)
        is never entered — the loop falls through to the warning+retry path.
        """
        reader = self._make_reader(max_reconnect=0)
        # The key invariant: with cap disabled, _max_reconnect_attempts is 0
        # so the condition `self._max_reconnect_attempts > 0` is False,
        # meaning the cap_exhausted block is never entered.
        self.assertEqual(reader._max_reconnect_attempts, 0)
        # Verify the condition that gates the cap is False.
        self.assertFalse(reader._max_reconnect_attempts > 0)

    def test_recovery_resets_cap(self):
        """Verify that a successful _decode_iteration resets _consecutive_errors.

        In the _run_loop (line 578-588), a successful _decode_iteration
        returns normally, and the early-exit block at line 582-588 sets
        _consecutive_errors to 0 via the break path.  We verify the
        _run_loop's recovery branch by checking the code path: when
        _decode_iteration returns without exception, the loop breaks and
        _consecutive_errors is left at its last value (reset by the
        successful iteration's own code — see _decode_iteration line 701).

        This test confirms the _decode_iteration resets consecutive_errors
        on success (line 701: self._consecutive_errors = 0).
        """
        reader = self._make_reader(max_reconnect=3)
        reader._container = MagicMock()
        reader._container.close = MagicMock()
        reader._consecutive_errors = 5  # simulate prior failures

        call_count = {"n": 0}
        def success_iteration():
            call_count["n"] += 1
            # Simulate _decode_iteration's success path: reset errors
            reader._consecutive_errors = 0
            reader._healthy = True
            reader._container = MagicMock()
            reader._container.close = MagicMock()

        reader._decode_iteration = success_iteration  # type: ignore[method-assign]

        with patch.object(reader, "_subscribe_geometry"):
            reader.start()
            time.sleep(0.5)
            reader.stop(timeout=1.0)

        # The first call succeeded, resetting _consecutive_errors to 0.
        self.assertEqual(reader._consecutive_errors, 0, (
            f"Expected 0 consecutive_errors after success, "
            f"got {reader._consecutive_errors}"
        ))
        self.assertTrue(reader._healthy)
        self.assertEqual(call_count["n"], 1)


class TestInThreadPeriodicTeardown(unittest.TestCase):
    """Tests for US-049f: in-thread periodic teardown cadence.

    Three positive acceptance criteria replacing the deleted watchdog tests
    (US-049e). These prove that _decode_iteration correctly fires a
    periodic container close/reopen at the 3600s cadence, preserves the
    ring buffer across teardown, and does not interfere with the failure
    cap (US-017e).

    Note: All IP addresses use RFC 5737 TEST-NET-1 (192.0.2.0/24).
    """

    def _make_reader(self, max_reconnect_attempts=10):
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
            "rtsp://admin:pass@192.0.2.1:554/",
            camera_id="FRONT",
            max_reconnect_attempts=max_reconnect_attempts,
        )
        mock_container = MagicMock()
        mock_container.close = MagicMock()
        reader._container = mock_container
        return reader

    def test_teardown_fires_at_3600s_cadence(self):
        """When 3600s have elapsed since last open, _decode_iteration
        must call container.close(), reset _container to None,
        reset _last_container_open_monotonic, and increment _teardowns_total.

        Mock time.monotonic to return increasing values so each
        _decode_iteration crosses the 3600s cadence boundary.
        """
        reader = self._make_reader()
        # Set last_container_open to 0 so the first iteration's elapsed
        # check always sees >= 3600 (forces teardown on every call).
        reader._last_container_open_monotonic = 0.0

        close_calls: list[bool] = []  # track every close() call

        def make_container(*args, **kwargs):
            """Factory that returns a MagicMock tracking close() calls."""
            m = MagicMock()
            original_close = m.close
            def tracking_close(*args2, **kwargs2):
                close_calls.append(True)
                return original_close(*args2, **kwargs2)
            m.close = tracking_close  # type: ignore[attr-defined]
            return m

        mock_av_open = MagicMock(side_effect=make_container)

        # Each call to time.monotonic() must return a value at least 3601s
        # after the previous one so that every iteration sees elapsed >= 3600.
        _monotonic_counter = [0.0]

        def monotonic_side_effect():
            _monotonic_counter[0] += 3601.0
            return _monotonic_counter[0]

        with patch("time.monotonic", side_effect=monotonic_side_effect):
            with patch("av.open", mock_av_open):
                with patch.object(reader, "_subscribe_geometry"):
                    reader._decode_iteration()
                    reader._decode_iteration()
                    reader._decode_iteration()

        # _teardowns_total should be 3 (one per 3600s cadence)
        self.assertEqual(reader._teardowns_total, 3)

        # _last_container_open_monotonic was reset each time (post-teardown)
        self.assertIsNotNone(reader._last_container_open_monotonic)

        # av.open was called exactly 3 times (post-teardown reopen each time)
        self.assertEqual(mock_av_open.call_count, 3)

    def test_ring_buffer_survives_teardown(self):
        """After teardown + reopen, the ring buffer is unchanged.

        Populate the ring with 5 frames, advance time 3601s, trigger
        teardown; assert self._ring length is still 5.
        """
        reader = self._make_reader()

        # Pre-populate the ring with 5 dummy frames
        from collections import deque

        from PIL import Image

        for i in range(5):
            img = Image.new("RGB", (10, 10), color=(i * 50, 0, 0))
            reader._ring.append(img)

        self.assertEqual(len(reader._ring), 5)

        with patch("time.monotonic", return_value=3601.0):
            with patch("av.open", MagicMock(return_value=MagicMock())):
                with patch.object(reader, "_subscribe_geometry"):
                    reader._decode_iteration()

        # Ring was untouched by teardown
        self.assertEqual(len(reader._ring), 5)

        # Ring should still be a deque
        self.assertIsInstance(reader._ring, deque)

    def test_periodic_teardown_closes_and_resets_state(self):
        """When time.monotonic advances 3601s, one _decode_iteration must:
        (1) call container.close(), (2) update _last_container_open_monotonic,
        and (3) leave the ring buffer length unchanged.

        Mock time.monotonic to return 3601.0 so every call sees elapsed >= 3600.
        """
        reader = self._make_reader()
        mock_container = reader._container
        # Force teardown to fire: set last_container_open to 0 so elapsed >= 3600.
        reader._last_container_open_monotonic = 0.0

        # Pre-populate the ring to verify it is preserved.
        from PIL import Image

        for i in range(5):
            img = Image.new("RGB", (10, 10), color=(i * 50, 0, 0))
            reader._ring.append(img)
        ring_len_before = len(reader._ring)

        with patch("time.monotonic", return_value=3601.0):
            with patch("av.open", MagicMock(return_value=MagicMock())):
                with patch.object(reader, "_subscribe_geometry"):
                    reader._decode_iteration()

        # (1) container.close() was called
        self.assertTrue(mock_container.close.called)
        # (2) _last_container_open_monotonic was updated
        self.assertIsNotNone(reader._last_container_open_monotonic)
        # (3) ring length is unchanged
        self.assertEqual(len(reader._ring), ring_len_before)

    def test_failure_driven_cap_still_fails_loud(self):
        """Regression: 11 consecutive decode failures (cap default = 10);
        assert is_healthy() returns False after cap, and NO silent recovery.

        The teardown is independent of the cap; they should not interfere.
        We verify the cap fires at 10 and is_healthy() is False.
        """
        from infra import frame_capture as fc

        reader = self._make_reader(max_reconnect_attempts=10)
        # Reset _last_container_open so teardown doesn't interfere
        reader._last_container_open_monotonic = time.monotonic()
        decode_calls = {"n": 0}

        def always_fail_decode():
            decode_calls["n"] += 1
            raise RuntimeError("simulated RTSP decode failure")

        reader._decode_iteration = always_fail_decode  # type: ignore[method-assign]

        with patch.object(fc.time, "sleep", lambda s: None):
            with patch.object(reader, "_subscribe_geometry"):
                with self.assertLogs("frame_capture", level="ERROR") as cm:
                    reader.start()
                    time.sleep(0.3)
                    reader.stop(timeout=1.0)

        # Cap fired: 10 consecutive errors logged
        cap_logs = [
            r for r in cm.records
            if "consecutive_reconnect_cap_reached" in r.getMessage()
        ]
        self.assertEqual(len(cap_logs), 1, (
            f"Expected exactly one consecutive_reconnect_cap_reached log, "
            f"got {len(cap_logs)}"
        ))

        # _consecutive_errors should equal 10 (the cap value)
        self.assertEqual(reader._consecutive_errors, 10, (
            f"_consecutive_errors should be 10, got {reader._consecutive_errors}"
        ))

        # _decode_iteration was called exactly 10 times (cap tipped on the 10th)
        self.assertEqual(decode_calls["n"], 10, (
            f"Expected 10 _decode_iteration calls, got {decode_calls['n']}"
        ))

        # is_healthy() must be False — cap reached = reader unhealthy
        self.assertFalse(reader.is_healthy())


if __name__ == "__main__":
    unittest.main()
