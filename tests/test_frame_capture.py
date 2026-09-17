"""
test_frame_capture.py — Tests for infra/frame_capture (PersistentRTSPReader).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import time as _time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from infra.frame_capture import RING_SIZE_DEFAULT, PersistentRTSPReader

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_av_frame():
    """Return a MagicMock mimicking an av.Frame with to_image()."""
    pil_img = Image.new("RGB", (640, 480), color="red")
    frame = MagicMock()
    frame.dts = 0
    frame.to_image.return_value = pil_img
    return frame


@pytest.fixture()
def mock_av_stream():
    """Return a MagicMock mimicking an av.VideoStream."""
    stream = MagicMock()
    stream.time_base = 1 / 30
    stream.average_rate = 30
    stream.codec_context.width = 640
    stream.codec_context.height = 480
    return stream


@pytest.fixture()
def mock_av_container(mock_av_stream, mock_av_frame):
    """Return a MagicMock mimicking an av.container.InputContainer."""
    container = MagicMock()
    container.streams.video = [mock_av_stream]

    def demux_gen(stream_arg):
        pkt = MagicMock()
        pkt.dts = 0
        pkt.decode.return_value = [mock_av_frame, mock_av_frame, mock_av_frame]
        yield pkt

    container.demux = demux_gen
    return container


# ---------------------------------------------------------------------------
# Test: constructor / properties
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_ac1_import_and_name(self):
        """Module imports and class exists with all public methods."""
        assert hasattr(PersistentRTSPReader, "start")
        assert hasattr(PersistentRTSPReader, "stop")
        assert hasattr(PersistentRTSPReader, "get_recent_frames")
        assert hasattr(PersistentRTSPReader, "is_healthy")
        assert hasattr(PersistentRTSPReader, "uptime_seconds")

    def test_default_ring_size(self):
        """Default ring size is RING_SIZE_DEFAULT."""
        reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
        assert reader._ring_size == RING_SIZE_DEFAULT

    def test_custom_ring_size(self):
        """Custom ring size is respected."""
        reader = PersistentRTSPReader(
            "rtsp://user:pass@10.0.0.1:554/h264", ring_size=10
        )
        assert reader._ring_size == 10

    def test_initial_state(self):
        """Reader starts stopped, not healthy, zero uptime."""
        reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
        assert reader.is_running is False
        assert reader.is_healthy() is False
        assert reader.uptime_seconds() == 0.0
        assert reader.frames_decoded_total == 0

    def test_initial_ring_is_deque_with_maxlen(self):
        """Ring buffer is a deque with the configured maxlen."""
        reader = PersistentRTSPReader("rtsp://u:p@h:554/h", ring_size=42)
        assert isinstance(reader._ring, __import__("collections").deque)
        assert reader._ring.maxlen == 42

    def test_default_ffmpeg_flags(self):
        """Default ffmpeg_flags match expected transport settings."""
        reader = PersistentRTSPReader("rtsp://u:p@h:554/h")
        assert reader._ffmpeg_flags["rtsp_transport"] == "tcp"
        assert reader._ffmpeg_flags["timeout"] == "10000000"

    def test_custom_ffmpeg_flags(self):
        """Custom ffmpeg_flags override defaults."""
        reader = PersistentRTSPReader(
            "rtsp://u:p@h:554/h",
            ffmpeg_flags={"rtsp_transport": "udp", "timeout": "5000000"},
        )
        assert reader._ffmpeg_flags["rtsp_transport"] == "udp"
        assert reader._ffmpeg_flags["timeout"] == "5000000"


# ---------------------------------------------------------------------------
# Test: start / stop
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_starts_thread_and_decodes(
        self, mock_av_container, mock_av_stream, mock_av_frame
    ):
        """start() launches the background thread, which decodes frames."""
        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            pkt = MagicMock()
            pkt.dts = 0
            pkt.decode.return_value = [mock_av_frame, mock_av_frame, mock_av_frame]
            yield pkt

        container.demux = demux_gen

        with patch("av.open", return_value=container):
            reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
            reader.start()
            # Wait for thread to finish (unpatched sleep gives it 0.1s)
            time.sleep(0.1)
            assert reader.frames_decoded_total == 3
            reader.stop(timeout=2.0)

    def test_stop_cleans_up(self, mock_av_stream, mock_av_frame):
        """stop() halts the thread and sets _healthy to False."""
        call_count = [0]

        def live_yield(stream_arg):
            while True:
                pkt = MagicMock()
                pkt.dts = call_count[0]
                call_count[0] += 1
                pkt.decode.return_value = [mock_av_frame]
                yield pkt

        container_live = MagicMock()
        container_live.streams.video = [mock_av_stream]
        container_live.demux = live_yield

        with (
            patch("av.open", return_value=container_live),
        ):
            reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
            reader.start()
            # Real sleep — thread processes infinite frames, _healthy=True
            time.sleep(0.05)
            assert reader.is_healthy(stale_seconds=5.0) is True
            reader.stop(timeout=2.0)
            assert reader.is_healthy() is False

    def test_stop_while_not_running_is_safe(self):
        """Calling stop() on a never-started reader does not crash."""
        reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
        reader.stop(timeout=1.0)  # should not raise

    def test_double_start_only_one_thread(self, mock_av_stream, mock_av_frame):
        """Calling start() twice does not launch a second thread."""
        thread_ids: list[int] = []
        original_start = threading.Thread.start

        def track_start(self):
            thread_ids.append(id(self))
            return original_start(self)

        call_count = [0]

        def live_yield(stream_arg):
            while True:
                pkt = MagicMock()
                pkt.dts = call_count[0]
                call_count[0] += 1
                pkt.decode.return_value = [mock_av_frame]
                yield pkt

        container_live = MagicMock()
        container_live.streams.video = [mock_av_stream]
        container_live.demux = live_yield

        with (
            patch("av.open", return_value=container_live),
            patch.object(threading.Thread, "start", track_start),
        ):
            reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
            reader.start()
            time.sleep(0.1)
            reader.start()
            time.sleep(0.1)
            # US-049b: watchdog deleted — start() spawns only decode thread.
            # Second start() is a no-op (is_running), so we see exactly 1
            # thread start, not 2.
            assert len(thread_ids) == 1, "Only decode thread should launch"
            reader.stop(timeout=2.0)

    def test_get_frames_after_start(self, mock_av_stream, mock_av_frame):
        """After start + sleep, get_recent_frames returns frame paths."""
        output_dir = "/tmp/test_frame_after_start"

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            pkt = MagicMock()
            pkt.dts = 0
            pkt.decode.return_value = [mock_av_frame, mock_av_frame, mock_av_frame]
            yield pkt

        container.demux = demux_gen

        with patch("av.open", return_value=container):
            reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
            reader.start()
            # Real sleep — thread decodes 3 frames (~50ms)
            time.sleep(0.15)
            frames = reader.get_recent_frames(3, output_dir)
            assert len(frames) == 3
            for fp in frames:
                assert os.path.isfile(fp)
            reader.stop(timeout=2.0)
            for old in __import__("pathlib").Path(output_dir).glob("frame_*.png"):
                old.unlink()


# ---------------------------------------------------------------------------
# Test: is_healthy / uptime_seconds
# ---------------------------------------------------------------------------


class TestHealthAndUptime:
    def test_uptime_increases(self, mock_av_stream, mock_av_frame):
        """uptime_seconds() increases while reader runs."""
        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            pkt = MagicMock()
            pkt.dts = 0
            pkt.decode.return_value = [mock_av_frame]
            yield pkt

        container.demux = demux_gen

        with patch("av.open", return_value=container):
            reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
            reader.start()
            t0 = reader.uptime_seconds()
            time.sleep(0.05)
            t1 = reader.uptime_seconds()
            assert t1 > t0, "uptime should be monotonic"
            reader.stop(timeout=2.0)

    def test_healthy_true_during_decode(self, mock_av_stream, mock_av_frame):
        """is_healthy() returns True while the decode loop is still running."""
        call_count = [0]

        def infinite_yield(stream_arg):
            while True:
                pkt = MagicMock()
                pkt.dts = call_count[0]
                call_count[0] += 1
                pkt.decode.return_value = [mock_av_frame]
                yield pkt

        container_live = MagicMock()
        container_live.streams.video = [mock_av_stream]
        container_live.demux = infinite_yield

        with (
            patch("av.open", return_value=container_live),
        ):
            reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
            reader.start()
            time.sleep(0.05)
            assert reader.is_healthy(stale_seconds=5.0) is True
            reader.stop(timeout=2.0)

    def test_is_healthy_false_before_start(self):
        """is_healthy() returns False when reader has not started."""
        reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
        assert reader.is_healthy() is False


# ---------------------------------------------------------------------------
# Test: get_recent_frames
# ---------------------------------------------------------------------------


class TestGetRecentFrames:
    def test_returns_empty_when_no_frames(self):
        """get_recent_frames returns [] when the ring buffer is empty."""
        reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
        frames = reader.get_recent_frames(6, "/tmp/test_frames_empty")
        assert frames == []

    def test_writes_png_files(self, mock_av_stream, mock_av_frame):
        """get_recent_frames() writes PNGs and returns paths."""
        output_dir = "/tmp/test_png_write"

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            pkt = MagicMock()
            pkt.dts = 0
            pkt.decode.return_value = [mock_av_frame, mock_av_frame, mock_av_frame]
            yield pkt

        container.demux = demux_gen

        with patch("av.open", return_value=container):
            reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
            reader.start()
            time.sleep(0.15)
            frames = reader.get_recent_frames(3, output_dir)
            assert len(frames) == 3
            for fp in frames:
                assert os.path.isfile(fp)
                assert fp.endswith(".png")
            reader.stop(timeout=2.0)
            for old in __import__("pathlib").Path(output_dir).glob("frame_*.png"):
                old.unlink()

    def test_frame_order_oldest_first(self, mock_av_stream, mock_av_frame):
        """Frames are returned in chronological order (oldest first)."""
        output_dir = "/tmp/test_frame_order"

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            pkt = MagicMock()
            pkt.dts = 0
            pkt.decode.return_value = [mock_av_frame, mock_av_frame, mock_av_frame]
            yield pkt

        container.demux = demux_gen

        with patch("av.open", return_value=container):
            reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264")
            reader.start()
            time.sleep(0.15)
            frames = reader.get_recent_frames(10, output_dir)
            for i, fp in enumerate(frames, start=1):
                assert fp == os.path.join(output_dir, f"frame_{i:03d}.png")
            reader.stop(timeout=2.0)
            for old in __import__("pathlib").Path(output_dir).glob("frame_*.png"):
                old.unlink()

    def test_ring_bounded_to_maxlen(self, mock_av_stream, mock_av_frame):
        """Ring buffer is bounded to ring_size even when many frames are decoded."""

        def many_frames_yield(stream_arg):
            for i in range(20):
                pkt = MagicMock()
                pkt.dts = i
                pkt.decode.return_value = [mock_av_frame] * 3
                yield pkt

        container = MagicMock()
        container.streams.video = [mock_av_stream]
        container.demux = many_frames_yield

        reader = PersistentRTSPReader("rtsp://user:pass@10.0.0.1:554/h264", ring_size=5)
        with patch("av.open", return_value=container):
            reader.start()
            time.sleep(0.1)
            assert reader.frames_decoded_total > 0, (
                f"Expected frames decoded, got {reader.frames_decoded_total}"
            )
            assert reader._ring.maxlen == 5
            assert len(reader._ring) == 5, (
                f"Ring should be capped at maxlen, got {len(reader._ring)}"
            )
            reader.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Test: module-level get_recent_frames
# ---------------------------------------------------------------------------


class TestModuleGetRecentFrames:
    def test_returns_empty_when_no_frames_in_ring(self):
        """Module-level get_recent_frames returns [] for empty ring."""
        from infra.frame_capture import get_recent_frames

        frames = get_recent_frames("front_gate", n=4, offset_seconds=6)
        assert frames == []

    def test_respects_n_limit(self, mock_av_stream, mock_av_frame, tmp_path):
        """get_recent_frames returns at most n frames."""
        output_dir = str(tmp_path / "frames")
        from infra.frame_capture import CameraCaptureRegistry, get_recent_frames

        CameraCaptureRegistry.clear()

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        for i in range(8):
            img_path = os.path.join(output_dir, f"frame_{i + 1:03d}.png")
            Image.new("RGB", (640, 480), color="red").save(img_path, format="PNG", optimize=True)

        # Mock the reader to return all 8 frames
        mock_reader = MagicMock()
        mock_reader.is_healthy.return_value = True

        def mock_get_recent(n, *args, **kwargs):
            # Return at most n frames
            all_frames = [
                os.path.join(output_dir, f"frame_{i + 1:03d}.png") for i in range(8)
            ]
            return all_frames[:n]

        mock_reader.get_recent_frames.side_effect = mock_get_recent

        with patch("infra.frame_capture._get_healthy_reader", return_value=mock_reader):
            frames = get_recent_frames("test_cam", n=3, offset_seconds=60)
            assert len(frames) <= 3

    def test_ages_out_frames_beyond_offset(
        self, mock_av_stream, mock_av_frame, tmp_path
    ):
        """get_recent_frames filters frames older than offset_seconds."""
        output_dir = str(tmp_path / "frames")
        from infra.frame_capture import CameraCaptureRegistry, get_recent_frames

        CameraCaptureRegistry.clear()

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        # Create frames directly in the output dir
        for i in range(2):
            img_path = os.path.join(output_dir, f"frame_{i + 1:03d}.png")
            Image.new("RGB", (640, 480), color="red").save(img_path, format="PNG", optimize=True)

        # Make frame_001 old (>6s)
        old_time = time.time() - 10
        os.utime(os.path.join(output_dir, "frame_001.png"), (old_time, old_time))

        # Mock the reader path — the module-level func calls _get_healthy_reader
        # which checks CameraCaptureRegistry. We mock it to return a reader
        # whose get_recent_frames just returns our pre-made frames.
        mock_reader = MagicMock()
        mock_reader.is_healthy.return_value = True
        mock_reader.get_recent_frames.return_value = [
            os.path.join(output_dir, "frame_001.png"),
            os.path.join(output_dir, "frame_002.png"),
        ]

        with patch("infra.frame_capture._get_healthy_reader", return_value=mock_reader):
            result = get_recent_frames("test_cam", n=2, offset_seconds=6)
            # frame_001 is >6s old, so only frame_002 should pass
            assert len(result) == 1
            assert os.path.basename(result[0]) == "frame_002.png"

    def test_multi_camera_isolation(self, mock_av_stream, mock_av_frame, tmp_path):
        """Two cameras' frames go to different directories."""
        output_dir_a = str(tmp_path / "frames_a")
        output_dir_b = str(tmp_path / "frames_b")
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            pkt = MagicMock()
            pkt.dts = 0
            pkt.decode.return_value = [mock_av_frame]
            yield pkt

        container.demux = demux_gen

        with patch("av.open", return_value=container):
            reader_a = PersistentRTSPReader("rtsp://u:p@h1:554/h")
            reader_a.start()
            time.sleep(0.15)
            reader_a.get_recent_frames(1, output_dir_a)

            reader_b = PersistentRTSPReader("rtsp://u:p@h2:554/h")
            reader_b.start()
            time.sleep(0.15)
            reader_b.get_recent_frames(1, output_dir_b)

            assert len(list((tmp_path / "frames_a").glob("frame_*.png"))) == 1
            assert len(list((tmp_path / "frames_b").glob("frame_*.png"))) == 1
            reader_a.stop(timeout=2.0)
            reader_b.stop(timeout=2.0)
            CameraCaptureRegistry.clear()


