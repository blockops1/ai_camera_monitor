"""
frame_capture.py — Persistent RTSP reader with in-memory ring buffer.

STATUS: stable
THREAD SAFETY: uses threading.Lock
INPUTS:
    - rtsp_url: str (required)
    - ring_size: int (optional, default 180)
    - ffmpeg_flags: dict | None (optional)

OUTPUTS:
    - list[str]: PNG file paths from get_recent_frames
PUBLIC API:
    start() — Open RTSP and start decode loop (idempotent).
    stop(timeout=5.0) — Stop decode loop and close socket (idempotent).
    get_recent_frames(n, output_dir) -> list[str]
    capture_around_now(count, stride_s, output_dir) -> concurrent.futures.Future
    is_healthy(stale_seconds=_UNSET → env-tuned, default 20.0) -> bool
    uptime_seconds() -> float
    get_frames_by_offset(indices, output_dir) -> list[str]
    CameraCaptureRegistry.start_all() — Boot one reader per camera.
    CameraCaptureRegistry.stop_all() — Stop all readers, drain threads.
    CameraCaptureRegistry.is_healthy_all() -> dict[str, bool]
DOES NOT DO:
    - Parse camera-creds.env (infra/camera_creds)
    - Send Telegram alerts (listener/daemon.py)
    - Motion detection (infra/motion_detector)
    - On-demand capture (fresh socket per alert); this replaces that.
WHY HERE:
    On-demand RTSP suffered from Reolink kernel pre-buffer replay.
    Holding one connection per camera 24/7 avoids the warmup window.
CALLS INTO:
    - av (PyAV): RTSP demux; PIL.Image: JPEG save;
      infra.camera_creds: get_camera()/get_all_cameras();
      infra.paths: FRAMES_DIR
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import av
from PIL import Image

log = logging.getLogger("frame_capture")

RING_SIZE_DEFAULT = 12
RECONNECT_BACKOFF_INITIAL = 1.0   # seconds
RECONNECT_BACKOFF_MAX = 30.0
RECONNECT_BACKOFF_MULT = 2.0

# US-017e — Phase 6B.155 (PLAN §11.78). Cap on consecutive failure-driven
# reconnects. After N attempts the failure loop stops hammering the camera
# and waits for either (a) the next in-thread periodic teardown (US-049c,
# every 3600s) or (b) operator intervention via stop_event. Prevents log/CPU
# starvation during a camera-side Reolink stickiness event (where RTSP
# returns ERRNO 60 / Invalid data / 404 in a burst, but the camera IS still
# on the network — just temporarily refusing new sessions). Without this cap
# the loop will keep hammering the camera at the 30s max forever, filling
# logs and (worse) potentially blocking other code paths.
RECONNECT_MAX_ATTEMPTS_DEFAULT = 10

# US-049c: in-thread periodic teardown cadence. The _decode_iteration loop
# closes + reopens the RTSP container every TEARDOWN_INTERVAL_SECONDS
# so PyAV's C demux() can't wedge silently (observed on Reolink CAM5
# after 41h uptime: frames_decoded_total frozen, zero exceptions).
_MAX_RECONNECT_ATTEMPTS_ENV = "FARMSV_RTSP_MAX_RETRIES"

# US-049c: in-thread periodic teardown cadence. After every 3600 seconds
# the decode thread closes and re-opens the RTSP container to prevent
# PyAV's C demux() from wedging silently (go2rtc / Frigate pattern).
TEARDOWN_INTERVAL_SECONDS = 3600.0

# US-098: env-tunable stale-frame threshold. How many seconds with no frames
# before a reader is considered unhealthy. Default 20.0 exceeds OFP's observed
# 12s RTSP session-idle gap, so the listener rides through the natural cam gap
# instead of false-triggering a reconnect into it.
_STALE_SECONDS_ENV = "FARMSV_RTSP_STALE_SECONDS"

# US-098: env-tunable anti-flapping guard. A reader alive < this many seconds
# is NOT killed even if unhealthy (prevents reconnect storms during the cam's
# initial 12s idle window when the listener first subscribes).
_RECONNECT_MIN_UPTIME_ENV = "FARMSV_RTSP_RECONNECT_MIN_UPTIME"

# Sentinel: signals that no explicit arg was passed (caller wants env/default).
# Replaces `float | None` / `int | None` in resolver signatures — the None
# branch would silently pick up env vars, creating two data sources for the
# same value.
_UNSET = object()




def _resolve_max_reconnect_attempts(arg_value: int | object = _UNSET) -> int:
    """Resolve the max consecutive failure-driven reconnect attempts.

    Precedence:
    1. Explicit constructor arg (if not _UNSET)
    2. Env var FARMSV_RTSP_MAX_RETRIES (if set + non-empty)
    3. RECONNECT_MAX_ATTEMPTS_DEFAULT (10)

    Returns an int. A value <= 0 disables the cap (legacy behavior,
    retry forever). Raises ValueError if env var is set but not a valid
    int.
    """
    if arg_value is not _UNSET:
        return int(arg_value)
    env_val = os.environ.get(_MAX_RECONNECT_ATTEMPTS_ENV)
    if env_val:
        return int(env_val)
    return RECONNECT_MAX_ATTEMPTS_DEFAULT


# US-098 defaults (higher than legacy hardcoded values to reduce reconnect
# aggressiveness for cams like OFP with 12s RTSP session-idle gaps).
_STALE_SECONDS_DEFAULT = 20.0
_RECONNECT_MIN_UPTIME_DEFAULT = 30.0


def _resolve_stale_seconds(arg_value: float | object = _UNSET) -> float:
    """Resolve the stale-frame threshold in seconds.

    Precedence:
    1. Explicit constructor arg (if not _UNSET)
    2. Env var FARMSV_RTSP_STALE_SECONDS (if set + non-empty)
    3. _STALE_SECONDS_DEFAULT (20.0)

    Returns a float. Raises ValueError if env var is not a valid float.
    """
    if arg_value is not _UNSET:
        return float(arg_value)
    env_val = os.environ.get(_STALE_SECONDS_ENV)
    if env_val:
        return float(env_val)
    return _STALE_SECONDS_DEFAULT


def _resolve_reconnect_min_uptime(arg_value: float | object = _UNSET) -> float:
    """Resolve the anti-flapping minimum uptime in seconds.

    Precedence:
    1. Explicit constructor arg (if not _UNSET)
    2. Env var FARMSV_RTSP_RECONNECT_MIN_UPTIME (if set + non-empty)
    3. _RECONNECT_MIN_UPTIME_DEFAULT (30.0)

    Returns a float. Raises ValueError if env var is not a valid float.
    """
    if arg_value is not _UNSET:
        return float(arg_value)
    env_val = os.environ.get(_RECONNECT_MIN_UPTIME_ENV)
    if env_val:
        return float(env_val)
    return _RECONNECT_MIN_UPTIME_DEFAULT


def _sleep_until_stop_or_watchdog(
    stop_event: threading.Event,
    scheduled_reconnect_seconds: float,
) -> None:
    """Sleep that wakes on stop_event OR roughly every watchdog cadence.

    Used by PersistentRTSPReader._run_loop after the max-attempts cap is
    exhausted. The watchdog closes _container from its own thread; the
    resulting demux exception in the decode thread wakes this sleep via
    the cap_exhausted check returning control to the outer loop. The
    stop_event covers listener shutdown.

    We sleep in 60-second increments capped at `scheduled_reconnect_seconds`
    (so a 3600s cadence means we sleep at most 60s before checking). This
    trades efficiency for shutdown latency — listener shutdown will wait
    up to 60s for the sleep to expire. Acceptable for a graceful shutdown.
    """
    # At most 60s per iteration; bounded to one watchdog cycle.
    step = min(60.0, max(1.0, scheduled_reconnect_seconds))
    # Total budget: one watchdog cycle (the watchdog will fire and
    # either reopen us successfully or trigger another failure).
    deadline = time.monotonic() + scheduled_reconnect_seconds
    while time.monotonic() < deadline:
        if stop_event.wait(timeout=step):
            return
        if stop_event.is_set():
            return


class PersistentRTSPReader:
    """Long-lived RTSP reader: background thread decodes into a deque ring."""

    def __init__(
        self,
        rtsp_url: str,
        ring_size: int = RING_SIZE_DEFAULT,
        ffmpeg_flags: dict | None = None,
        max_reconnect_attempts: int = _UNSET,  # type: ignore[assignment]
        camera_id: str | None = None,
    ) -> None:
        self._rtsp_url = rtsp_url
        self._camera_id = camera_id
        self._ring_size = ring_size
        self._ring: deque[Image.Image] = deque(maxlen=ring_size)
        self._ring_lock = threading.Lock()
        self._ffmpeg_flags = ffmpeg_flags or {
            "rtsp_transport": "tcp",
            "timeout": "10000000",
            "fflags": "+genpts",
            "buffer_size": "20000000",
        }
        self._scheduled_reconnect_seconds = 3600.0  # 1 hour — hardcoded per US-049a
        self._max_reconnect_attempts = _resolve_max_reconnect_attempts(
            max_reconnect_attempts  # type: ignore[arg-type]
        )
        self._container: av.container.InputContainer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._container_lock = threading.Lock()
        self._start_time: float | None = None
        self._last_frame_time: float | None = None
        self._consecutive_errors = 0
        self._healthy = False
        self.frames_decoded_total = 0
        self.reconnects_total = 0
        self._teardowns_total = 0
        self._last_teardown_monotonic: float | None = None
        self._geometry_subscribed = False  # US-096b: one-shot geo subscribe

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> dict:
        """Return live runtime stats for this reader (debug introspection)."""
        with self._ring_lock:
            ring_size = len(self._ring)
        last = self._last_frame_time
        return {
            "frames_decoded_total": self.frames_decoded_total,
            "ring_size": ring_size,
            "ring_capacity": self._ring_size,
            "healthy_flag": self._healthy,
            "last_frame_time": last,
            "seconds_since_last_frame": (
                (time.monotonic() - last) if last is not None else None
            ),
            "consecutive_errors": self._consecutive_errors,
            "reconnects_total": self.reconnects_total,
            "teardowns_total": self._teardowns_total,
            "seconds_since_teardown": (
                (time.monotonic() - self._last_teardown_monotonic)
                if self._last_teardown_monotonic is not None
                else None
            ),
            "container_open": self._container is not None,
            "is_running": self.is_running,
        }

    def is_healthy(self, stale_seconds: float | object = _UNSET) -> bool:
        """Return True if the reader is healthy (recent frames, alive).

        The default stale threshold comes from the env-tunable resolver (US-098)
        rather than the legacy hardcoded 5.0, so the caller can use a sensible
        default without passing an argument.
        """
        if stale_seconds is _UNSET:
            stale_seconds = _resolve_stale_seconds()
        if not self._healthy or self._last_frame_time is None:
            return False
        return (time.monotonic() - self._last_frame_time) < stale_seconds  # type: ignore[operator]

    def _subscribe_geometry(self) -> None:
        """Probe cv2.VideoCapture for live stream geometry and write into
        infra.camera_creds._all_cameras on the matching camera entry.

        Called once at first RTSP open. Uses a lightweight metadata probe
        — does NOT decode frames.  Idempotent: skips on teardown reconnects
        via the _geometry_subscribed flag.

        Failure modes:
        - cap.get returns 0 for width or height -> log WARNING, proceed with
          None for width/height (gate in US-096c falls back to frame.shape).
        - cap reports width < 1280 -> log WARNING (substream), proceed.
        - RTSP probe fails to open -> log WARNING, skip.
        - No matching camera_creds entry -> log DEBUG, skip.
        """
        if self._camera_id is None:
            return
        # US-096b: one-shot — do not re-probe on teardown reconnects.
        if self._geometry_subscribed:
            return
        # Look up the camera entry by matching our camera_id (prefix) to the
        # prefix stored in each _all_cameras entry. Use get_all_cameras() at
        # runtime, not the cached module dict from import time.
        import cv2

        import infra.camera_creds as _camera_creds

        all_cam = _camera_creds.get_all_cameras()
        cam_entry = None
        for info in all_cam.values():
            if info.get("prefix") == self._camera_id:
                cam_entry = info
                break
        if cam_entry is None:
            log.debug(
                "frame_capture: no camera_creds entry for %s; skipping geometry",
                self._camera_id,
            )
            return

        # Lightweight cv2 probe — open the RTSP stream, read metadata, close.
        cap = cv2.VideoCapture(self._rtsp_url)
        if not cap.isOpened():
            log.warning(
                "frame_capture: %s cv2 probe failed to open RTSP; "
                "geometry not populated",
                self._camera_id,
            )
            cap.release()
            return

        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        cap.release()

        fmt = cam_entry.get("frame_format")

        # Zero-value guard
        if w == 0 or h == 0:
            log.warning(
                "frame_capture: %s could not subscribe geometry, w=%d h=%d",
                self._camera_id,
                w,
                h,
            )
            self._geometry_subscribed = True
            return

        # Substream guard
        if w < 1280:
            log.warning(
                "frame_capture: %s negotiated substream %dx%d; "
                "expected main stream >=1280 wide",
                self._camera_id,
                w,
                h,
            )

        # Write into camera_creds
        cam_entry["width"] = w
        cam_entry["height"] = h
        cam_entry["fps"] = fps
        self._geometry_subscribed = True

        log.info(
            "frame_capture: %s subscribed %dx%d @ %.1ffps %s",
            self._camera_id,
            w,
            h,
            fps,
            fmt or "unknown",
        )

    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._start_time = time.monotonic()
        self._last_teardown_monotonic = time.monotonic()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"RTSP[{self._rtsp_url.split('@')[-1]}]",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "PersistentRTSPReader started for %s",
            self._rtsp_url.split("@")[-1],
        )

    def stop(self, timeout: float = 12.0) -> None:
        """Stop the decode loop and drop the container reference.

        After *timeout* seconds: the join has expired, the native
        demux() thread may still be blocked inside PyAV's C demux()
        call.  We acquire _container_lock to serialize with any
        in-progress demux iteration, then explicitly call
        _container.close() at the Python level to force the
        underlying RTSP socket closed.  The lock prevents a
        SIGSEGV from racing the native thread.  Idempotent:
        repeated calls are safe.
        """
        if not self.is_running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        # Post-join: the demux thread may still be inside the native
        # C call.  Acquiring _container_lock serializes with the
        # demux loop's own _container_lock usage in _decode_iteration,
        # so we can safely call close() here.
        with self._container_lock:
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:
                    # Container may already be closed by the demux
                    # thread exiting; this is not an error.
                    log.exception("stop: container.close() failed")
                self._container = None
        self._healthy = False

    def get_recent_frames(
        self,
        n: int,
        output_dir: str,
    ) -> list[str]:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        self._clean_old(output_dir)
        with self._ring_lock:
            frames = list(self._ring)[-n:]
        if not frames:
            return []
        return self._save_frames(frames, output_dir, start=1)

    def capture_around_now(
        self,
        count: int,
        stride_s: float,
        output_dir: str,
    ) -> concurrent.futures.Future:
        """Capture *count* frames at *stride_s* intervals into *output_dir*.

        Returns a ``concurrent.futures.Future`` immediately.  A per-alert
        worker thread (daemon=True, name "capture-{camera_id}") grabs the
        most recent frame from ``self._ring`` on each stride, saves it as
        ``frame_NNN.png``, and sets the Future result to the list of paths.

        Only ``future.result()`` blocks — the calling thread is free
        immediately after this call returns.
        """
        future: concurrent.futures.Future[list[str]] = (
            concurrent.futures.Future()
        )
        name = f"capture-{self._camera_id}" if self._camera_id else "capture-unknown"

        def _worker() -> None:
            """Run in a daemon thread; set Future result/exception."""
            try:
                result = self._capture_worker(count, stride_s, output_dir)
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)

        t = threading.Thread(
            target=_worker,
            name=name,
            daemon=True,
        )
        t.start()
        return future

    def _capture_worker(
        self,
        count: int,
        stride_s: float,
        output_dir: str,
    ) -> list[str]:
        """Per-capture worker: grab frame, sleep, repeat.

        Called from a daemon thread so the Future is returned immediately
        to the caller.
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        self._clean_old(output_dir)
        out_paths: list[str] = []
        for i in range(count):
            if i > 0:
                time.sleep(stride_s)
            with self._ring_lock:
                if self._ring:
                    frame = self._ring[-1]
                else:
                    frame = None
            if frame is not None:
                self._save_one(frame, output_dir, i + 1, out_paths)
            else:
                log.warning(
                    "capture_around_now: %s ring empty at i=%d, "
                    "skipping frame",
                    self._camera_id or "unknown",
                    i,
                )
        return out_paths

    def get_frames_by_offset(
        self,
        indices: list[int],
        output_dir: str,
    ) -> list[str]:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        self._clean_old(output_dir)
        with self._ring_lock:
            ring_list = list(self._ring)
        out_paths: list[str] = []
        for idx in indices:
            if idx < 0 or idx >= len(ring_list):
                continue
            self._save_one(
                ring_list[idx], output_dir, len(out_paths) + 1, out_paths
            )
        return out_paths

    # -- internal helpers --

    def _clean_old(self, output_dir: str) -> None:
        for old in Path(output_dir).glob("frame_*.png"):
            try:
                old.unlink()
            except OSError:
                log.exception("_clean_old: failed to unlink %s", old)
                raise

    def _save_frames(
        self,
        frames: list[Image.Image],
        output_dir: str,
        start: int = 1,
    ) -> list[str]:
        out_paths: list[str] = []
        for i, frame in enumerate(frames, start=start):
            self._save_one(frame, output_dir, i, out_paths)
        return out_paths

    def _save_one(
        self,
        img: Image.Image,
        output_dir: str,
        n: int,
        out_paths: list[str],
    ) -> None:
        out_path = os.path.join(output_dir, f"frame_{n:03d}.png")
        img.save(out_path, format="PNG", optimize=True)
        out_paths.append(out_path)

    def _run_loop(self) -> None:
        """Main decode loop. Runs in a background thread.

        Phase 6B.155 (PLAN §11.78): failure-driven reconnects are capped at
        `self._max_reconnect_attempts`. After the cap, the loop stops retrying
        and waits on _stop_event with the configured cadence. The in-thread
        periodic teardown (US-049c, every 3600s) re-opens the container and
        either succeeds (logging a recovery line + resetting the cap) or hits
        the cap again. The outer while-loop retries the decode without
        re-entering this except-block exhaustion logic on each iteration.

        Per the 2026-08-28 CAM3 incident (6 errors in 80s with ERRNO 60 /
        Invalid data / 404 — Reolink RTSP session stickiness), this caps
        log spam and CPU usage during long camera outages. The watchdog +
        launchd-respawn antipattern was removed in US-049b/US-049d.
        """
        backoff = RECONNECT_BACKOFF_INITIAL
        cap_exhausted = False
        while not self._stop_event.is_set():
            try:
                self._decode_iteration()
                # Clean exit or successful decode (stop_event set OR
                # _decode_iteration returned normally).
                if cap_exhausted:
                    log.info(
                        "RTSP recovered after %d consecutive failures — "
                        "decoder running normally",
                        self._consecutive_errors,
                    )
                break
            except Exception as e:  # noqa: BLE001
                self._consecutive_errors += 1
                self._healthy = False
                if self._stop_event.is_set():
                    break

                # 2026-08-28 — Phase 6B.155 (PLAN §11.78). Cap on
                # failure-driven reconnects. After N consecutive failures
                # we stop retrying here and let the scheduled_reconnect
                # watchdog handle the next attempt (every hour by
                # default). The watchdog closes _container from its own
                # thread; the demux loop raises, falls through to this
                # except block, and re-attempts — either succeeding and
                # resetting the cap, or hitting the cap again (which is
                # a no-op since cap_exhausted is already True).
                if self._max_reconnect_attempts > 0 and not cap_exhausted:
                    remaining = (
                        self._max_reconnect_attempts - self._consecutive_errors
                    )
                    if remaining <= 0:
                        log.error(
                            "consecutive_reconnect_cap_reached: "
                            "PersistentRTSP decode iteration failed "
                            "(%d consecutive attempts). Last error: %s. "
                            "Reader is UNHEALTHY — waiting %.0fs for the "
                            "next in-thread periodic teardown "
                            "(US-049c).",
                            self._consecutive_errors,
                            e,
                            self._scheduled_reconnect_seconds,
                        )
                        self.reconnects_total += 1
                        cap_exhausted = True
                        # Sleep until either stop_event OR the next
                        # in-thread periodic teardown (US-049c) reopens
                        # the container. The teardown happens inside
                        # _decode_iteration and will either succeed
                        # (recovering) or hit this cap-block again.
                        self._stop_event.wait(
                            self._scheduled_reconnect_seconds,
                        )
                        if self._stop_event.is_set():
                            break
                        # Outer while-loop retries the decode. If it
                        # succeeds, the early "if cap_exhausted" block
                        # above logs recovery. If it fails again, we
                        # hit this except block again — but since
                        # cap_exhausted=True, the inner if is skipped
                        # and we just fall through to the warning (no
                        # behavioral change vs. retry-forever, but
                        # without the 30s hammering — the cadence
                        # waits on _stop_event for the next teardown).
                        continue

                # Standard failure-driven warning (used when cap not yet
                # exhausted, or when cap is disabled via
                # max_reconnect_attempts<=0).
                if self._max_reconnect_attempts > 0 and not cap_exhausted:
                    remaining = (
                        self._max_reconnect_attempts - self._consecutive_errors
                    )
                    log.warning(
                        "Decode failed (attempt %d): %s. Reconnecting "
                        "in %.1fs... (%d attempt(s) remaining before "
                        "deferring to watchdog)",
                        self._consecutive_errors,
                        e,
                        backoff,
                        remaining,
                    )
                else:
                    # Cap disabled (max_reconnect_attempts<=0): retry
                    # forever, no remaining-count to report.
                    log.warning(
                        "Decode failed (attempt %d): %s. Reconnecting "
                        "in %.1fs...",
                        self._consecutive_errors,
                        e,
                        backoff,
                    )
                time.sleep(backoff)
                backoff = min(backoff * RECONNECT_BACKOFF_MULT, RECONNECT_BACKOFF_MAX)
                self.reconnects_total += 1

    def _decode_iteration(self) -> None:
        # US-049c: periodic in-thread teardown (Frigate/go2rtc pattern).
        # Closes + reopens the av container every TEARDOWN_INTERVAL_SECONDS
        # so PyAV's demux can't wedge silently in C-land.
        if (
            self._last_teardown_monotonic is not None
            and (
                time.monotonic() - self._last_teardown_monotonic
            )
            >= TEARDOWN_INTERVAL_SECONDS
        ):
            log.info("in_thread_teardown: starting")
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:
                    log.exception("in_thread_teardown: container.close() failed")
            self._container = av.open(self._rtsp_url, options=self._ffmpeg_flags)
            self._last_teardown_monotonic = time.monotonic()
            self._teardowns_total += 1
            log.info("in_thread_teardown: completed")
        # Normal connect (or post-teardown reuse of self._container).
        if self._container is None:
            self._container = av.open(self._rtsp_url, options=self._ffmpeg_flags)
        # Probe cv2.VideoCapture for live stream geometry and write into
        # infra.camera_creds on each successful RTSP open.
        self._subscribe_geometry()
        stream = self._container.streams.video[0]
        self._consecutive_errors = 0
        self._healthy = True
        # Hold _container_lock during demux() so stop() can safely
        # drop the reference without racing a native C demux call.
        with self._container_lock:
            for packet in self._container.demux(stream):
                if self._stop_event.is_set():
                    break
                if packet.dts is None:
                    continue
                for frame in packet.decode():
                    if self._stop_event.is_set():
                        break
                    if frame is None:
                        continue
                    try:
                        img = frame.to_image()
                    except Exception:
                        log.exception(
                            "_decode_iteration: frame.to_image() failed, "
                            "discarding this frame"
                        )
                        raise
                    with self._ring_lock:
                        self._ring.append(img)
                    self.frames_decoded_total += 1
                    self._last_frame_time = time.monotonic()
        # Close under lock so stop()'s lock-guarded close cannot race.
        # When stop() times out on join and calls close() inside _container_lock,
        # the demux thread's post-loop close must also acquire the lock,
        # ensuring serialized access to the container and preventing a
        # half-closed RTSP socket leak.
        with self._container_lock:
            if self._container is not None:
                try:
                    self._container.close()
                except Exception:
                    log.exception("_decode_iteration: container.close() failed")
                    raise
                self._container = None
        self._healthy = False


