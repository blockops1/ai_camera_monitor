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
    is_healthy(stale_seconds=5.0) -> bool
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

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import av
from PIL import Image

log = logging.getLogger("frame_capture")

RING_SIZE_DEFAULT = 180
RECONNECT_BACKOFF_INITIAL = 1.0   # seconds
RECONNECT_BACKOFF_MAX = 30.0
RECONNECT_BACKOFF_MULT = 2.0

# US-017e — Phase 6B.155 (PLAN §11.78). Cap on consecutive failure-driven
# reconnects. After N attempts the failure loop defers to the proactive
# scheduled_reconnect_watchdog (still fires hourly). Prevents log/CPU
# starvation during a camera-side Reolink stickiness event (where RTSP
# returns ERRNO 60 / Invalid data / 404 in a burst, but the camera IS still
# on the network — just temporarily refusing new sessions). Without this cap
# the loop will keep hammering the camera at the 30s max forever, filling
# logs and (worse) potentially blocking other code paths. With it: log the
# failure once at ERROR, let the hourly watchdog take over.
RECONNECT_MAX_ATTEMPTS_DEFAULT = 10

# Proactive watchdog cadence (Phase 6B.80 / PLAN §11.13). The v2 watchdog
# closes + respawns the decode thread every `scheduled_reconnect_seconds`
# so PyAV's container.demux() can't wedge silently in C-land (observed
# on Reolink CAM5 after 41h uptime: frames_decoded_total frozen, zero
# exceptions surfaced). Defense in depth — the existing _reconnect_loop
# handles failure-driven recoveries; the watchdog handles the "stuck
# without raising" failure mode.
SCHEDULED_RECONNECT_DEFAULT = 3600.0  # 1 hour
_SCHEDULED_RECONNECT_ENV = "FARMSV_RTSP_RECONNECT_SECONDS"
_MAX_RECONNECT_ATTEMPTS_ENV = "FARMSV_RTSP_MAX_RETRIES"

# Sentinel: signals that no explicit arg was passed (caller wants env/default).
# Replaces `float | None` / `int | None` in resolver signatures — the None
# branch would silently pick up env vars, creating two data sources for the
# same value.
_UNSET = object()