# ---------------------------------------------------------------------------
# Test: module-level get_frames_by_offset
# ---------------------------------------------------------------------------


class TestModuleGetFramesByOffset:
    def test_returns_empty_when_no_frames(self):
        """Module-level get_frames_by_offset returns [] for empty ring."""
        from infra.frame_capture import get_frames_by_offset

        result = get_frames_by_offset("front_gate", [0, 1, 2])
        assert result == []

    def test_returns_frames_at_requested_indices(
        self, mock_av_stream, mock_av_frame, tmp_path
    ):
        """get_frames_by_offset returns frames at specified indices."""
        output_dir = str(tmp_path / "frames")
        from infra.frame_capture import CameraCaptureRegistry, get_frames_by_offset

        CameraCaptureRegistry.clear()

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        # Pre-create frames in the output dir
        for i in range(3):
            img_path = os.path.join(output_dir, f"frame_{i + 1:03d}.png")
            Image.new("RGB", (640, 480), color="red").save(img_path, format="PNG", optimize=True)

        mock_reader = MagicMock()
        mock_reader.is_healthy.return_value = True
        mock_reader.get_frames_by_offset.return_value = [
            os.path.join(output_dir, "frame_001.png"),
            os.path.join(output_dir, "frame_003.png"),
        ]

        with patch("infra.frame_capture._get_healthy_reader", return_value=mock_reader):
            result = get_frames_by_offset("test_cam", [0, 2])
            assert len(result) == 2
            assert os.path.basename(result[0]) == "frame_001.png"
            assert os.path.basename(result[1]) == "frame_003.png"

    def test_skips_out_of_bounds_indices(self, mock_av_stream, mock_av_frame, tmp_path):
        """get_frames_by_offset skips indices beyond ring length."""
        output_dir = str(tmp_path / "frames")
        from infra.frame_capture import CameraCaptureRegistry, get_frames_by_offset

        CameraCaptureRegistry.clear()

        mock_reader = MagicMock()
        mock_reader.is_healthy.return_value = True
        mock_reader.get_frames_by_offset.return_value = [
            os.path.join(output_dir, "frame_001.png"),
        ]

        with patch("infra.frame_capture._get_healthy_reader", return_value=mock_reader):
            result = get_frames_by_offset("test_cam", [0, 99, -5])
            # 99 is out of bounds, -5 is out of bounds; only 0 is valid
            assert len(result) == 1
            assert os.path.basename(result[0]) == "frame_001.png"

    def test_multi_camera_offset_isolation(
        self, mock_av_stream, mock_av_frame, tmp_path
    ):
        """Frame indices are per-camera, not global."""
        output_dir_a = str(tmp_path / "frames_a")
        output_dir_b = str(tmp_path / "frames_b")
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            pkt = MagicMock()
            pkt.dts = 0
            pkt.decode.return_value = [mock_av_frame] * 4
            yield pkt

        container.demux = demux_gen

        with patch("av.open", return_value=container):
            reader_a = PersistentRTSPReader("rtsp://u:p@h1:554/h")
            reader_a.start()
            time.sleep(0.15)
            reader_a.get_frames_by_offset([0, 3], output_dir_a)

            reader_b = PersistentRTSPReader("rtsp://u:p@h2:554/h")
            reader_b.start()
            time.sleep(0.15)
            reader_b.get_frames_by_offset([0, 3], output_dir_b)

            assert len(list((tmp_path / "frames_a").glob("frame_*.png"))) == 2
            assert len(list((tmp_path / "frames_b").glob("frame_*.png"))) == 2
            reader_a.stop(timeout=2.0)
            reader_b.stop(timeout=2.0)
            CameraCaptureRegistry.clear()


