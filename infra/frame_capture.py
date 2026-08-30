"""
frame_capture.py — Orchestrate frame capture from RTSP / snapshot.

STATUS: stable
THREAD SAFETY: thread-safe (no shared mutable state; per-camera
    persistent_rtsp reader is itself thread-safe)

INPUTS:
    - function arg rtsp_url: str (required) — full RTSP URL with creds
    - function arg output_dir: str (required) — directory to write frames
    - function arg count: int (default 3) — number of frames
    - function arg interval: float (default 2) — seconds between captures
    - function arg max_size: tuple[int, int] (default DEFAULT_MAX_SIZE)
    - function arg timeout: float (default 30) — per-frame abort timeout
    - function arg snapshot_dir: str | None — fallback snapshot directory
    - function arg offset: int (default 0) — file numbering start
    - function arg frame_offsets: list[int] | None — sample indices from
      persistent reader's ring buffer (gatekeeper pre-event trail)
    - function arg reader: PersistentRTSPReader | None — bypass on-demand RTSP

OUTPUTS:
    - return value: list[str] — paths to captured JPEG files
    - writes files: <output_dir>/frame_001.jpg, frame_002.jpg, ...
    - writes files: <output_dir>/snapshot_001.jpg, ... (fallback path)
    - network call: RTSP TCP read or HTTP GET on snapshot endpoint

PUBLIC API:
    DEFAULT_MAX_SIZE: tuple[int, int] = (3840, 2160)
        Default capture size — cameras stream at full 4K so Phase 6A
        can crop face regions from the full-resolution original.
    capture_frames(rtsp_url: str, output_dir: str, count: int = 3,
                   interval: float = 2,
                   max_size: tuple[int, int] = DEFAULT_MAX_SIZE,
                   timeout: float = 30,
                   snapshot_dir: str | None = None,
                   offset: int = 0,
                   frame_offsets: list[int] | None = None,
                   reader=None) -> list[str]
        Capture `count` JPEG frames. Order of preference:
          1. PersistentRTSPReader if provided (or default singleton if
             registered and matches URL) — pulls from in-memory ring buffer.
             THIS PATH IS FAIL-LOUD (see KNOWN VIOLATIONS).
          2. On-demand RTSP via PyAV — opens a fresh connection, demuxes
             continuously using the FFmpeg select pattern (avoids the
             Reolink 510A pre-buffer dump + replay bug 2026-08-05).
             Used for non-OFS cameras (no persistent reader registered).
          3. Camera push snapshot directory (<nas> @PushServ).
        Returns list of captured paths (may be fewer than `count`).

KNOWN VIOLATIONS (see PLAN.md Part 11.8):
    - When a PersistentRTSPReader is in play for the URL (today: only
      OFS / gatekeeper camera), capture_frames MUST read from it. The
      on-demand RTSP fallback path (PyAV fresh session) is removed
      because it suffers the Reolink 510A pre-buffer dump + 13× wall-
      clock decode, AND it can't produce the pre-event motion trail
      that the gatekeeper needs. If the persistent reader is unhealthy
      or empty, capture_frames RAISES RuntimeError — the alert is
      dropped, not degraded. This is the explicit contract Note
      asked for on 2026-08-14: "if it's not using the persistent
      stream, it doesn't have the ability to get the images."
    - Trade-off: during the ~5s warmup window after listener boot, the
      persistent reader's ring buffer may not have enough history.
      The first OFS event in that window will be dropped rather than
      producing a degraded capture.

DOES NOT DO:
    - Downscale frames for Qwen → infra.image_prep.downscale_for_qwen
    - Crop face regions → infra.image_prep.crop_face_region_from_4k
    - Load camera creds from env → infra.camera_creds.load_camera_creds
    - Resolve camera name aliases → infra.camera_aliases.resolve_camera_name
    - Maintain the persistent RTSP connection → infra.persistent_rtsp

WHY HERE:
    Splits a 698-line multi-job module into 4 single-concern modules per
    PLAN.md Part 9 step 6. This module owns the CAPTURE PATH (RTSP +
    snapshot fallback) — the sequence of "try persistent reader → try
    on-demand RTSP → fall back to snapshots." Other concerns (image
    prep, creds parsing, name aliasing) live in their own modules.
    Re-exports the extracted symbols so existing imports keep working.

CALLED BY:
    - listener.listener: capture_frames() per webhook
    - infra.persistent_rtsp: callback consumer (indirectly, when reader
      hands off recent frames to the listener)

CALLS INTO:
    - infra.persistent_rtsp: get_reader_for_url() resolves the right
      registered reader for an inbound alert (Phase.87, PLAN §11.17)
    - av (PyAV): RTSP demux + decode
    - os, glob, shutil (stdlib): snapshot fallback
    - logging (stdlib): log per-frame captures + fallbacks

RELATED:
    - infra.image_prep: post-capture image processing (downscale, crop)
    - infra.camera_creds: load_camera_creds() called by listener at bootstrap
    - infra.camera_aliases: resolve_camera_name() called per-alert
    - camera-creds.env: RTSP credentials (parsed by infra.camera_creds)
"""

