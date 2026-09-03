"""
Tests for infra/frame_capture.py — capture orchestrator + re-exports.

Covers:
    - DEFAULT_MAX_SIZE constant
    - capture_frames: persistent reader fast path (mocked), on-demand RTSP
      (mocked to fail), snapshot fallback, all-fail returns []
    - _capture_from_snapshot: glob + sort by mtime + copy
    - Re-exports: downscale_for_qwen, crop_face_region_from_4k,
      NATIVE_RES, INSIGHTFACE_CROP_SIZE, load_camera_creds,
      resolve_camera_name, CAMERA_NAME_ALIASES — all point to the same
      objects as in their owning modules.
      (§11.88 2026-09-01: QWEN_INPUT_SIZE removed — see image_prep.NATIVE_RES)
"""

import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from infra.frame_capture import (
    CAMERA_NAME_ALIASES,
    DEFAULT_MAX_SIZE,
    _capture_from_snapshot,
    capture_frames,
    crop_face_region_from_4k,
    downscale_for_qwen,
    load_camera_creds,
    resolve_camera_name,
)
from infra.persistent_rtsp import PersistentRTSPReader

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_max_size_is_4k(self):
        # Cameras stream at full 4K so Phase 6A can crop face regions
        # from the original.
        assert DEFAULT_MAX_SIZE == (3840, 2160)


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


class TestReExports:
    """Verify the orchestrator re-exports every extracted symbol."""

    def test_image_prep_symbols(self):
        from infra.image_prep import (
            crop_face_region_from_4k as direct_crop,
        )
        from infra.image_prep import (
            downscale_for_qwen as direct_down,
        )
        # Re-exports must point to the same function objects.
        assert downscale_for_qwen is direct_down
        assert crop_face_region_from_4k is direct_crop

    def test_camera_creds_symbols(self):
        from infra.camera_creds import load_camera_creds as direct_lc
        assert load_camera_creds is direct_lc

    def test_camera_aliases_symbols(self):
        from infra.camera_aliases import (
            CAMERA_NAME_ALIASES as direct_aliases,
        )
        from infra.camera_aliases import (
            resolve_camera_name as direct_rcn,
        )
        assert resolve_camera_name is direct_rcn
        assert CAMERA_NAME_ALIASES is direct_aliases


# ---------------------------------------------------------------------------
# _capture_from_snapshot (private — pure file-glob + copy)
# ---------------------------------------------------------------------------


class TestCaptureFromSnapshot:
    """Glob + sort + copy the most recent JPEGs."""

    def test_no_snapshot_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as output_dir:
            result = _capture_from_snapshot(
                snapshot_dir="/nonexistent/path",
                output_dir=output_dir,
                count=3,
            )
            assert result == []

    def test_empty_snapshot_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as snap_dir, tempfile.TemporaryDirectory() as output_dir:
            result = _capture_from_snapshot(
                snapshot_dir=snap_dir,
                output_dir=output_dir,
                count=3,
            )
            assert result == []

    def test_copies_most_recent_jpegs(self):
        with tempfile.TemporaryDirectory() as snap_dir, tempfile.TemporaryDirectory() as output_dir:
            # Create 3 valid JPEGs with staggered mtimes. §11.88 (2026-09-01):
            # snapshot fallback re-encodes JPEG→PNG via PIL, so source files
            # must actually decode (no more passing fake-JPEG-bytes through
            # shutil.copyfile).
            for i in range(3):
                p = os.path.join(snap_dir, f"frame_{i}.jpg")
                arr = np.zeros((10, 10, 3), dtype=np.uint8) + i * 50
                cv2.imwrite(p, arr, [cv2.IMWRITE_JPEG_QUALITY, 85])
                os.utime(
                    p, (time.time() - (3 - i) * 10, time.time() - (3 - i) * 10)
                )

            result = _capture_from_snapshot(
                snapshot_dir=snap_dir,
                output_dir=output_dir,
                count=3,
            )
            assert len(result) == 3
            # All output files exist (§11.88: now PNG, not JPEG).
            for path in result:
                assert os.path.exists(path)
                assert path.startswith(output_dir)
                # Filename pattern: snapshot_001.png, snapshot_002.png, snapshot_003.png
                basename = os.path.basename(path)
                assert basename.startswith("snapshot_")
                assert basename.endswith(".png")

    def test_count_limits_results(self):
        with tempfile.TemporaryDirectory() as snap_dir, tempfile.TemporaryDirectory() as output_dir:
            # Create 5 valid JPEGs, but only request 2. §11.88: must decode.
            for i in range(5):
                p = os.path.join(snap_dir, f"frame_{i}.jpg")
                arr = np.zeros((10, 10, 3), dtype=np.uint8) + i * 50
                cv2.imwrite(p, arr, [cv2.IMWRITE_JPEG_QUALITY, 85])
                os.utime(p, (time.time() - i, time.time() - i))

            result = _capture_from_snapshot(
                snapshot_dir=snap_dir,
                output_dir=output_dir,
                count=2,
            )
            assert len(result) == 2

    def test_non_jpeg_files_ignored(self):
        with tempfile.TemporaryDirectory() as snap_dir, tempfile.TemporaryDirectory() as output_dir:
            # Create a valid JPEG and a non-image file. §11.88: must decode.
            jpeg_path = os.path.join(snap_dir, "valid.jpg")
            arr = np.zeros((10, 10, 3), dtype=np.uint8)
            cv2.imwrite(jpeg_path, arr, [cv2.IMWRITE_JPEG_QUALITY, 85])
            non_jpeg = os.path.join(snap_dir, "notes.txt")
            with open(non_jpeg, "w") as f:
                f.write("not a jpeg")

            result = _capture_from_snapshot(
                snapshot_dir=snap_dir,
                output_dir=output_dir,
                count=3,
            )
            assert len(result) == 1
            # Output is renamed to snapshot_001.png (§11.88: PNG, not JPEG).
            assert os.path.basename(result[0]) == "snapshot_001.png"