class CameraCaptureRegistry:
    """Singleton: boots one reader per camera, manages lifecycle and reconnects."""

    _instance: CameraCaptureRegistry | None = None
    _init_lock = threading.Lock()
    _lock = threading.Lock()
    _readers: dict[str, PersistentRTSPReader]
    _reconnect_thread: threading.Thread | None = None
    _reconnect_stop = threading.Event()

    def __new__(cls) -> CameraCaptureRegistry:  # noqa: PYI034
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._readers = {}
                    cls._instance = inst
        return cls._instance

    @classmethod
    def get(cls, camera_id: str) -> PersistentRTSPReader:
        """Return reader for *camera_id*, starting it lazily if needed.

        Raises KeyError if the camera is not configured or has no rtsp_url.
        """
        inst = cls._instance
        if inst is None:
            raise KeyError(f"CameraCaptureRegistry not initialized: {camera_id}")
        with inst._lock:
            if camera_id in inst._readers:
                return inst._readers[camera_id]
        import infra.camera_creds as _camera_creds

        cam = _camera_creds.get_camera(camera_id)
        if cam is None:
            raise KeyError(f"Camera not configured: {camera_id}")
        rtsp_url = cam["rtsp_url"]
        if not rtsp_url:
            raise KeyError(f"Camera {camera_id} has no rtsp_url")
        reader = PersistentRTSPReader(rtsp_url, camera_id=camera_id)
        reader.start()
        with inst._lock:
            inst._readers[camera_id] = reader
        return reader

    @classmethod
    def start_all(cls) -> None:
        """Boot one reader per configured camera, concurrently."""
        import infra.camera_creds as _camera_creds

        cameras = _camera_creds.get_all_cameras()
        if not cameras:
            return

        inst = cls._instance
        if inst is None:
            cls._instance = inst = CameraCaptureRegistry()

        threads: list[threading.Thread] = []
        for camera_id, cam_info in cameras.items():
            rtsp_url = cam_info["rtsp_url"]
            # Use the uppercase prefix as the registry key so it matches the
            # camera_id that the /alert pipeline resolves via IP fallback.
            # Without this, get_recent_frames("OUTSIDE_FRONT_SOLAR") misses the
            # boot reader keyed by friendly name and the lazy get() fallback
            # would create a second reader under the prefix key.
            registry_key = cam_info.get("prefix", camera_id)
            t = threading.Thread(
                target=cls._boot_one,
                args=(registry_key, rtsp_url),
                name=f"boot[{registry_key}]",
                daemon=True,
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        cls._ensure_reconnect_running()

    @classmethod
    def _boot_one(cls, camera_id: str, rtsp_url: str) -> None:
        if not rtsp_url:
            return
        reader = PersistentRTSPReader(rtsp_url, camera_id=camera_id)
        reader.start()
        inst = cls._instance
        if inst is not None:
            with inst._lock:
                inst._readers[camera_id] = reader

    @classmethod
    def stop_all(cls) -> None:
        """Stop all readers and drain background threads."""
        inst = cls._instance
        if inst is None:
            return
        inst._reconnect_stop.set()
        with cls._init_lock:
            if inst._reconnect_thread is not None:
                inst._reconnect_thread.join(timeout=5.0)
                inst._reconnect_thread = None
        with inst._lock:
            for reader in inst._readers.values():
                reader.stop(timeout=3.0)
            inst._readers.clear()

    @classmethod
    def stats_all(cls) -> dict[str, dict]:
        """Return {camera_id: stats_dict} for all registered readers."""
        inst = cls._instance
        if inst is None:
            return {}
        with inst._lock:
            return {cid: r.stats() for cid, r in inst._readers.items()}

    @classmethod
    def is_healthy_all(cls) -> dict[str, bool]:
        """Return {camera_id: is_healthy} for all registered readers."""
        inst = cls._instance
        if inst is None:
            return {}
        stale_sec = _resolve_stale_seconds()
        with inst._lock:
            return {cid: r.is_healthy(stale_sec) for cid, r in inst._readers.items()}

    @classmethod
    def _reconnect_loop(cls) -> None:
        """Monitor readers; restart ones unhealthy past the env-tuned thresholds.

        Single-reader-per-cam invariant: exactly one PersistentRTSPReader owns
        each camera at any time.  When a reader becomes unhealthy the reconnect
        loop swaps it out.  The swap lifecycle:

        1. The dead reader's decode thread is stopped and its PyAV container is
           closed **under _container_lock** so no demux thread can race.
        2. If the thread is still alive after the initial join, the loop forces
           the container closed again (with a second join) to prevent the cam-
           side TCP socket from lingering in ESTABLISHED state.
        3. reconnects_total is incremented so every swap is counted.
        4. A new reader is created, started, and registered in _readers.

        US-098: uses _resolve_stale_seconds() and
        _resolve_reconnect_min_uptime() (env-tunable, defaults 20s / 30s)
        instead of the legacy hardcoded 5s / 10s.
        """
        inst = cls._instance
        if inst is None:
            return
        stale_sec = _resolve_stale_seconds()
        min_uptime = _resolve_reconnect_min_uptime()
        while not inst._reconnect_stop.is_set():
            inst = cls._instance
            if inst is None:
                break
            dead: list[tuple[str, PersistentRTSPReader]] = []
            with inst._lock:
                for cid, reader in inst._readers.items():
                    if not reader.is_healthy(stale_sec):
                        dead.append((cid, reader))
            for cid, dead_reader in dead:
                if dead_reader.uptime_seconds() > min_uptime:
                    reader_id = f"{dead_reader._rtsp_url.split('@')[-1]}"
                    dead_uptime = dead_reader.uptime_seconds()
                    retry_count = dead_reader._consecutive_errors
                    log.warning(
                        "Reader swap: %s (id=%s, uptime=%.0fs, "
                        "consecutive_errors=%d); reconnecting.",
                        cid, reader_id, dead_uptime, retry_count,
                    )
                    # AC1: Close the old reader fully before opening a new one.
                    # stop() joins the thread (12s timeout) then closes the
                    # container under _container_lock.  If the demux thread
                    # is still stuck after 12s, we force-close a second time
                    # to drain any lingering ESTABLISHED sockets on the cam
                    # side — otherwise the socket persists ~60s past close().
                    dead_reader.stop(timeout=12.0)
                    # Force a second close if the thread is still alive.
                    with dead_reader._container_lock:
                        if dead_reader._container is not None:
                            try:
                                dead_reader._container.close()
                            except Exception:
                                pass
                            dead_reader._container = None
                    inst._readers.pop(cid, None)
                    new_reader = PersistentRTSPReader(
                        dead_reader._rtsp_url, camera_id=cid
                    )
                    new_reader.start()
                    # AC2: Count every _reconnect_loop swap.
                    # Carry over the dead reader's total and add 1 for this swap.
                    new_reader.reconnects_total = dead_reader.reconnects_total + 1
                    with inst._lock:
                        inst._readers[cid] = new_reader
            inst._reconnect_stop.wait(timeout=5.0)

    @classmethod
    def _ensure_reconnect_running(cls) -> None:
        inst = cls._instance
        if inst is None:
            return
        if inst._reconnect_thread is not None and inst._reconnect_thread.is_alive():
            return
        inst._reconnect_stop.clear()
        t = threading.Thread(target=cls._reconnect_loop, name="reconnect-loop", daemon=True)
        t.start()
        inst._reconnect_thread = t

    @classmethod
    def clear(cls) -> None:
        """Stop all readers and release the singleton (for tests)."""
        cls.stop_all()
        with cls._init_lock:
            if cls._instance is not None:
                cls._instance._readers.clear()
                cls._instance._reconnect_thread = None
                cls._instance._reconnect_stop.clear()
                cls._instance = None


def _get_healthy_reader(camera_id: str):
    try:
        reader = CameraCaptureRegistry.get(camera_id)
    except KeyError:
        log.warning("No reader for camera %s; returning empty.", camera_id)
        return None
    # US-098: use env-tuned stale threshold (default 20s) instead of 5s.
    if not reader.is_healthy(_resolve_stale_seconds()):
        log.warning("Reader for %s is not healthy.", camera_id)
        return None
    return reader


def get_recent_frames(
    camera_id: str,
    n: int = 4,
    output_dir: str | None = None,
) -> list[str]:
    """Get the *n* most recent frames for *camera_id*.

    When *output_dir* is provided, *n* PNGs land in ``output_dir/frame_NNN.png``
    (per-alert directory).  When *output_dir* is *None*, defaults to the
    legacy per-camera ``FRAMES_DIR/<camera_id>`` path (preserved for
    backward compatibility).
    """
    reader = _get_healthy_reader(camera_id)
    if reader is None:
        return []
    from infra.paths import FRAMES_DIR

    dir_to_use = output_dir if output_dir is not None else os.path.join(FRAMES_DIR, camera_id)
    return reader.get_recent_frames(n, dir_to_use)


def get_frames_by_offset(
    camera_id: str,
    indices: list[int],
) -> list[str]:
    """Pull frames at specific deque indices from *camera_id*'s ring buffer."""
    reader = _get_healthy_reader(camera_id)
    if reader is None:
        return []
    from infra.paths import FRAMES_DIR

    return reader.get_frames_by_offset(
        indices, os.path.join(FRAMES_DIR, camera_id)
    )