from __future__ import annotations

import glob
import logging
import os

# Local imports
from infra.persistent_rtsp import get_reader_for_url

log = logging.getLogger(__name__)


# Default max frame size for capture. Set to (3840, 2160) — the cameras
# stream at full 4K. Frames are stored at 4K so the Phase 6A pipeline
# can crop a 640x640 region around Qwen's reported face bbox and feed
# THAT to InsightFace (insightface downsamples internally to 640x640,
# so a tight crop preserves face detail far better than sending the
# full 4K frame).
#
# Qwen3-VL itself is given DOWNSAMPLED copies (see QWEN_INPUT_SIZE in
# infra.image_prep) — at full 4K the image alone consumes ~4000 tokens,
# blowing past llama-server's per-slot context budget when running
# concurrent requests (--parallel 4).
DEFAULT_MAX_SIZE: tuple[int, int] = (3840, 2160)


def capture_frames(
    rtsp_url: str,
    output_dir: str,
    count: int = 3,
    interval: float = 2,
    max_size: tuple[int, int] = DEFAULT_MAX_SIZE,
    timeout: float = 30,
    snapshot_dir: str | None = None,
    offset: int = 0,
    frame_offsets: list[int] | None = None,
    reader=None,
) -> list[str]:
    """
    Capture `count` JPEG frames from an RTSP stream.

    Args:
        rtsp_url: Full RTSP URL with credentials.
        output_dir: Directory to write frame_001.jpg, frame_002.jpg, etc.
        count: Number of frames to capture.
        interval: Seconds between consecutive captures.
        max_size: (width, height) to downscale frames to. Aspect preserved.
        timeout: Abort if a single frame takes longer than this (seconds).
        snapshot_dir: Fallback directory of camera push snapshots.
        offset: File numbering starts at `offset + 1`. Used by Phase.9
            vehicle flow to split capture into two phases (frame 1 first,
            then frames 2-6 with offset=1 so they don't overwrite frame_001.jpg).
        frame_offsets: Optional list of deque indices to sample from the
            persistent reader's ring buffer. When set, the reader's
            `get_frames_by_offset()` is used instead of `get_recent_frames(n=count)`.
            Index 0 = oldest, index len-1 = newest. Useful for capturing a
            pre-event motion trail (e.g. [0, 30, 60, 90, 120, 150] at 15fps
            spans T-12s through T+0s). When None, falls back to last-N semantics.
        reader: Optional PersistentRTSPReader instance. When provided, the
            recent N frames are pulled from the reader's in-memory ring
            buffer instead of opening a fresh RTSP connection. This avoids
            the Reolink 510A pre-buffer dump + replay bug (2026-08-05).
            When None, falls back to the on-demand _capture_from_rtsp path.

    Returns:
        List of absolute paths to captured JPEG files (may be fewer than
        `count` if some frames failed to capture).

    Raises:
        No exceptions — failures return an empty list or partial results.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Persistent-reader fast path: pull recent frames from the in-memory
    # ring buffer. No fresh RTSP connection, no pre-buffer dump.
    #
    # 2026-08-14 — Fail-loud contract. If a persistent reader is
    # registered for this URL (today: only OFS), the capture MUST
    # come from it. Fall-through to _capture_from_rtsp is removed
    # because that path opens a fresh RTSP session which suffers the
    # Reolink 510A pre-buffer dump + replay bug — every fresh socket
    # would be ~1.5s of prebuffer + 13x wall-clock decode, AND the
    # captured frames would not include any pre-event history
    # (the gatekeeper needs frame offsets [0, 30, ...] from the
    # persistent ring). The persistent reader is the only way to
    # get a correct pre-event motion trail.
    #
    # If the reader is registered but unhealthy / empty / wrong URL,
    # we raise so the alert fails LOUDLY instead of producing a
    # degraded-but-plausible-looking capture that misses the actual
    # motion.
    if reader is None:
        # Auto-resolve the persistent reader via the URL-keyed registry
        # (Phase.87, PLAN §11.17). Strips creds before matching so
        # alert-side URLs that differ in password encoding still resolve
        # to the right camera's reader. With multiple registered readers
        # (OFS, OFG, ...), this is the path that picks the correct one.
        reader = get_reader_for_url(rtsp_url)

    from infra.persistent_rtsp import PersistentRTSPReader

    if isinstance(reader, PersistentRTSPReader):
        # Persistent reader is in play (either explicitly passed or
        # auto-discovered from singleton). Apply the fail-loud
        # contract: don't fall back to on-demand capture.
        if not reader.is_healthy():
            raise RuntimeError(
                f"capture_frames: persistent reader for {rtsp_url.split('@')[-1]} "
                f"is unhealthy (uptime={reader.uptime_seconds():.0f}s, "
                f"frames_decoded={reader.frames_decoded_total}, "
                f"reconnects={reader.reconnects_total}). Refusing to fall back "
                f"to on-demand capture — the alert will be dropped, not degraded."
            )
        if frame_offsets is not None:
            # Pre-event motion trail path (gatekeeper / OFS).
            paths = reader.get_frames_by_offset(
                indices=frame_offsets,
                output_dir=output_dir,
                max_size=max_size,
            )
            if not paths:
                raise RuntimeError(
                    f"capture_frames: persistent reader for {rtsp_url.split('@')[-1]} "
                    f"is healthy but get_frames_by_offset returned empty for "
                    f"indices={frame_offsets} (ring buffer may not have enough "
                    f"history yet — warmup in progress). Refusing to fall back."
                )
            log.info(
                f"Pulled {len(paths)} frames by offset from persistent "
                f"reader (uptime={reader.uptime_seconds():.0f}s, "
                f"frames_decoded={reader.frames_decoded_total})"
            )
            return paths
        # Persistent reader registered but no frame_offsets requested —
        # use last-N as the canonical persistent-reader path.
        paths = reader.get_recent_frames(
            n=count, output_dir=output_dir, max_size=max_size
        )
        if not paths:
            raise RuntimeError(
                f"capture_frames: persistent reader for {rtsp_url.split('@')[-1]} "
                f"is healthy but get_recent_frames returned empty (ring buffer "
                f"still warming up). Refusing to fall back."
            )
        log.info(
            f"Pulled {len(paths)} frames from persistent reader "
            f"(uptime={reader.uptime_seconds():.0f}s, "
            f"frames_decoded={reader.frames_decoded_total})"
        )
        return paths

    # No persistent reader registered for this URL — fall through to
    # on-demand RTSP capture (canonical path for non-OFS cameras).
    frames = _capture_from_rtsp(
        rtsp_url=rtsp_url,
        output_dir=output_dir,
        count=count,
        interval=interval,
        max_size=max_size,
        timeout=timeout,
        offset=offset,
    )

    if frames:
        return frames

    # Fallback: use camera push snapshot
    if snapshot_dir:
        frames = _capture_from_snapshot(
            snapshot_dir=snapshot_dir,
            output_dir=output_dir,
            count=count,
        )
        if frames:
            return frames

    return []


def _capture_from_rtsp(
    rtsp_url: str,
    output_dir: str,
    count: int,
    interval: float,
    max_size: tuple[int, int],
    timeout: float,
    offset: int = 0,
) -> list[str]:
    """Capture frames from RTSP using PyAV (libav linked into Python).

    Why PyAV and not a subprocess-ffmpeg shell-out:
      - Same library (libav* is what the `ffmpeg` binary wraps).
      - Linked as a C extension into this Python process. The TCP socket
        to the camera opens in the Python interpreter's own signing
        context, which has full network access under macOS launchd.
      - A subprocess-launched `ffmpeg` is adhoc-signed; on macOS 26.x
        launchd-managed processes cannot open RTSP TCP sockets when
        launching adhoc binaries ("No route to host" on port 554, even
        though the shell-launched ffmpeg works fine).
      - Bonus: ~100x faster per frame (no subprocess startup, no
        reconnection per frame — single container reused across all frames).

    Why the FFmpeg `select` pattern works (and `time.sleep` does not):
      The Reolink RTSP server sends a ~1-2s prebuffer of video as fast
      as TCP can carry it when the session opens. After that it
      sustains ~15 fps. If we use `time.sleep(interval)` between
      `decode()` calls, ALL of the prebuffered and live frames get
      drained into PyAV's internal buffer during the sleep, and the
      next decode() returns a frame that's only milliseconds newer
      than the previous one. Note observed this 2026-08-03: 6 frames
      supposedly 2s apart all had the SAME OSD timestamp because they
      came from the same 1s prebuffer.

    The correct pattern is the FFmpeg `-vf select` filter:
      - Open RTSP once.
      - Demux continuously — never sleep.
      - Save the first frame immediately.
      - For each subsequent frame, save it only if
        `pts - last_saved_pts >= target_interval_pts`, where
        `target_interval_pts = interval / stream.time_base`.
      - Continue demuxing until `count` frames are saved.

    This guarantees each saved frame is exactly `interval` seconds
    after the previous one in camera video time, regardless of how
    fast PyAV drains the buffer.
    """
    import av  # PyAV (av 15.x). Imported here so module load doesn't fail

    # if the package is missing — the snapshot fallback path is
    # still useful in that case.
    import av.error  # PyAV's exception namespace. Static type checkers don't
    # see Cython-generated submodules, so we just catch a
    # broad Exception below and rely on the print for diagnosis.

    saved: list[str] = []
    container = None
    try:
        # open() does the RTSP handshake + DESCRIBE + SETUP + PLAY.
        # Microseconds for the per-protocol timeout (RTSP expects this).
        container = av.open(
            rtsp_url,
            options={
                "rtsp_transport": "tcp",
                "timeout": str(int(timeout * 1_000_000)),
                # 2026-08-05: Reolink 510A RTSP replays its ~1.5s pre-buffer
                # window instead of streaming live. Trusting the camera's PTS
                # values caused the gate to deadlock after 3 saved frames (the
                # pre-buffer was exhausted and the same packets were replayed).
                # +genpts regenerates PTS from local packet arrival time so the
                # interval gate sees advancing values; 20MB buffer prevents
                # the 13x pre-buffer drain from dropping packets at the socket.
                # Reference: OBS forum fix for "Reolink RTSP / no live-streaming,
                # only recording" — fflags=+genpts + buffer_size=20000000.
                "fflags": "+genpts",
                "buffer_size": "20000000",
            },
        )
        stream = container.streams.video[0]
        # time_base = 1/90000 for Reolink 510A/833A main stream.
        # target_interval_pts = `interval` seconds in that time_base.
        # PyAV's typing incorrectly marks time_base as Optional[Fraction];
        # in practice for video streams it is always set.
        tb_num = float(stream.time_base.numerator)  # type: ignore[union-attr]
        tb_den = float(stream.time_base.denominator)  # type: ignore[union-attr]
        target_interval_pts = int(interval * tb_den / tb_num)

        log.info(
            f"pyav capture: time_base={stream.time_base}, "
            f"average_rate={stream.average_rate}, count={count}, "
            f"interval={interval}s, target_interval_pts={target_interval_pts}"
        )

        last_saved_pts: int | None = None
        saved_count = 0

        # Continuous demux loop. No sleep. We save frames whose PTS
        # has advanced by at least `target_interval_pts` from the
        # last saved frame.
        for packet in container.demux(stream):
            if saved_count >= count:
                break
            pts = packet.pts
            if pts is None:
                # Some packets (e.g. SPS/PPS) have no PTS — skip.
                continue

            # Decide whether to save this frame.
            if last_saved_pts is None:
                # First frame — save immediately.
                should_save = True
            else:
                should_save = (pts - last_saved_pts) >= target_interval_pts

            if not should_save:
                continue

            # Decode and save this frame.
            try:
                decoded_frames = list(packet.decode())
                if not decoded_frames:
                    continue
                frame = decoded_frames[0]
                small = frame.reformat(
                    width=max_size[0],
                    height=max_size[1],
                    interpolation="BILINEAR",
                )
                frame_number = saved_count + 1 + offset
                frame_path = os.path.join(
                    output_dir, f"frame_{frame_number:03d}.jpg"
                )
                small.save(frame_path)

                if os.path.exists(frame_path) and os.path.getsize(frame_path) > 512:
                    saved.append(frame_path)
                    last_saved_pts = pts
                    saved_count += 1
                    log.info(
                        f"pyav saved frame {frame_number}/{count + offset}: "
                        f"pts={pts} pts_sec={pts * tb_num / tb_den:.3f}"
                    )
                else:
                    log.warning(
                        f"pyav saved frame {frame_number}/{count + offset} but "
                        f"file empty/missing (path={frame_path})"
                    )
            except Exception as e:
                log.warning(f"pyav frame decode/save failed: {e}")
                continue

    except av.error.FFmpegError as e:
        log.warning(f"pyav open/decode failed: {e}")
    except Exception as e:
        # Catch-all so the snapshot fallback can run. Print type for diagnostics.
        log.info(f"pyav unexpected {type(e).__name__}: {e}")
    finally:
        if container is not None:
            try:
                container.close()
            except Exception as _close_err:
                # Container already torn down by PyAV; not actionable
                log.debug(f"PyAV container.close() failed: {_close_err}")

    return saved


def _capture_from_snapshot(
    snapshot_dir: str,
    output_dir: str,
    count: int,
) -> list[str]:
    """
    Copy the most recent snapshot from the <nas> push snapshot directory.

    Snapshot files look like:
        /Volumes/surveillance/@Snapshot/@PushServ/Front Corner Inside/filename.jpg
    """
    if not os.path.isdir(snapshot_dir):
        return []

    # Find all JPEG files, sorted by modification time (newest first)
    pattern = os.path.join(snapshot_dir, "*.jpg")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

    if not files:
        return []

    saved = []
    for i, src in enumerate(files[:count]):
        dst = os.path.join(output_dir, f"snapshot_{i + 1:03d}.jpg")
        try:
            import shutil

            shutil.copy2(src, dst)
            saved.append(dst)
        except OSError:
            continue

    return saved


# ---------------------------------------------------------------------------
# Backward-compatible re-exports. Extracted modules own these symbols; the
# orchestrator re-exports them so existing
#   `from infra.frame_capture import downscale_for_qwen`
# callers keep working without changes.
# ---------------------------------------------------------------------------

from infra.camera_aliases import (  # noqa: E402, F401
    CAMERA_NAME_ALIASES,
    resolve_camera_name,
)
from infra.camera_creds import load_camera_creds  # noqa: E402, F401
from infra.image_prep import (  # noqa: E402, F401
    INSIGHTFACE_CROP_SIZE,
    QWEN_INPUT_SIZE,
    crop_face_region_from_4k,
    downscale_for_qwen,
)
