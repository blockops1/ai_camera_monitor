"""test_capture_around_now.py -- Tests for PersistentRTSPReader.capture_around_now().

Verifies:
- AC1: Method exists, returns concurrent.futures.Future, writes PNGs.
- AC2: Calling thread is NOT blocked - method returns within 50ms.
- AC3: Worker thread is daemon=True, named "capture-{camera_id}".
- AC4: Exception in worker is captured into the Future.
"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import time
import unittest

import PIL.Image


class TestCaptureAroundNow(unittest.TestCase):
    """Tests for PersistentRTSPReader.capture_around_now()."""

    def setUp(self):
        """Import the module and create a reader with a fake ring buffer."""
        from infra.frame_capture import PersistentRTSPReader

        self.Reader = PersistentRTSPReader

        # Create a real PIL Image to serve as ring-buffer content.
        self.mock_frame = PIL.Image.new("RGB", (10, 10), color="red")

        # Build a reader with a pre-populated ring buffer (no real RTSP).
        # ring_size 200 so window-slot assertions don't depend on the
        # production default.
        self.reader = self.Reader(
            "rtsp://192.0.2.1/stream", camera_id="TEST_CAM", ring_size=200,
        )
        # Manually populate the ring buffer (bypass the decode loop).
        for _ in range(6):
            self.reader._ring.append(self.mock_frame)

    def test_AC1_method_exists_returns_future(self):
        """AC1: Method exists, returns Future, writes PNGs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            future = self.reader.capture_around_now(
                count=4, stride_s=0.05, output_dir=tmpdir,
            )
            self.assertIsInstance(future, concurrent.futures.Future)
            # Wait for the worker thread to finish.
            paths = future.result(timeout=10.0)
            self.assertEqual(len(paths), 4)
            for p in paths:
                self.assertTrue(os.path.isfile(p))

    def test_AC2_non_blocking_return(self):
        """AC2: Calling thread is NOT blocked - Future returned within 50ms."""
        with tempfile.TemporaryDirectory() as tmpdir:
            start = time.monotonic()
            future = self.reader.capture_around_now(
                count=4, stride_s=2.0,
                output_dir=tmpdir,
            )
            elapsed = time.monotonic() - start
            self.assertLess(
                elapsed, 0.05,
                f"capture_around_now blocked for {elapsed:.3f}s (should be < 50ms)"
            )
            self.assertFalse(future.done())
            # Operator spec 2026-10-30: fixed 0.5 s sleep before the final grab.
            future.result(timeout=5.0)

    def test_AC3_worker_thread_name_and_daemon(self):
        """AC3: Worker thread is daemon=True, named 'capture-{camera_id}'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Thread starts immediately on capture_around_now return.
            future = self.reader.capture_around_now(
                count=4, stride_s=2.0, output_dir=tmpdir,
            )
            # Give the thread time to start, THEN check its name.
            time.sleep(0.1)
            found = False
            for t in threading.enumerate():
                if t.name == "capture-TEST_CAM":
                    found = True
                    self.assertTrue(t.daemon, "Worker thread must be daemon=True")
                    break
            self.assertTrue(found, "Worker thread 'capture-TEST_CAM' not found")
            # Wait for the worker to finish (~0.5 s with the final delayed grab).
            future.result(timeout=5.0)

    def test_AC4_exception_captured_into_future(self):
        """AC4: Exception in worker is captured into the Future."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Patch _capture_worker to raise.
            original = self.reader._capture_worker

            def failing_worker(count, stride_s, output_dir):
                raise RuntimeError("simulated failure")

            self.reader._capture_worker = failing_worker

            try:
                future = self.reader.capture_around_now(
                    count=4, stride_s=0.0, output_dir=tmpdir,
                )
                with self.assertRaises(RuntimeError) as ctx:
                    future.result(timeout=5.0)
                self.assertEqual(str(ctx.exception), "simulated failure")
            finally:
                self.reader._capture_worker = original

    def test_no_get_recent_frames_fallback(self):
        """AC5: get_recent_frames is removed — no fallback path exists."""
        self.assertTrue(hasattr(self.reader, "capture_around_now"))
        self.assertFalse(hasattr(self.reader, "get_recent_frames"))

    def test_frame_names_ordered(self):
        """Helper test: frames are named frame_001.png through frame_NNN.png."""
        with tempfile.TemporaryDirectory() as tmpdir:
            future = self.reader.capture_around_now(
                count=4, stride_s=0.05, output_dir=tmpdir,
            )
            paths = future.result(timeout=10.0)
            expected_names = [
                "frame_001.png", "frame_002.png",
                "frame_003.png", "frame_004.png",
            ]
            actual_names = [os.path.basename(p) for p in paths]
            self.assertEqual(actual_names, expected_names)

    def test_empty_ring_no_crash(self):
        """Edge case: ring buffer empty - method handles gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.reader._ring.clear()
            future = self.reader.capture_around_now(
                count=4, stride_s=0.05, output_dir=tmpdir,
            )
            paths = future.result(timeout=10.0)
            # All frames skipped, returns empty list (no exception).
            self.assertEqual(paths, [])

    def test_capture_window_spec_operator_slots(self):
        """Operator spec 2026-10-30: grab ring slots [-5, -4, -2, -1].

        Frame 4 is the newest available after a 0.5 s delay.  The ring is
        static in tests (no decoder), so frames 3 and 4 resolve to ring[-1].
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            self.reader._ring.clear()
            frames = [
                PIL.Image.new("RGB", (10, 10), color=(i % 256, (255 - i) % 256, 0))
                for i in range(100)
            ]
            for f in frames:
                self.reader._ring.append(f)

            future = self.reader.capture_around_now(
                count=4, stride_s=2.0, output_dir=tmpdir,
            )
            paths = future.result(timeout=10.0)
            self.assertEqual(len(paths), 4)
            # ring[-5], ring[-4], ring[-2], ring[-1]
            for idx, slot in enumerate((95, 96, 98, 99)):
                self.assertEqual(
                    PIL.Image.open(paths[idx]).getpixel((0, 0)),
                    frames[slot].getpixel((0, 0)),
                    f"frame_{idx + 1} should be ring[-{100 - slot}]",
                )

    def test_capture_clamps_to_oldest_on_short_ring(self):
        """Ring shorter than slot depth: clamp to oldest frame, never drop event.

        A freshly (re)subscribed ring is short but near-live; dropping the
        whole capture loses the alert entirely.  Clamping loses at most the
        earliest context.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            self.reader._ring.clear()
            f1 = PIL.Image.new("RGB", (10, 10), color=(0, 128, 0))
            f2 = PIL.Image.new("RGB", (10, 10), color=(0, 0, 128))
            self.reader._ring.append(f1)
            self.reader._ring.append(f2)

            future = self.reader.capture_around_now(
                count=4, stride_s=0.05, output_dir=tmpdir,
            )
            paths = future.result(timeout=10.0)
            self.assertEqual(len(paths), 4)
            # slots -5/-4/-2 clamp to ring[-2] = f1; -1 = ring[-1] = f2
            self.assertEqual(PIL.Image.open(paths[0]).getpixel((0, 0)), (0, 128, 0))
            self.assertEqual(PIL.Image.open(paths[1]).getpixel((0, 0)), (0, 128, 0))
            self.assertEqual(PIL.Image.open(paths[2]).getpixel((0, 0)), (0, 128, 0))
            self.assertEqual(PIL.Image.open(paths[3]).getpixel((0, 0)), (0, 0, 128))

    def test_capture_save_no_longer_uses_optimize_true(self):
        """AC5: _save_one no longer passes optimize=True."""
        # Verify the source code of _save_one doesn't contain optimize=True
        import inspect
        source = inspect.getsource(self.reader._save_one)
        self.assertNotIn("optimize=True", source)
        self.assertIn('format="PNG"', source)


if __name__ == "__main__":
    unittest.main()