# ---------------------------------------------------------------------------
# Test: CameraCaptureRegistry
# ---------------------------------------------------------------------------


class TestCameraCaptureRegistry:
    def test_singleton(self):
        """Two get() calls return the same instance."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()
        inst1 = CameraCaptureRegistry()
        inst2 = CameraCaptureRegistry()
        assert inst1 is inst2
        CameraCaptureRegistry.clear()

    def test_clear_stops_readers(self):
        """clear() stops all readers and resets singleton."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()
        # After clear, instance should be None
        assert CameraCaptureRegistry._instance is None

    def test_get_raises_keyerror_for_unknown_camera(self):
        """get() raises KeyError when camera is not in camera_creds."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()
        with pytest.raises(KeyError, match="CameraCaptureRegistry not initialized"):
            CameraCaptureRegistry.get("nonexistent_cam")

    def test_clear_then_reinstantiate(self):
        """After clear, a new CameraCaptureRegistry() creates a fresh instance."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()
        assert CameraCaptureRegistry._instance is None
        # Instantiating creates the singleton
        CameraCaptureRegistry()
        assert CameraCaptureRegistry._instance is not None
        CameraCaptureRegistry.clear()
        assert CameraCaptureRegistry._instance is None

    def test_get_uses_camera_rtsp_url(self, mock_av_stream, mock_av_frame):
        """get() reads rtsp_url from camera_creds and starts a reader."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            pkt = MagicMock()
            pkt.dts = 0
            pkt.decode.return_value = [mock_av_frame]
            yield pkt

        container.demux = demux_gen

        with (
            patch("av.open", return_value=container),
            patch(
                "infra.camera_creds.get_camera",
                return_value={"rtsp_url": "rtsp://u:p@h:554/h"},
            ),
        ):
            # Create the singleton first (get() doesn't create it)
            CameraCaptureRegistry()
            reader = CameraCaptureRegistry.get("test_cam")
            assert reader is not None
            assert reader._rtsp_url == "rtsp://u:p@h:554/h"
            reader.stop(timeout=2.0)
            CameraCaptureRegistry.clear()

    def test_clear_is_idempotent(self):
        """Calling clear() twice does not crash."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()
        CameraCaptureRegistry.clear()  # should not raise

    def test_start_all_boots_concurrent_threads(self, mock_av_stream, mock_av_frame):
        """start_all() launches one reader thread per camera concurrently."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            while True:
                pkt = MagicMock()
                pkt.dts = 0
                pkt.decode.return_value = [mock_av_frame]
                yield pkt

        container.demux = demux_gen

        with (
            patch("av.open", return_value=container),
            patch(
                "infra.camera_creds.get_all_cameras",
                return_value={
                    "cam_a": {"rtsp_url": "rtsp://a:554/h"},
                    "cam_b": {"rtsp_url": "rtsp://b:554/h"},
                },
            ),
        ):
            CameraCaptureRegistry.start_all()
            time.sleep(0.1)
            inst = CameraCaptureRegistry._instance
            assert inst is not None
            assert "cam_a" in inst._readers
            assert "cam_b" in inst._readers
            assert inst._readers["cam_a"].is_running
            assert inst._readers["cam_b"].is_running
            CameraCaptureRegistry.stop_all()
            CameraCaptureRegistry.clear()

    def test_start_all_with_empty_cameras(self):
        """start_all() is a no-op when no cameras are configured."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()
        with patch("infra.camera_creds.get_all_cameras", return_value={}):
            CameraCaptureRegistry.start_all()
            assert CameraCaptureRegistry._instance is None
        CameraCaptureRegistry.clear()

    def test_stop_all_drains_threads(self, mock_av_stream, mock_av_frame):
        """stop_all() stops all readers and the reconnect thread."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            while True:
                pkt = MagicMock()
                pkt.dts = 0
                pkt.decode.return_value = [mock_av_frame]
                yield pkt

        container.demux = demux_gen

        reader_was_running = [False]

        with (
            patch("av.open", return_value=container),
            patch(
                "infra.camera_creds.get_all_cameras",
                return_value={
                    "cam_x": {"rtsp_url": "rtsp://x:554/h"},
                },
            ),
        ):
            CameraCaptureRegistry.start_all()
            time.sleep(0.1)
            inst = CameraCaptureRegistry._instance
            assert inst is not None
            assert inst._reconnect_thread is not None
            reader_was_running[0] = inst._readers["cam_x"].is_running
            CameraCaptureRegistry.stop_all()
            assert inst._reconnect_thread is None
            assert reader_was_running[0] is True
        CameraCaptureRegistry.clear()

    def test_is_healthy_all_returns_dict(self, mock_av_stream, mock_av_frame):
        """is_healthy_all() returns a dict with camera_id keys."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()

        container = MagicMock()
        container.streams.video = [mock_av_stream]

        def demux_gen(stream_arg):
            while True:
                pkt = MagicMock()
                pkt.dts = 0
                pkt.decode.return_value = [mock_av_frame]
                yield pkt

        container.demux = demux_gen

        with (
            patch("av.open", return_value=container),
            patch(
                "infra.camera_creds.get_all_cameras",
                return_value={"cam_h": {"rtsp_url": "rtsp://h:554/h"}},
            ),
        ):
            CameraCaptureRegistry.start_all()
            time.sleep(0.1)
            health = CameraCaptureRegistry.is_healthy_all()
            assert "cam_h" in health
            assert isinstance(health["cam_h"], bool)
            CameraCaptureRegistry.stop_all()
        CameraCaptureRegistry.clear()

    def test_is_healthy_all_empty_when_no_readers(self):
        """is_healthy_all() returns {} when registry is empty."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()
        result = CameraCaptureRegistry.is_healthy_all()
        assert result == {}
        CameraCaptureRegistry.clear()

    def test_reconnect_detects_dead_reader(self, mock_av_stream, mock_av_frame):
        """_reconnect_loop restarts a reader that has been unhealthy 10s+."""
        from infra.frame_capture import CameraCaptureRegistry

        first_container_ready = threading.Event()
        container_1_died = threading.Event()

        def make_container_1(stream_arg):
            """First container: yields until signaled, then exits."""
            while not container_1_died.is_set():
                pkt = MagicMock()
                pkt.dts = 0
                pkt.decode.return_value = [mock_av_frame]
                yield pkt

        def make_container_alive(stream_arg):
            """Second container (reconnect): yields forever."""
            while True:
                pkt = MagicMock()
                pkt.dts = 0
                pkt.decode.return_value = [mock_av_frame]
                yield pkt

        container_1 = MagicMock()
        container_1.streams.video = [mock_av_stream]
        container_1.demux = make_container_1

        container_alive = MagicMock()
        container_alive.streams.video = [mock_av_stream]
        container_alive.demux = make_container_alive

        call_count = [0]

        def open_factory(url, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                first_container_ready.set()
                return container_1
            return container_alive

        with (
            patch("av.open", side_effect=open_factory),
            patch(
                "infra.camera_creds.get_all_cameras",
                return_value={"cam_r": {"rtsp_url": "rtsp://r:554/h"}},
            ),
        ):
            CameraCaptureRegistry.start_all()
            first_container_ready.wait(timeout=5)
            time.sleep(0.2)
            inst = CameraCaptureRegistry._instance
            assert inst is not None
            assert inst._readers["cam_r"].is_running

            # Signal container_1 to stop — reader becomes unhealthy
            container_1_died.set()

            # Patch uptime_seconds to fake >10s so reconnect triggers immediately
            orig_uptime = PersistentRTSPReader.uptime_seconds
            PersistentRTSPReader.uptime_seconds = lambda self: 11.0

            # Wait for reconnect loop to detect and restart
            time.sleep(7)

            # Reconnect should have replaced the dead reader
            assert inst._readers["cam_r"].is_running

            PersistentRTSPReader.uptime_seconds = orig_uptime
            CameraCaptureRegistry.stop_all()
        CameraCaptureRegistry.clear()

    def test_reconnect_skips_fresh_readers(self):
        """is_healthy_all() returns {} when registry has no readers."""
        from infra.frame_capture import CameraCaptureRegistry

        CameraCaptureRegistry.clear()
        inst = CameraCaptureRegistry()
        health = CameraCaptureRegistry.is_healthy_all()
        assert health == {}
        CameraCaptureRegistry.clear()


# ---------------------------------------------------------------------------
# Test: failure-driven reconnect cap (US-017e, migrated from test_rtsp_watchdog)
# ---------------------------------------------------------------------------


class TestFailureDrivenReconnectCap:
    def test_cap_reached_failure_driven_logs_error_no_watchdog(self, monkeypatch, caplog):
        """REGRESSION: RECONNECT_MAX_ATTEMPTS_DEFAULT = 10 consecutive decode
        failures still log ERROR and exit the decode loop. No 'proceed-anyway'
        branch. No watchdog to mask this path.

        The cap path in _run_loop (lines 352-389):
          - On Nth consecutive failure (N == max_reconnect_attempts),
            logs ERROR 'consecutive_reconnect_cap_reached'.
          - Enters _stop_event.wait(scheduled_reconnect_seconds).
          - If stop_event is set during wait, breaks the outer while-loop.
          - If stop_event is NOT set, the outer while-loop retries
            _decode_iteration — which either recovers or re-hits the cap.

        This test verifies:
          1. ERROR log line with 'consecutive_reconnect_cap_reached' emitted.
          2. After cap exhaustion, no further _decode_iteration calls
             fire before stop() — the loop is halted, not hammering.
          3. is_healthy() == False after cap exhaustion.
          4. NO log line containing 'proceed' (the old watchdog
             'proceed anyway' path is gone).
        """
        from infra import frame_capture as fc

        decode_calls = {"n": 0}

        def always_fail_decode() -> None:
            decode_calls["n"] += 1
            raise RuntimeError("simulated RTSP decode failure")

        # The current code uses self._stop_event.wait() directly in the
        # cap block (not _sleep_until_stop_or_watchdog). We monkeypatch
        # wait() so it returns True on the first call, simulating the
        # stop_event being set (which happens when stop() is called).
        def fake_wait(self, timeout=None):
            # Actually set the event so is_set() returns True after this
            # call, matching real stop() behavior.  This makes the cap
            # block break cleanly (the while-loop sees the event set).
            self.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", fake_wait)
        # Backoff sleeps — make them no-ops to speed up the test.
        monkeypatch.setattr(fc.time, "sleep", lambda _s: None)

        reader = PersistentRTSPReader(
            "rtsp://u:p@h:554/h",
            max_reconnect_attempts=3,
        )
        reader._decode_iteration = always_fail_decode  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger="frame_capture"):
            reader.start()
            # Wait for the decode thread to run:
            # 3 failures -> cap fires -> wait() returns True -> break
            deadline = _time.monotonic() + 2.0
            while _time.monotonic() < deadline and decode_calls["n"] < 3:
                _time.sleep(0.01)
            _time.sleep(0.1)
            reader.stop(timeout=2.0)

        # AC — ERROR log with consecutive_reconnect_cap_reached.
        cap_logs = [
            r for r in caplog.records
            if "consecutive_reconnect_cap_reached" in r.getMessage()
        ]
        assert len(cap_logs) == 1, (
            f"Expected exactly one consecutive_reconnect_cap_reached log, "
            f"got {len(cap_logs)}: {[r.getMessage() for r in cap_logs]}"
        )

        # AC — is_healthy() is False after cap exhaustion.
        assert reader.is_healthy() is False

        # AC — exactly 3 decode calls (cap tipped on 3rd failure).
        assert decode_calls["n"] == 3, (
            f"Expected 3 _decode_iteration calls (cap tipped on 3rd "
            f"failure when max_reconnect_attempts=3), got {decode_calls['n']}"
        )

        # AC — no 'proceed' log line (old watchdog proceed-anyway path is gone).
        all_msgs = [r.getMessage() for r in caplog.records]
        assert not any("proceed" in m.lower() for m in all_msgs), (
            f"Found 'proceed' in log messages — old watchdog path should be gone: {all_msgs}"
        )


# ---------------------------------------------------------------------------
# Test: in-thread periodic teardown cadence (US-049c / US-049f)
# ---------------------------------------------------------------------------


class TestPeriodicTeardown:
    """Tests for the in-thread periodic teardown mechanism (US-049c)."""

    def test_teardown_fires_after_3600s(
        self, mock_av_stream, mock_av_frame, monkeypatch
    ):
        """AC: teardown fires when monotonic time advances past
        TEARDOWN_INTERVAL_SECONDS (3600). Assert av.open() called >= 2x
        (once at start, once at teardown).

        The teardown check is at the START of _decode_iteration. To trigger
        it, the first demux must raise (simulating RTSP disconnect) so
        _run_loop retries _decode_iteration — on the retry the time is past
        3600 and teardown fires.

        NOTE: fc.time IS the stdlib time module, so monkeypatching fc.time.sleep
        also patches stdlib time.sleep. We save a reference to the real sleep
        BEFORE the monkeypatch.
        """
        from infra import frame_capture as fc

        # Capture real time.sleep before monkeypatch (fc.time is stdlib time)
        _real_sleep = fc.time.sleep

        teardown_interval = fc.TEARDOWN_INTERVAL_SECONDS  # 3600.0
        call_log = {"open": 0}

        fake_time = [0.0]

        def fake_monotonic():
            return fake_time[0]

        open_count = [0]

        def fake_av_open(*args, **kwargs):
            call_log["open"] += 1
            open_count[0] += 1
            container = MagicMock()
            container.streams.video = [mock_av_stream]

            def demux_gen(stream_arg):
                if open_count[0] == 1:
                    # First container: yield one frame then raise
                    # to trigger _run_loop retry (which fires teardown).
                    pkt = MagicMock()
                    pkt.dts = 0
                    pkt.decode.return_value = [mock_av_frame]
                    yield pkt
                    raise OSError("simulated RTSP disconnect")
                # Post-teardown container: yield and raise (so stop works).
                pkt = MagicMock()
                pkt.dts = 0
                pkt.decode.return_value = [mock_av_frame]
                yield pkt
                raise OSError("simulated disconnect after teardown")

            container.demux = demux_gen
            return container

        monkeypatch.setattr(fc.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(fc.time, "sleep", lambda _s: None)

        with patch("av.open", side_effect=fake_av_open):
            reader = PersistentRTSPReader(
                "rtsp://u:p@h:554/h",
                max_reconnect_attempts=0,  # cap disabled, keep looping
            )
            reader.start()

            # Yield to background thread using REAL sleep (saved before patch)
            _real_sleep(0.05)

            # Advance past teardown interval
            fake_time[0] = teardown_interval + 1.0

            # Yield to let teardown fire
            _real_sleep(0.3)

            # Stop reader
            reader.stop(timeout=2.0)

        # AC: av.open called at least twice (initial connect + teardown)
        assert call_log["open"] >= 2, (
            f"av.open() called {call_log['open']}x, expected >= 2"
        )

    def test_ring_buffer_survives_teardown(
        self, mock_av_stream, mock_av_frame, monkeypatch
    ):
        """AC: frames appended before teardown survive teardown; post-teardown
        frames are also appended — ring buffer (deque) is never cleared by
        teardown. Total frames >= pre + post (bounded by ring_size).

        The demux yields 3 frames then raises OSError. _run_loop catches and
        retries (with capped backoff sleep). On retry, monotonic time is
        past 3600 so teardown fires (closes + reopens container). The new
        demux yields 3 more frames and raises. _run_loop catches, retries,
        and on this retry _container is already set (from teardown), so
        av.open() is NOT called again — the exhausted demux of the teardown
        container is used. Each demux() call creates a new generator, so
        frames continue to be appended. The ring grows past the initial
        count and is bounded by ring_size=10. After stop() the thread exits
        cleanly.
        """
        from infra import frame_capture as fc

        _real_sleep = fc.time.sleep

        teardown_interval = fc.TEARDOWN_INTERVAL_SECONDS  # 3600.0
        fake_time = [0.0]
        open_count = [0]

        def fake_monotonic():
            return fake_time[0]

        def fake_av_open(*args, **kwargs):
            open_count[0] += 1
            container = MagicMock()
            container.streams.video = [mock_av_stream]

            def demux_gen(stream_arg):
                # First container: yield 3 frames then raise (triggers retry)
                if open_count[0] == 1:
                    for i in range(3):
                        pkt = MagicMock()
                        pkt.dts = i
                        pkt.decode.return_value = [mock_av_frame]
                        yield pkt
                    raise OSError("simulated RTSP disconnect")
                # Post-teardown container: yield 3 frames then raise.
                # Subsequent retries reuse this container (av.open not
                # called again), but each call to demux() creates a new
                # generator — so it yields 3 more frames each retry.
                else:
                    for i in range(3):
                        pkt = MagicMock()
                        pkt.dts = 100 + i
                        pkt.decode.return_value = [mock_av_frame]
                        yield pkt
                    raise OSError("simulated RTSP disconnect")

            container.demux = demux_gen
            return container

        monkeypatch.setattr(fc.time, "monotonic", fake_monotonic)
        # sleep() uses _real_sleep but caps at 0.05s to keep the test fast.
        # Without capping, backoff (1,2,4,8,16,30s) would make the test
        # extremely slow. With max_reconnect_attempts=0 (retry forever),
        # the backoff loop must not spin at full speed — capping at 50ms
        # gives the main thread a chance to run.
        monkeypatch.setattr(fc.time, "sleep", lambda _s: _real_sleep(min(_s or 0, 0.05)))

        with patch("av.open", side_effect=fake_av_open):
            reader = PersistentRTSPReader(
                "rtsp://u:p@h:554/h",
                ring_size=10,
                max_reconnect_attempts=0,  # cap disabled, keep looping
            )
            reader.start()

            # Wait for initial frames to be decoded.
            _real_sleep(0.1)
            frames_before = len(reader._ring)
            assert frames_before >= 3, (
                f"Expected at least 3 frames before teardown, got {frames_before}"
            )

            # Advance past teardown interval
            fake_time[0] = teardown_interval + 1.0

            # Wait for teardown to fire and post-teardown frames to be decoded.
            _real_sleep(0.1)
            frames_after = len(reader._ring)

            # Ring should have grown (post-teardown frames appended) and
            # be bounded by ring_size. Crucially, teardown does NOT clear
            # the ring — old frames are preserved.
            assert frames_after > frames_before, (
                f"Ring should have more frames after teardown: "
                f"before={frames_before}, after={frames_after}"
            )
            assert frames_after <= 10, (
                f"Ring should not exceed ring_size=10, got {frames_after}"
            )

            reader.stop(timeout=2.0)

    def test_stats_exposes_teardown_counters(self, monkeypatch):
        """AC: stats() dict contains 'teardowns_total' and 'seconds_since_teardown'
        keys with correct values.

        Before start(), teardowns_total == 0 and seconds_since_teardown is None
        (_last_teardown_monotonic is not set yet). After start(), _last_teardown_
        monotonic is set by start(), so seconds_since_teardown is a finite number.
        """
        from infra import frame_capture as fc

        fake_time = [1000.0]

        def fake_monotonic():
            return fake_time[0]

        monkeypatch.setattr(fc.time, "monotonic", fake_monotonic)

        reader = PersistentRTSPReader("rtsp://u:p@h:554/h")
        stats = reader.stats()

        assert "teardowns_total" in stats
        assert stats["teardowns_total"] == 0, (
            f"Expected 0 teardowns on fresh reader, got {stats['teardowns_total']}"
        )
        assert "seconds_since_teardown" in stats
        # Before start, _last_teardown_monotonic is None → None.
        assert stats["seconds_since_teardown"] is None, (
            "seconds_since_teardown should be None before start()"
        )

        # start() sets _last_teardown_monotonic = time.monotonic() internally.
        reader.start()
        stats_after = reader.stats()
        assert stats_after["seconds_since_teardown"] is not None, (
            "seconds_since_teardown should be set after start()"
        )
        assert stats_after["seconds_since_teardown"] >= 0
        reader.stop(timeout=2.0)

    def test_failure_driven_cap_still_fails_loud_at_10(
        self, monkeypatch, caplog
    ):
        """AC: after hitting the reconnect cap, the reader logs ERROR, exits
        the decode loop cleanly, and is_healthy() == False. The in-thread
        teardown does NOT reset the consecutive failure counter, so the cap
        still raises loudly.

        Uses the same pattern as TestFailureDrivenReconnectCap but runs under
        the teardown era to ensure US-049c didn't break this path.
        """
        from infra import frame_capture as fc

        decode_calls = {"n": 0}

        def always_fail_decode() -> None:
            decode_calls["n"] += 1
            raise RuntimeError("simulated RTSP decode failure")

        # Patch Event.wait so it returns True immediately (simulating stop_event
        # being set), which lets the cap block break the run loop cleanly.
        def fake_wait(self, timeout=None):
            self.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", fake_wait)
        monkeypatch.setattr(fc.time, "sleep", lambda _s: None)

        reader = PersistentRTSPReader(
            "rtsp://u:p@h:554/h",
            max_reconnect_attempts=3,
        )
        reader._decode_iteration = always_fail_decode  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger="frame_capture"):
            reader.start()
            # Wait for the decode thread to hit the cap.
            deadline = _time.monotonic() + 2.0
            while _time.monotonic() < deadline and decode_calls["n"] < 3:
                _time.sleep(0.01)
            _time.sleep(0.1)
            reader.stop(timeout=2.0)

        # AC — exactly one ERROR log with cap-reached marker.
        cap_logs = [
            r for r in caplog.records
            if "consecutive_reconnect_cap_reached" in r.getMessage()
        ]
        assert len(cap_logs) == 1, (
            f"Expected exactly one consecutive_reconnect_cap_reached log, "
            f"got {len(cap_logs)}: {[r.getMessage() for r in cap_logs]}"
        )

        # AC — is_healthy() is False after cap exhaustion.
        assert reader.is_healthy() is False

        # AC — exactly N decode calls (cap tipped on Nth failure).
        assert decode_calls["n"] == 3, (
            f"Expected 3 _decode_iteration calls (cap on 3rd failure), "
            f"got {decode_calls['n']}"
        )

        # AC — no 'proceed' log line (old watchdog proceed-anyway path is gone).
        all_msgs = [r.getMessage() for r in caplog.records]
        assert not any("proceed" in m.lower() for m in all_msgs), (
            f"Found 'proceed' in log messages — old watchdog path should be gone: {all_msgs}"
        )