def _resolve_scheduled_reconnect_seconds(arg_value: float | object = _UNSET) -> float:
    """Resolve the scheduled-reconnect cadence. Precedence:
    1. Explicit constructor arg (if not _UNSET)
    2. Env var FARMSV_RTSP_RECONNECT_SECONDS (if set + non-empty)
    3. SCHEDULED_RECONNECT_DEFAULT (3600s)

    Returns a float (seconds). Raises ValueError if env var is set
    but not a valid float — same shape as the existing ring-size
    resolver pattern.
    """
    if arg_value is not _UNSET:
        return float(arg_value)
    env_val = os.environ.get(_SCHEDULED_RECONNECT_ENV)
    if env_val:
        return float(env_val)
    return SCHEDULED_RECONNECT_DEFAULT


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
        scheduled_reconnect_seconds: float = _UNSET,  # type: ignore[assignment]
        max_reconnect_attempts: int = _UNSET,  # type: ignore[assignment]
    ) -> None:
        self._rtsp_url = rtsp_url
        self._ring_size = ring_size
        self._ring: deque[Image.Image] = deque(maxlen=ring_size)
        self._ring_lock = threading.Lock()
        self._ffmpeg_flags = ffmpeg_flags or {
            "rtsp_transport": "tcp",
            "timeout": "10000000",
            "fflags": "+genpts",
            "buffer_size": "20000000",
        }
        self._scheduled_reconnect_seconds = _resolve_scheduled_reconnect_seconds(
            scheduled_reconnect_seconds  # type: ignore[arg-type]
        )
        self._max_reconnect_attempts = _resolve_max_reconnect_attempts(
            max_reconnect_attempts  # type: ignore[arg-type]
        )
        self._container: av.container.InputContainer | None = None
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._start_time: float | None = None
        self._last_frame_time: float | None = None
        self._consecutive_errors = 0
        self._healthy = False
        self.frames_decoded_total = 0
        self.reconnects_total = 0

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
            "container_open": self._container is not None,
            "is_running": self.is_running,
        }

    def is_healthy(self, stale_seconds: float = 5.0) -> bool:
        if not self._healthy or self._last_frame_time is None:
            return False
        return (time.monotonic() - self._last_frame_time) < stale_seconds

    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._start_time = time.monotonic()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"RTSP[{self._rtsp_url.split('@')[-1]}]",
            daemon=True,
        )
        self._thread.start()
        # Proactive watchdog (PLAN §11.13). Closes the av container every
        # `scheduled_reconnect_seconds` so PyAV's C demux() can't wedge
        # silently. Idempotent with respect to the failure-driven reconnect
        # in _run_loop — both paths target the same _container slot.
        self._watchdog_thread = threading.Thread(
            target=self._scheduled_reconnect_watchdog,
            name=f"RTSPWatchdog[{self._rtsp_url.split('@')[-1]}]",
            daemon=True,
        )
        self._watchdog_thread.start()
        log.info(
            "PersistentRTSPReader started for %s "
            "(scheduled_reconnect_seconds=%.0f)",
            self._rtsp_url.split("@")[-1],
            self._scheduled_reconnect_seconds,
        )

    def stop(self, timeout: float = 5.0) -> None:
        if not self.is_running and self._watchdog_thread is None:
            return
        self._stop_event.set()
        # Watchdog sleeps on _stop_event.wait() — it'll exit promptly.
        # Join with a short timeout for cleanliness (it just breaks the
        # sleep loop, no I/O to drain).
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=timeout)
            self._watchdog_thread = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
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

    def _scheduled_reconnect_watchdog(self) -> None:
        """PLAN §11.13. Proactive cycle of stop-then-restart on the decode
        thread every `scheduled_reconnect_seconds`. Replaces the container-
        close approach (§11.13.2) after the v1-refactor probe showed that
        closing av.container from outside the decode loop can segfault
        PyAV's C demux() loop.

        Cycle (each fire):
          1. Set stop_event so the decode thread breaks out of
             `_decode_iteration`'s packet loop on the next packet. In
             practice the demux() loop is blocked on libav so the thread
             does NOT exit until libav times out or yields — see
             "failure modes" below.
          2. Join the decode thread with a 10 s timeout. If alive, the
             thread is stuck in C-land demux() (zombie state). Log a
             warning and proceed anyway.
          3. Close the container (now safe — decode thread is gone or
             stuck in C). Set _container = None.
          4. Spin up a fresh decode thread via the same code path
             `.start()` uses.

        Failure modes:
        - If PyAV's C demux() doesn't yield on stop_event (the zombie
          scenario we're trying to break out of): the decode thread may
          stay stuck past the 10 s join timeout. We log
          `decode_thread_did_not_exit` and proceed. In the worst case,
          the new decode thread spins up alongside the stuck one —
          mitigated by the next watchdog fire 1 h later, which would
          re-attempt. The `is_healthy()` staleness check still trips
          within ~30 s of frames stopping, so alerts fail-loud as
          expected.
        - If container.close() raises (socket already torn down):
          caught at debug; _container set to None regardless.

        Trade-off vs. the original §11.13 design: stop+start preserves
        the ring buffer (frames captured up to the moment of stop are
        still in self._ring), but briefly pauses frame ingest for ~1-2 s
        during the close+reopen window.
        """
        while not self._stop_event.is_set():
            # Sleep on stop_event until the next deadline.
            self._stop_event.wait(timeout=self._scheduled_reconnect_seconds)
            if self._stop_event.is_set():
                break
            self._scheduled_reconnect_fire()
        # On exit (stop_event set), we don't fire — .stop() handles the
        # rest of the lifecycle.

    def _scheduled_reconnect_fire(self) -> None:
        """One fire of the proactive reconnect cycle. Called by the
        watchdog after the cadence elapses. Stops the decode thread
        (gracefully via stop_event + bounded join), closes the container,
        then spawns a fresh decode thread.
        """
        log.info(
            "scheduled_reconnect_fire: starting (uptime=%.0fs, "
            "frames_decoded=%d, reconnects_total=%d)",
            self.uptime_seconds(),
            self.frames_decoded_total,
            self.reconnects_total,
        )
        # Signal the decode thread to break out on its next packet. The
        # decode loop checks self._stop_event.is_set() on every packet,
        # but the demux() generator blocks in C waiting for a packet —
        # so this signal alone may not free the thread.
        self._stop_event.set()
        # Join the decode thread. If it's stuck in demux() (the very
        # zombie state we're trying to recover from), the join will time
        # out — proceed anyway.
        old_thread = self._thread
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=10.0)
            if old_thread.is_alive():
                log.warning(
                    "scheduled_reconnect_fire: decode thread did not exit "
                    "within 10s (stuck in container.demux()). "
                    "Proceeding with reconnect anyway."
                )
        # Now safe to close the container — decode thread is gone or
        # stuck in C-land and we can't reach it from here.
        if self._container is not None:
            try:
                self._container.close()
            except Exception as e:  # noqa: BLE001
                log.debug(
                    "scheduled_reconnect_fire: container.close() "
                    "raised (ignored): %s",
                    e,
                )
            self._container = None
        # Reset the stop_event and start a fresh decode thread.
        # self._thread was set to the old (now-exited) Thread object;
        # .start() will overwrite it with a new Thread on the next call.
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"RTSP[{self._rtsp_url.split('@')[-1]}]",
            daemon=True,
        )
        self._thread.start()
        self.reconnects_total += 1
        log.info(
            "scheduled_reconnect_fire: completed (reconnects_total=%d)",
            self.reconnects_total,
        )

    def _run_loop(self) -> None:
        """Main decode loop. Runs in a background thread.

        Phase 6B.155 (PLAN §11.78): failure-driven reconnects are capped at
        `self._max_reconnect_attempts`. After the cap, the loop defers to
        the proactive scheduled_reconnect_watchdog (still fires every
        hour). The watchdog closes _container from a separate thread; the
        resulting demux exception in this thread flows back through the
        outer try/except, re-attempts the connection, and either recovers
        (logging a recovery line + resetting the cap) or hits the cap again.

        Per the 2026-08-28 CAM3 incident (6 errors in 80s with ERRNO 60 /
        Invalid data / 404 — Reolink RTSP session stickiness), this caps
        log spam and CPU usage during long camera outages while keeping the
        scheduled watchdog as the long-term recovery mechanism.
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
                            "Deferring to scheduled_reconnect_watchdog "
                            "(%.0fs cadence). Reader is UNHEALTHY until "
                            "next reconnect.",
                            self._consecutive_errors,
                            e,
                            self._scheduled_reconnect_seconds,
                        )
                        self.reconnects_total += 1
                        cap_exhausted = True
                        # Sleep until either stop_event or the watchdog
                        # closes the container (which raises in the demux
                        # loop and we fall through here again). Both paths
                        # exit the inner sleep cleanly.
                        _sleep_until_stop_or_watchdog(
                            self._stop_event,
                            scheduled_reconnect_seconds=(
                                self._scheduled_reconnect_seconds
                            ),
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
                        # without the 30s hammering — the watchdog
                        # controls cadence now).
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
        container = av.open(self._rtsp_url, options=self._ffmpeg_flags)
        self._container = container
        stream = container.streams.video[0]
        self._consecutive_errors = 0
        self._healthy = True
        for packet in container.demux(stream):
            if self._stop_event.is_set() or packet.dts is None:
                continue
            for frame in packet.decode():
                if self._stop_event.is_set() or frame is None:
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
        try:
            container.close()
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
        reader = PersistentRTSPReader(rtsp_url)
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
            registry_key = cam_info["prefix"] if "prefix" in cam_info else camera_id
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
        reader = PersistentRTSPReader(rtsp_url)
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
        with inst._lock:
            return {cid: r.is_healthy() for cid, r in inst._readers.items()}

    @classmethod
    def _reconnect_loop(cls) -> None:
        """Monitor readers; restart ones unhealthy for 10s+."""
        inst = cls._instance
        if inst is None:
            return
        while not inst._reconnect_stop.is_set():
            inst = cls._instance
            if inst is None:
                break
            dead: list[tuple[str, PersistentRTSPReader]] = []
            with inst._lock:
                for cid, reader in inst._readers.items():
                    if not reader.is_healthy():
                        dead.append((cid, reader))
            for cid, dead_reader in dead:
                if dead_reader.uptime_seconds() > 10.0:
                    log.warning("Reader %s unhealthy; reconnecting.", cid)
                    dead_reader.stop(timeout=2.0)
                    inst._readers.pop(cid, None)
                    new_reader = PersistentRTSPReader(dead_reader._rtsp_url)
                    new_reader.start()
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
    if not reader.is_healthy():
        log.warning("Reader for %s is not healthy.", camera_id)
        return None
    return reader


def get_recent_frames(
    camera_id: str,
    n: int = 4,
    offset_seconds: int = 6,
) -> list[str]:
    """Get recent frames for *camera_id*, aged within *offset_seconds*."""
    reader = _get_healthy_reader(camera_id)
    if reader is None:
        return []
    from infra.paths import FRAMES_DIR

    frame_paths = reader.get_recent_frames(
        n, os.path.join(FRAMES_DIR, camera_id)
    )
    now = time.time()
    cutoff = now - offset_seconds
    aged: list[str] = []
    for p in frame_paths:
        try:
            if os.path.getmtime(p) >= cutoff:
                aged.append(p)
        except OSError:
            log.exception("get_recent_frames: stat failed for %s", p)
            continue
    return aged


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