# ---------------------------------------------------------------------------
# capture_frames: orchestrator (with mocked persistent reader + RTSP)
# ---------------------------------------------------------------------------


class TestCaptureFrames:
    """Verify the orchestrator's fallback chain."""

    def test_persistent_reader_path_returns_frames(self):
        with tempfile.TemporaryDirectory() as output_dir:
            mock_reader = MagicMock(spec=PersistentRTSPReader)
            mock_reader.is_healthy.return_value = True
            mock_reader.uptime_seconds.return_value = 0.0
            mock_reader.frames_decoded_total = 0
            mock_reader.reconnects_total = 0
            expected_paths = [os.path.join(output_dir, "frame_001.jpg")]
            mock_reader.get_recent_frames.return_value = expected_paths

            with patch("infra.frame_capture.get_reader_for_url", return_value=None):
                result = capture_frames(
                    rtsp_url="rtsp://test/stream",
                    output_dir=output_dir,
                    count=3,
                    reader=mock_reader,
                )
            assert result == expected_paths
            mock_reader.get_recent_frames.assert_called_once()

    def test_persistent_reader_unhealthy_raises(self):
        # 2026-08-14 — Fail-loud contract. When a persistent reader
        # is in play and is unhealthy, capture_frames MUST raise
        # instead of falling through to on-demand capture. The
        # on-demand path produces degraded-but-plausible-looking
        # captures that miss the pre-event motion trail.
        with tempfile.TemporaryDirectory() as output_dir:
            mock_reader = MagicMock(spec=PersistentRTSPReader)
            mock_reader.is_healthy.return_value = False
            mock_reader.uptime_seconds.return_value = 0
            mock_reader.frames_decoded_total = 0
            mock_reader.reconnects_total = 0

            with patch(
                "infra.frame_capture.get_reader_for_url", return_value=None
            ), pytest.raises(RuntimeError, match="unhealthy"):
                capture_frames(
                    rtsp_url="rtsp://test/stream",
                    output_dir=output_dir,
                    count=3,
                    reader=mock_reader,
                )

    def test_rtsp_failure_falls_back_to_snapshot(self):
        with tempfile.TemporaryDirectory() as output_dir, tempfile.TemporaryDirectory() as snap_dir:
            # Create a valid JPEG snapshot so the snapshot path has something.
            # §11.88 (2026-09-01): PIL.Image.open must decode, so source must
            # be a real JPEG (was fake-JPEG-bytes passed via shutil.copyfile).
            snap_file = os.path.join(snap_dir, "recent.jpg")
            arr = np.zeros((10, 10, 3), dtype=np.uint8) + 128
            cv2.imwrite(snap_file, arr, [cv2.IMWRITE_JPEG_QUALITY, 85])

            with patch("infra.frame_capture.get_reader_for_url", return_value=None), \
                 patch("infra.frame_capture._capture_from_rtsp", return_value=[]):
                result = capture_frames(
                    rtsp_url="rtsp://test/stream",
                    output_dir=output_dir,
                    count=3,
                    snapshot_dir=snap_dir,
                )
            # Snapshot fallback should have produced at least 1 frame.
            assert len(result) >= 1

    def test_all_paths_fail_returns_empty(self):
        with tempfile.TemporaryDirectory() as output_dir:
            with patch("infra.frame_capture.get_reader_for_url", return_value=None), \
                 patch("infra.frame_capture._capture_from_rtsp", return_value=[]):
                # No snapshot_dir → no fallback possible.
                result = capture_frames(
                    rtsp_url="rtsp://test/stream",
                    output_dir=output_dir,
                    count=3,
                )
            assert result == []

    def test_output_dir_created_if_missing(self):
        with tempfile.TemporaryDirectory() as parent:
            output_dir = os.path.join(parent, "new_subdir")
            # Don't create output_dir beforehand.
            assert not os.path.exists(output_dir)
            with patch("infra.frame_capture.get_reader_for_url", return_value=None), \
                 patch("infra.frame_capture._capture_from_rtsp", return_value=[]):
                capture_frames(
                    rtsp_url="rtsp://test/stream",
                    output_dir=output_dir,
                    count=3,
                )
            # Orchestrator should have created it.
            assert os.path.isdir(output_dir)

    def test_frame_offsets_uses_get_frames_by_offset(self):
        with tempfile.TemporaryDirectory() as output_dir:
            mock_reader = MagicMock(spec=PersistentRTSPReader)
            mock_reader.is_healthy.return_value = True
            mock_reader.uptime_seconds.return_value = 0.0
            mock_reader.frames_decoded_total = 0
            mock_reader.reconnects_total = 0
            expected_paths = [
                os.path.join(output_dir, "frame_001.jpg"),
                os.path.join(output_dir, "frame_002.jpg"),
            ]
            mock_reader.get_frames_by_offset.return_value = expected_paths

            with patch("infra.frame_capture.get_reader_for_url", return_value=None):
                result = capture_frames(
                    rtsp_url="rtsp://test/stream",
                    output_dir=output_dir,
                    count=3,
                    frame_offsets=[0, 30, 60, 90, 120, 150],  # gatekeeper pre-event trail
                    reader=mock_reader,
                )
            assert result == expected_paths
            mock_reader.get_frames_by_offset.assert_called_once()
            # get_recent_frames should NOT have been called.
            mock_reader.get_recent_frames.assert_not_called()

    def test_url_keyed_reader_used_when_registered(self):
        # When reader=None but a reader is registered for the alert's
        # RTSP URL (per-camera registry, Phase.87), capture_frames
        # auto-resolves it via get_reader_for_url() and uses it.
        with tempfile.TemporaryDirectory() as output_dir:
            mock_reader = MagicMock(spec=PersistentRTSPReader)
            mock_reader.is_for_url.return_value = True  # not consulted anymore; kept for clarity
            mock_reader.is_healthy.return_value = True
            mock_reader.uptime_seconds.return_value = 0.0
            mock_reader.frames_decoded_total = 0
            mock_reader.reconnects_total = 0
            mock_reader.get_recent_frames.return_value = [os.path.join(output_dir, "frame_001.jpg")]

            with patch("infra.frame_capture.get_reader_for_url", return_value=mock_reader):
                result = capture_frames(
                    rtsp_url="rtsp://test/stream",
                    output_dir=output_dir,
                    count=3,
                )
            # Reader was used.
            mock_reader.get_recent_frames.assert_called_once()
            assert len(result) == 1

    def test_url_keyed_reader_skipped_when_url_unregistered(self):
        # When get_reader_for_url returns None (no reader registered
        # for this URL), capture_frames falls through to on-demand RTSP
        # — same behavior as the singleton-mismatch case but driven by
        # the per-camera registry rather than the singleton's own
        # is_for_url().
        with tempfile.TemporaryDirectory() as output_dir:
            mock_reader = MagicMock(spec=PersistentRTSPReader)

            with patch("infra.frame_capture.get_reader_for_url", return_value=None), \
                 patch("infra.frame_capture._capture_from_rtsp", return_value=[]):
                result = capture_frames(
                    rtsp_url="rtsp://other/stream",
                    output_dir=output_dir,
                    count=3,
                )
            # No reader used at all.
            mock_reader.get_recent_frames.assert_not_called()
            mock_reader.is_for_url.assert_not_called()
            # On-demand RTSP was attempted.
            assert result == []  # RTSP returned empty, no snapshot fallback
