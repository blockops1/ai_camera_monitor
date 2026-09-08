"""
frame_capture.py — Persistent RTSP reader with in-memory ring buffer.

STATUS: stable
THREAD SAFETY: uses threading.Lock
INPUTS:
    - rtsp_url: str (required)
    - ring_size: int (optional, default 180)
    - ffmpeg_flags: dict | None (optional)

OUTPUTS:
    - list[str]: JPEG file paths from get_recent_frames
PUBLIC API:
    start() — Open RTSP and start decode loop (idempotent).
    stop(timeout=5.0) — Stop decode loop and close socket (idempotent).
    get_recent_frames(n, output_dir, max_size=None) -> list[str]
    is_healthy(stale_seconds=5.0) -> bool
    uptime_seconds() -> float
    get_frames_by_offset(indices, output_dir, max_size=None) -> list[str]
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
RECONNECT_BACKOFF_MAX = 30.0
RECONNECT_BACKOFF_MULT = 2.0


class PersistentRTSPReader:
    """Long-lived RTSP reader: background thread decodes into a deque ring."""

    def __init__(
        self,
        rtsp_url: str,
        ring_size: int = RING_SIZE_DEFAULT,
        ffmpeg_flags: dict | None = None,
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
        self._container: av.container.InputContainer | None = None
        self._thread: threading.Thread | None = None
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

    def stop(self, timeout: float = 5.0) -> None:
        if not self.is_running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._container is not None:
            try:
                self._container.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self._container = None
        self._healthy = False

    def get_recent_frames(
        self,
        n: int,
        output_dir: str,
        max_size: tuple[int, int] | None = None,
    ) -> list[str]:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        self._clean_old(output_dir)
        with self._ring_lock:
            frames = list(self._ring)[-n:]
        if not frames:
            return []
        return self._save_frames(frames, output_dir, max_size, start=1)

    def get_frames_by_offset(
        self,
        indices: list[int],
        output_dir: str,
        max_size: tuple[int, int] | None = None,
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
                ring_list[idx], output_dir, max_size, len(out_paths) + 1, out_paths
            )
        return out_paths

    # -- internal helpers --

    def _clean_old(self, output_dir: str) -> None:
        for old in Path(output_dir).glob("frame_*.jpg"):
            try:
                old.unlink()
            except OSError:
                pass

    def _save_frames(
        self,
        frames: list[Image.Image],
        output_dir: str,
        max_size: tuple[int, int] | None,
        start: int = 1,
    ) -> list[str]:
        out_paths: list[str] = []
        for i, frame in enumerate(frames, start=start):
            self._save_one(frame, output_dir, max_size, i, out_paths)
        return out_paths

    def _save_one(
        self,
        img: Image.Image,
        output_dir: str,
        max_size: tuple[int, int] | None,
        n: int,
        out_paths: list[str],
    ) -> None:
        out_path = os.path.join(output_dir, f"frame_{n:03d}.jpg")
        if max_size is not None:
            img = img.copy()
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
        img.save(out_path, quality=85)
        out_paths.append(out_path)

    def _run_loop(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                self._decode_iteration()
                break
            except Exception as e:  # noqa: BLE001
                self._consecutive_errors += 1
                self._healthy = False
                if self._stop_event.is_set():
                    break
                log.warning(
                    "Decode failed (attempt %d): %s. Reconnecting in %.1fs...",
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
                except Exception:  # noqa: BLE001, S112
                    continue
                with self._ring_lock:
                    self._ring.append(img)
                self.frames_decoded_total += 1
                self._last_frame_time = time.monotonic()
        try:
            container.close()
        except Exception:  # noqa: BLE001, S110
            pass
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
    def get(cls, camera_id: str) -> PersistentRTSPReader | None:
        """Return reader for *camera_id*, starting it lazily if needed."""
        inst = cls._instance
        if inst is None:
            return None
        with inst._lock:
            if camera_id in inst._readers:
                return inst._readers[camera_id]
        import infra.camera_creds as _camera_creds

        cam = _camera_creds.get_camera(camera_id)
        if cam is None:
            return None
        rtsp_url = cam.get("rtsp_url", "")
        if not rtsp_url:
            return None
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
            rtsp_url = cam_info.get("rtsp_url", "")
            # Use the uppercase prefix as the registry key so it matches the
            # camera_id that the /alert pipeline resolves via IP fallback.
            # Without this, get_recent_frames("OUTSIDE_FRONT_SOLAR") misses the
            # boot reader keyed by friendly name and the lazy get() fallback
            # would create a second reader under the prefix key.
            registry_key = cam_info.get("prefix") or camera_id
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
    reader = CameraCaptureRegistry.get(camera_id)
    if reader is None:
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
    max_size: tuple[int, int] | None = None,
) -> list[str]:
    """Get recent frames for *camera_id*, aged within *offset_seconds*."""
    reader = _get_healthy_reader(camera_id)
    if reader is None:
        return []
    from infra.paths import FRAMES_DIR

    frame_paths = reader.get_recent_frames(
        n, os.path.join(FRAMES_DIR, camera_id), max_size=max_size
    )
    now = time.time()
    cutoff = now - offset_seconds
    aged: list[str] = []
    for p in frame_paths:
        try:
            if os.path.getmtime(p) >= cutoff:
                aged.append(p)
        except OSError:
            continue
    return aged


def get_frames_by_offset(
    camera_id: str,
    indices: list[int],
    max_size: tuple[int, int] | None = None,
) -> list[str]:
    """Pull frames at specific deque indices from *camera_id*'s ring buffer."""
    reader = _get_healthy_reader(camera_id)
    if reader is None:
        return []
    from infra.paths import FRAMES_DIR

    return reader.get_frames_by_offset(
        indices, os.path.join(FRAMES_DIR, camera_id), max_size=max_size
    )
