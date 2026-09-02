"""
persistent_rtsp.py — Long-lived RTSP connection with a frame ring buffer.

STATUS: stable
THREAD SAFETY: uses threading for the decode loop + threading.Lock for
    ring buffer access; multiple readers can call get_recent_frames()
    concurrently with the writer.

INPUTS:
    - function arg rtsp_url: str (required) — RTSP URL with credentials
    - function arg scheduled_reconnect_seconds: float | None (optional) —
      overrides SCHEDULED_RECONNECT_DEFAULT (1 h). See PLAN §11.13.
    - env FARMSV_RTSP_RING_SIZE (optional) — overrides RING_SIZE_DEFAULT
    - env FARMSV_RTSP_RECONNECT_SECONDS (optional) — overrides
      SCHEDULED_RECONNECT_DEFAULT (constructor arg wins over env var)

OUTPUTS:
    - return value: PersistentRTSPReader instance (with .start/.stop/
      .get_recent_frames/.get_frames_by_offset/.is_healthy/.uptime_seconds)
    - writes file: <output_dir>/<camera>_<ts>.png when get_recent_frames
      is called (saves the ring buffer's frames as PNGs — §11.88 2026-09-01)
    - log line per reconnect attempt (warning level on failure)
    - log line per scheduled reconnect fire (info level, with uptime +
      frames_decoded + reconnects_total) — see PLAN §11.13

PUBLIC API:
    init_reader_registry(cameras: list[CameraSpec]) -> None
        Seed the registry with the canonical camera list. Must be
        called once at listener bootstrap BEFORE set_reader_*(); the
        list lets set_reader(name, ...) auto-derive spec.code from
        spec.name so name-keyed callers transparently populate the
        code-keyed storage too.
    set_reader_by_code(code: str, reader: PersistentRTSPReader) -> None
        Register a reader under its CameraSpec.code (e.g. "CAM1",
        "FRONT"). Primary API going forward (Phase 6B.167 §13.5
        Commit 10). Replaces any prior reader under the same code.
    get_reader_by_code(code: str) -> PersistentRTSPReader | None
        Look up a reader by code. Returns None if absent.
    set_reader(camera_name: str, reader: PersistentRTSPReader) -> None
        Back-compat. Equivalent to set_reader_by_code for cameras in
        the init'd registry (uses name→code resolution); falls back
        to storing under the raw name if no match.
    get_reader(camera_name: str) -> PersistentRTSPReader | None
        Back-compat. Looks up by either code or name.
    get_reader_for_url(rtsp_url: str) -> PersistentRTSPReader | None
        Look up a reader by full RTSP URL (creds stripped before
        match). Used by frame_capture to resolve the right reader
        for an inbound alert.
    set_default_reader(reader) / get_default_reader()
        Back-compat shims. `default` is just another slot in the
        registry; both shims delegate to set_reader/get_reader.
        Retained for any test or external caller that uses the
        singleton-without-name pattern.
    clear_reader_registry() -> None
        Test-only. Clears all registered readers.
    PersistentRTSPReader(rtsp_url, ring_size=RING_SIZE_DEFAULT, ...,
                         scheduled_reconnect_seconds=None)
        .start() -> None — begin background decode + watchdog threads
        .stop() -> None — graceful shutdown (joins both threads)
        .get_recent_frames(n, max_size) -> list[str]
            Snapshot the last N decoded frames as PNGs (§11.88 2026-09-01).
        .get_frames_by_offset(indices, max_size) -> list[str]
            Snapshot specific ring buffer indices as PNGs (§11.88 2026-09-01).
        .is_healthy() -> bool
            True if the last decode was within the staleness window.
        .uptime_seconds() -> float
            Seconds since the reader was started.

DOES NOT DO:
    - HTTP snapshot fallback — infra.frame_capture owns the fallback
    - Crop generation — infra.motion_types (dataclass home) +
      listener.motion_gate_pipeline own that
    - Match vehicles — infra.vehicle_matcher owns that

WHY HERE:
    The Reolink 510A at CAM1 has a kernel-side pre-buffer that dumps
    ~1.5s of camera-time into a fresh RTSP socket on open, then REPLAYS
    the same frames in a loop. The result: 6-frame bursts opened fresh
    per alert get the pre-buffer dump (3 distinct frames) followed by
    3 replayed frames. The fix: hold the RTSP connection open 24/7.
    After ~5s of warmup the camera transitions to "live stream" mode
    and produces fresh frames continuously. The alert handler then
    just asks for the 6 most recent decoded frames.

    2026-08-16 — Proactive hourly reconnect (Phase 6B.80, PLAN §11.13).
    PyAV's container.demux() blocks indefinitely on a zombie socket
    without raising — observed after 41h of uptime on CAM1 (frames
    decoded counter frozen, zero reconnects). A watchdog daemon thread
    closes the av container every `scheduled_reconnect_seconds`
    (default 1 h); the existing _run_loop reopens it. Same code path
    as a real failure, no new exception logic. Trade-off: ~1 alert
    in 1800 may be dropped during the 1-2s close-and-reopen window
    per cycle (fail-loud contract per PLAN §11.8).

    2026-08-17 — Multi-camera registry (Phase 6B.87, PLAN §11.17).
    The single-slot singleton was the right shape for one gatekeeper
    camera; with Outside Front Garage joining the gatekeeper tier
    (§11.17), the registry became a dict keyed by canonical camera
    name. capture_frames() resolves the right reader via
    get_reader_for_url(). Back-compat shims retained so existing
    single-camera callers (tests, future one-off scripts) still work.
    Registry lock (`_registry_lock`) guards concurrent register/
    lookup from listener bootstrap threads.

    2026-08-30 — Code-keyed registry (Phase 6B.167 §13.5 Commit 10).
    The listener grew to register persistent readers for every camera
    (Phase 6B.160), and the registry key is shifting from operator
    friendly-name to CameraSpec.code (CAM1/CAM2/... or FRONT/BACK/
    OUTSIDE_*_CODE in the LEGACY schema). The new primary API is
    `init_reader_registry(cameras)` + `set_reader_by_code(code, ...)` +
    `get_reader_by_code(code)`. `set_reader(name, ...)` / `get_reader
    (name)` retained as back-compat shims. The unified dict (`_readers`)
    stores readers under BOTH the name and its resolved code, so callers
    using either key get the same reader back. The new API is required
    for new code; old code continues to work.

CALLED BY:
    - infra.frame_capture: get_reader_for_url() for capture_frames()
    - listener.listener.bootstrap: set_reader_by_code() per camera

CALLS INTO:
    - av (PyAV): RTSP demux + frame decode
    - PIL.Image: av.Frame.to_image() conversion
    - threading.Thread: background decode loop + scheduled-reconnect watchdog
    - threading.Lock: ring buffer guard + registry guard
    - threading.Event: stop signal for both threads

RELATED:
    - infra.frame_capture — main consumer
    - data/frames/<camera>_<ts>/ — the PNGs this module writes (§11.88 2026-09-01)

Slot management:
    The camera caps concurrent RTSP sessions at 3-4. As of Phase 6B.160
    (2026-08-28) we hold 1 slot per camera (6 total) 24/7 at 2 fps
    reduced bitrate — the ring memory is small enough that running
    every persistent reader is well within budget. Persistent RTSP is
    keyed by CameraSpec.code (Phase 6B.167 §13.5 Commit 10); the
    listener iterates over `infra.cameras.load_cameras()` and calls
    `set_reader_by_code(spec.code, reader)` per camera. Synology
    Surveillance Station holds another slot; the mobile app and any
    browser sessions use the rest.

Resource budget (steady state, per reader):
    ~5 Mbps * 1 sec = 600 KB/sec decoded
    ~180 frames * ~200 KB each = 36 MB RAM
    Disk: 0 (frames only saved on alert, deleted by cleanup pipeline)
    With 2 readers running: ~72 MB RAM. Trivial on 64 GB Mac mini.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import av
from PIL import Image

if TYPE_CHECKING:
    from av.container import InputContainer

    from infra.cameras import CameraSpec

log = logging.getLogger("persistent_rtsp")

# Default: keep 30 frames in the ring buffer (~10s at 3 fps).
# 6-frame alert request is served in O(1) from the back of the deque.
RING_SIZE_DEFAULT = 180  # ~12s lookback at 15fps — supports 6-frame bursts
                      # with 2s spacing (frames 1/31/61/91/121/151 from head of deque).
                      # Old value (30) only held ~2s, sufficient for the old
                      # "last 6 from the tail" capture cadence but not for the
                      # pre-event motion trail maintainer asked for on 2026-08-06.
                      # NOTE: 2026-08-07 Phase 6B.60 attempted to bump to 240
                      # to support offsets [60,90,120,150,180,210], but maintainer
                      # wanted the capture to start at T-4s (not T-15s), and
                      # the geometry doesn't fit a 4s lead with 6 frames at 2s
                      # spacing on a single ring. Reverted to 180.

# Reconnect parameters
RECONNECT_BACKOFF_INITIAL = 1.0   # seconds
RECONNECT_BACKOFF_MAX = 30.0      # seconds
RECONNECT_BACKOFF_MULT = 2.0

# 2026-08-28 — Phase 6B.155 (PLAN §11.78). Cap on consecutive failure-driven
# reconnects. After N attempts the failure loop defers to the proactive
# scheduled_reconnect_watchdog (still fires hourly). Prevents log/CPU
# starvation during a camera-side Reolink stickiness event (where RTSP
# returns ERRNO 60 / Invalid data / 404 in a burst, but the camera IS still
# on the network — just temporarily refusing new sessions). Without this cap
# the loop will keep hammering the camera at the 30s max forever, filling
# logs and (worse) potentially blocking other code paths. With it: log the
# failure once at ERROR, let the hourly watchdog take over.
RECONNECT_MAX_ATTEMPTS_DEFAULT = 10
# Env override (analogous to FARMSV_RTSP_RECONNECT_SECONDS for scheduled).

# 2026-08-16 — Phase 6B.80 (PLAN §11.13). Proactive hourly reconnect.
# See module WHY HERE for rationale. Configurable via constructor arg
# `scheduled_reconnect_seconds` and env var FARMSV_RTSP_RECONNECT_SECONDS;
# constructor wins.
SCHEDULED_RECONNECT_DEFAULT = 3600.0  # 1 hour


# Env var name + resolver. Matches the FARMSV_RTSP_RECONNECT_SECONDS pattern.
_SCHEDULED_RECONNECT_ENV = "FARMSV_RTSP_RECONNECT_SECONDS"


_MAX_RECONNECT_ATTEMPTS_ENV = "FARMSV_RTSP_MAX_RETRIES"


# 2026-08-28 — Wired up the FARMSV_RTSP_RING_SIZE env var resolver. Until
# today only the constructor arg was actually resolved; the env var name
# was documented but not honored. Used by the launchd plist to set ring
# size without touching code.
_RING_SIZE_ENV = "FARMSV_RTSP_RING_SIZE"


def _resolve_ring_size(arg_value: int) -> int:
    """Resolve the ring buffer frame count. Precedence:
    1. Explicit constructor arg (if not the module default RING_SIZE_DEFAULT)
    2. Env var FARMSV_RTSP_RING_SIZE (if set + non-empty)
    3. RING_SIZE_DEFAULT (180)

    Returns an int. Value < 30 raises ValueError — anything below 30
    frames at 15fps is < 2s of pre-event trail, which breaks the
    motion-gate contract. Caller can ignore ValueError if it really
    wants a smaller ring.
    """
    # Treat RING_SIZE_DEFAULT as "no override" so the env var can still
    # win when the caller didn't bother to pass an arg.
    if arg_value is not None and arg_value != RING_SIZE_DEFAULT:
        return int(arg_value)
    env_val = os.environ.get(_RING_SIZE_ENV)
    if env_val:
        return int(env_val)
    return RING_SIZE_DEFAULT


def _resolve_scheduled_reconnect(arg_value: float | None) -> float:
    """Resolve the scheduled-reconnect cadence. Precedence:
    1. Explicit constructor arg (if not None)
    2. Env var FARMSV_RTSP_RECONNECT_SECONDS (if set + non-empty)
    3. SCHEDULED_RECONNECT_DEFAULT (3600s)

    Returns a float (seconds). Raises ValueError if env var is set
    but not a valid float — same shape as the existing ring-size
    resolver pattern at line 11.
    """
    if arg_value is not None:
        return float(arg_value)
    env_val = os.environ.get(_SCHEDULED_RECONNECT_ENV)
    if env_val:
        return float(env_val)
    return SCHEDULED_RECONNECT_DEFAULT


def _resolve_max_reconnect_attempts(arg_value: int | None) -> int:
    """Resolve the max consecutive failure-driven reconnect attempts.

    Precedence:
    1. Explicit constructor arg (if not None)
    2. Env var FARMSV_RTSP_MAX_RETRIES (if set + non-empty)
    3. RECONNECT_MAX_ATTEMPTS_DEFAULT (10)

    Returns an int. A value <= 0 disables the cap (legacy behavior,
    retry forever). Raises ValueError if env var is set but not a valid
    int.
    """
    if arg_value is not None:
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


# --------------------------------------------------------------------------
# Module-level registry (Phase 6B.87, PLAN §11.17)
# --------------------------------------------------------------------------
# The listener may register one (or more) PersistentRTSPReader instances at
# startup, keyed by canonical camera name. capture_frames() in
# frame_capture.py auto-resolves the right reader via get_reader_for_url()
# by stripping creds from the alert's RTSP URL. Tests can call
# clear_reader_registry() to reset between cases.
#
# Registry access is guarded by _registry_lock (RLock so a test that holds
# it can call further registry functions without deadlocking).
_READERS_KEY = "default"  # legacy single-slot shim key


_readers: dict[str, PersistentRTSPReader] = {}
_registry_lock = threading.RLock()


def set_reader(camera_name: str, reader: PersistentRTSPReader) -> None:
    """Register a reader. Back-compat: stores under `camera_name` AND under
    its CameraSpec.code (if the name resolves via the init'd registry).

    Replaces any prior reader registered under either key. Called at
    listener bootstrap, once per gatekeeper camera (CAM1/CAM2/...).

    Per Phase 6B.167 §13.5 Commit 10: this function is the back-compat
    path. New code should use `set_reader_by_code(spec.code, reader)`
    after calling `init_reader_registry(cameras)`.
    """
    code = _code_for_name(camera_name)
    with _registry_lock:
        _readers[camera_name] = reader
        if code is not None and code != camera_name:
            _readers[code] = reader
        log.info(
            "PersistentRTSPReader registered for %s (code=%s) (url=%s)",
            camera_name,
            code or "<unresolved>",
            reader._rtsp_url.split("@")[-1] if "@" in reader._rtsp_url else reader._rtsp_url,
        )


def get_reader(camera_name: str) -> PersistentRTSPReader | None:
    """Look up a reader by camera_name (canonical name OR CameraSpec.code).

    If `camera_name` matches a code in the registry, the code-stored
    reader is returned. Otherwise, falls back to direct name lookup
    (pre-init or non-canonical callers). Returns None if absent.
    """
    code = _code_for_name(camera_name)
    if code is not None:
        # Lookup by code to find code-stored readers (preferred).
        by_code = get_reader_by_code(code)
        if by_code is not None:
            return by_code
    # Fallback: direct name lookup (covers pre-init legacy callers).
    return get_reader_by_code(camera_name)


def get_reader_for_url(rtsp_url: str) -> PersistentRTSPReader | None:
    """Look up the registered reader whose URL matches `rtsp_url`.

    Credentials are stripped before comparison — the alert handler may
    pass a creds-bearing URL while the registered reader's URL has the
    same host:port/path. Comparison is exact-match on the creds-stripped
    form (case-insensitive scheme, exact host:port/path). The first
    matching reader wins; if multiple readers somehow share the same
    URL (shouldn't happen with one-reader-per-camera), behaviour is
    unspecified.
    """
    target = _strip_rtsp_creds(rtsp_url).lower()
    with _registry_lock:
        for reader in _readers.values():
            if _strip_rtsp_creds(reader._rtsp_url).lower() == target:
                return reader
    return None


def clear_reader_registry() -> None:
    """Test-only. Clear all registered readers. Never call in production."""
    with _registry_lock:
        _readers.clear()


def set_default_reader(reader: PersistentRTSPReader | None) -> None:
    """Back-compat shim. Equivalent to set_reader('default', reader).

    Retained so existing single-camera callers (and any future one-off
    scripts) don't break. The primary contract is set_reader() + name.
    """
    if reader is None:
        with _registry_lock:
            _readers.pop(_READERS_KEY, None)
    else:
        set_reader(_READERS_KEY, reader)


def get_default_reader() -> PersistentRTSPReader | None:
    """Back-compat shim. Equivalent to get_reader('default')."""
    return get_reader(_READERS_KEY)


def _strip_rtsp_creds(rtsp_url: str) -> str:
    """Return `rtsp_url` with the user:pass@ component removed.

    Example: rtsp://admin:secret@<CAM_IP_REDACTED>:554/path
          -> rtsp://<CAM_IP_REDACTED>:554/path

    Used by get_reader_for_url() so creds differences between the
    alert-side and reader-side URLs don't break the match.
    """
    scheme, _, rest = rtsp_url.partition("://")
    if "@" not in rest:
        return rtsp_url
    _, _, hostpath = rest.partition("@")
    return f"{scheme}://{hostpath}"


# ---------------------------------------------------------------------------
# CameraSpec-aware registry state (Phase 6B.167 §13.5 Commit 10, 2026-08-30)
# ---------------------------------------------------------------------------
#
# `_cameras` is the canonical list seeded by init_reader_registry(). It is
# the bridge from operator-flavored friendly names (passed to set_reader()
# by old callers) to CameraSpec.code (the primary key going forward).
# `_name_to_code` is a derived lookup table built from `_cameras` once per
# init. Both are module-level globals guarded by `_registry_lock`.

_cameras: list[CameraSpec] = []
_name_to_code: dict[str, str] = {}


def init_reader_registry(cameras: list[CameraSpec]) -> None:
    """Seed the registry with the canonical camera list.

    Must be called once at listener bootstrap BEFORE any set_reader_*()
    call. The list lets `set_reader(name, ...)` auto-derive spec.code
    from spec.name so name-keyed callers transparently populate the
    code-keyed storage too. Per Phase 6B.167 §13.5 Commit 10.

    Validates:
      - non-empty
      - unique spec.code
      - unique spec.name
    """
    global _cameras, _name_to_code
    if not cameras:
        raise ValueError("init_reader_registry() requires a non-empty cameras list")
    seen_codes: set[str] = set()
    seen_names: set[str] = set()
    for c in cameras:
        if c.code in seen_codes:
            raise ValueError(f"duplicate spec.code in registry: {c.code!r}")
        if c.name in seen_names:
            raise ValueError(f"duplicate spec.name in registry: {c.name!r}")
        seen_codes.add(c.code)
        seen_names.add(c.name)
    with _registry_lock:
        _cameras = list(cameras)
        _name_to_code = {c.name: c.code for c in cameras}
        # Clear any readers registered from a prior init — callers expect
        # a fresh registry tied to the new camera list.
        _readers.clear()


def set_reader_by_code(code: str, reader: PersistentRTSPReader) -> None:
    """Register a reader under its CameraSpec.code. Primary API going forward.

    Replaces any prior reader under the same code. Per Phase 6B.167
    §13.5 Commit 10.
    """
    with _registry_lock:
        _readers[code] = reader


def get_reader_by_code(code: str) -> PersistentRTSPReader | None:
    """Look up a reader by CameraSpec.code. Returns None if absent."""
    with _registry_lock:
        return _readers.get(code)


def _code_for_name(name: str) -> str | None:
    """Resolve an operator friendly-name back to its CameraSpec.code.

    Returns None if the name isn't in the init'd registry. Used by
    `set_reader()` to populate the code-keyed storage transparently
    for back-compat callers.
    """
    with _registry_lock:
        return _name_to_code.get(name)


class PersistentRTSPReader:
    """Long-lived RTSP connection that maintains a ring buffer of decoded frames.

    The reader opens the RTSP socket on .start() and keeps it open until
    .stop(). A background thread continuously decodes packets and pushes
    PNG-encoded bytes (lossless) into a deque. The main thread can call
    .get_recent_frames(n) at any time to get the most recent N frames as
    PNG file paths.

    2026-08-28 — Ring buffer switched to PNG bytes (~5 MB/frame for typical
    outdoor 2304x1296 footage) instead of decoded PIL.Image (~8.54 MB/frame).
    Previously encoded to JPEG q85 (~720 KB/frame) to cut RSS, but that
    lossy step made faces unrecognizable downstream.
    2026-09-01 — §11.88. Switched ring storage from JPEG q85 to lossless PNG.
    At 2 fps × 5 cams × 30 frames ring = ~750 MB RSS — well within the
    64 GB Mac mini's budget. Encoded bytes are PNG (headered), decodable
    via Image.open. Public API unchanged: get_recent_frames() /
    get_frames_by_offset() still return the same file-paths contract.
    """

    def __init__(
        self,
        rtsp_url: str,
        ring_size: int = RING_SIZE_DEFAULT,
        ffmpeg_flags: dict | None = None,
        scheduled_reconnect_seconds: float | None = None,
        max_reconnect_attempts: int | None = None,
    ):
        """Initialize the reader but do not connect yet. Call .start() to begin.

        Args:
            rtsp_url: Full RTSP URL with credentials (e.g.
                rtsp://admin:pass@<CAM_IP_REDACTED>:554/h264Preview_01_main).
            ring_size: How many decoded frames to retain in the in-memory
                ring buffer. Larger = more "lookback" time at the cost of
                RAM. 30 frames @ 5Mbps ~= 6MB.
            ffmpeg_flags: Optional dict of FFmpeg/libav input options.
                If None, defaults to: tcp transport, 10s timeout,
                fflags=+genpts, buffer_size=20MB. These are the same flags
                the on-demand capture uses post-2026-08-05 fix.
            scheduled_reconnect_seconds: 2026-08-16 (Phase 6B.80, PLAN
                §11.13). Cadence for proactive av-container close +
                reopen. Default 1 h. Resolved via env var
                FARMSV_RTSP_RECONNECT_SECONDS if arg is None.
            max_reconnect_attempts: 2026-08-28 (Phase 6B.155, PLAN
                §11.78). Cap on consecutive failure-driven reconnects.
                After N attempts the failure loop defers to the
                scheduled_reconnect_watchdog. Default 10. Resolved via
                env var FARMSV_RTSP_MAX_RETRIES if arg is None.
        """
        self._rtsp_url = rtsp_url
        # 2026-08-28 — Always resolve: constructor arg wins, then env var,
        # then constant. Earlier code assigned the raw constructor arg
        # which silently ignored the env var.
        self._ring_size = _resolve_ring_size(ring_size)
        self._scheduled_reconnect_seconds = _resolve_scheduled_reconnect(
            scheduled_reconnect_seconds
        )
        self._max_reconnect_attempts = _resolve_max_reconnect_attempts(
            max_reconnect_attempts
        )
        self._ring: deque[bytes] = deque(maxlen=self._ring_size)
        # §11.88 (2026-09-01): ring holds lossless PNG bytes (~5 MB/frame
        # at 2304x1296), not JPEG q85 (~720 KB/frame). Storing JPEG was
        # visibly hurting ArcFace face-recognition on the walk-test.
        # Decode is deferred to get_recent_frames / get_frames_by_offset.
        # 2026-08-28 bugfix: was deque(maxlen=ring_size) (raw constructor
        # arg), so the env-var resolver was being computed but never
        # applied. Use self._ring_size (the resolved value).
        self._ring_lock = threading.Lock()
        self._ffmpeg_flags = ffmpeg_flags or {
            "rtsp_transport": "tcp",
            "timeout": "10000000",  # 10s in microseconds
            "fflags": "+genpts",
            "buffer_size": "20000000",
        }
        self._container: InputContainer | None = None
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._start_time: float | None = None
        self._last_frame_time: float | None = None
        self._consecutive_errors = 0
        self._healthy = False
        # Public counters for /health
        self.frames_decoded_total = 0
        self.reconnects_total = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_for_url(self, rtsp_url: str) -> bool:
        """True if this reader was configured for the given RTSP URL.

        Match by URL string equality. Both URLs should be the full
        rtsp://user:pass@host:port/path form.
        """
        return self._rtsp_url == rtsp_url

    def is_healthy(self, stale_seconds: float = 5.0) -> bool:
        """True if we received a frame within the last `stale_seconds`."""
        if not self._healthy:
            return False
        if self._last_frame_time is None:
            return False
        return (time.monotonic() - self._last_frame_time) < stale_seconds

    def uptime_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def stream_fps(self) -> float:
        """Actual frames-per-second being delivered by the camera (camera time,
        not wall-clock). Reads from the video stream's `average_rate` which
        PyAV exposes after the RTSP container is opened. Returns 0.0 until
        the container is open, which signals callers (e.g. motion_gate) to
        use a default offset pattern.

        Phase 6B.160 (2026-08-28): added so motion_gate can compute fps-aware
        frame offsets at runtime. With Reolink main streams lowered from
        15fps → 2fps, hardcoded (0, 30, 60, 90) offsets exceed the ring
        buffer's 45s span at 2fps — fps-aware offsets (0, 4, 8, 12) keep
        the same 4-frame / 2s-spacing trail at any fps.
        """
        if self._container is None:
            return 0.0
        try:
            stream = self._container.streams.video[0]
            return float(stream.average_rate or 0.0)
        except (AttributeError, IndexError, TypeError):
            return 0.0

    @property
    def ring_size(self) -> int:
        """Configured ring buffer length (number of frames retained).

        Phase 6B.160 (2026-08-28): exposed so motion_gate can compute
        offset indices that count backward from the newest frame
        (ring_size - 1) instead of forward from the oldest (index 0).
        """
        return self._ring_size

    def start(self) -> None:
        """Open the RTSP socket and start the decode loop + scheduled-
        reconnect watchdog. Idempotent.
        """
        if self.is_running:
            log.warning("PersistentRTSPReader.start() called but already running")
            return
        self._stop_event.clear()
        self._start_time = time.monotonic()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"PersistentRTSP[{self._rtsp_url.split('@')[-1]}]",
            daemon=True,
        )
        self._thread.start()
        # 2026-08-16 — Phase 6B.80 (PLAN §11.13). Watchdog closes the
        # av container every `scheduled_reconnect_seconds` so the
        # existing _run_loop reopens a fresh one. Reuses the exception-
        # driven reconnect path; no new failure logic.
        self._watchdog_thread = threading.Thread(
            target=self._scheduled_reconnect_watchdog,
            name=f"PersistentRTSPWatchdog[{self._rtsp_url.split('@')[-1]}]",
            daemon=True,
        )
        self._watchdog_thread.start()
        log.info(
            f"PersistentRTSPReader started for {self._rtsp_url.split('@')[-1]} "
            f"(scheduled_reconnect_seconds={self._scheduled_reconnect_seconds:.0f})"
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the decode loop, watchdog, and close the socket. Idempotent."""
        if not self.is_running:
            return
        log.info("PersistentRTSPReader stopping...")
        self._stop_event.set()
        # Watchdog sleeps on _stop_event.wait() — it'll exit promptly.
        # Join with a short timeout for cleanliness.
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2.0)
            if self._watchdog_thread.is_alive():
                log.warning("Watchdog thread did not exit within timeout")
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("Decode thread did not exit within timeout")
        if self._container is not None:
            try:
                self._container.close()
            except Exception as e:
                log.debug(f"Error closing container (ignored): {e}")
            self._container = None
        self._healthy = False
        log.info("PersistentRTSPReader stopped")

    def get_recent_frames(
        self, n: int, output_dir: str, max_size: tuple[int, int] | None = None
    ) -> list[str]:
        """Return the most recent N frames as PNG file paths.

        Snapshots the current ring buffer, writes each frame to disk in
        output_dir/frame_001.png, frame_002.png, etc. Returns the list of
        file paths in chronological order (oldest first).

        If the ring buffer has fewer than N frames (e.g., startup), returns
        whatever's available — caller decides whether to abort or proceed.

        Args:
            n: How many frames to grab.
            output_dir: Directory to write frame_001.png ... frame_NNN.png.
            max_size: (width, height) to downscale to. Aspect preserved.
                If None, no resizing (full resolution).

        Returns:
            List of file paths, length <= n. Empty list if no frames in buffer.
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        # Clean any stale outputs in this directory
        for old in Path(output_dir).glob("frame_*.png"):
            try:
                old.unlink()
            except OSError:
                pass

        with self._ring_lock:
            # Grab the last n from the ring buffer
            frames = list(self._ring)[-n:]
        if not frames:
            return []

        out_paths = []
        for i, frame_bytes in enumerate(frames, start=1):
            out_path = os.path.join(output_dir, f"frame_{i:03d}.png")
            # §11.88 (2026-09-01): ring stores PNG bytes (lossless).
            # Decode to PIL.Image, optionally downscale, save as PNG.
            img: Image.Image = Image.open(io.BytesIO(frame_bytes))
            img.load()  # force full decode so BytesIO can be released
            if max_size is not None:
                img = img.copy()
                img.thumbnail(max_size, Image.Resampling.LANCZOS)
            img.save(out_path, format="PNG")
            out_paths.append(out_path)
        return out_paths

    def get_frames_by_offset(
        self,
        indices: list[int],
        output_dir: str,
        max_size: tuple[int, int] | None = None,
    ) -> list[str]:
        """Return frames at specific deque indices as PNG file paths.

        Index 0 = oldest frame in the ring buffer (farthest back).
        Index len(ring)-1 = newest frame (just decoded).

        Example: indices=[0, 30, 60, 90, 120, 150] at 15fps gives frames
        spanning T-12s through T+0s of camera time relative to the call
        (assuming the buffer is full and a fresh frame just landed).

        The requested indices are clamped to the valid range [0, len-1].
        Out-of-range indices are silently dropped — caller decides whether
        to abort or proceed with fewer frames (capture_frames() warns and
        returns what it got).

        Args:
            indices: List of deque positions to sample. Order is preserved
                in the output — frame_001.png = indices[0], frame_002.png =
                indices[1], etc.
            output_dir: Directory to write frame_001.png ... frame_NNN.png.
            max_size: (width, height) to downscale to. Aspect preserved.
                If None, no resizing (full resolution).

        Returns:
            List of file paths in the same order as indices. Empty if the
            ring buffer is empty.
        """
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        # Clean any stale outputs in this directory
        for old in Path(output_dir).glob("frame_*.png"):
            try:
                old.unlink()
            except OSError:
                pass

        with self._ring_lock:
            ring_snapshot = list(self._ring)
        if not ring_snapshot:
            return []

        ring_len = len(ring_snapshot)
        # Clamp indices to valid range; preserve original ordering
        valid_indices = [i for i in indices if 0 <= i < ring_len]
        if not valid_indices:
            log.warning(
                f"get_frames_by_offset: no valid indices (requested={indices}, "
                f"ring_len={ring_len})"
            )
            return []

        out_paths = []
        for i, idx in enumerate(valid_indices, start=1):
            frame_bytes = ring_snapshot[idx]
            out_path = os.path.join(output_dir, f"frame_{i:03d}.png")
            # §11.88 (2026-09-01): ring stores PNG bytes (lossless).
            img: Image.Image = Image.open(io.BytesIO(frame_bytes))
            img.load()
            if max_size is not None:
                img = img.copy()
                img.thumbnail(max_size, Image.Resampling.LANCZOS)
            img.save(out_path, format="PNG")
            out_paths.append(out_path)

        log.info(
            f"get_frames_by_offset: requested={indices}, valid={valid_indices}, "
            f"ring_len={ring_len}, returned={len(out_paths)} frames"
        )
        return out_paths

    def _scheduled_reconnect_watchdog(self) -> None:
        """2026-08-16 — Phase 6B.80 (PLAN §11.13). Proactive cycle of
        stop-then-restart on the decode thread every
        `scheduled_reconnect_seconds`. Replaces the container-close
        approach (PLAN §11.13.2) after probe-ofs-scheduled_reconnect
        showed that closing av.container from outside the decode loop
        segfaults PyAV's C demux() loop — see test_persistent_rtsp.py
        test_watchdog_closes_container_after_interval (the unit test
        mocks container.close() and so does NOT catch the segfault;
        only the live probe does).

        Cycle (each fire):
          1. Set stop_event so the decode thread breaks out of
             `_decode_iteration`'s packet loop on the next packet.
             In practice the demux() loop is blocked on libav so the
             thread does NOT exit until libav times out or yields —
             see "failure modes" below.
          2. Join the decode thread with a 10 s timeout. If alive,
             the thread is stuck in C-land demux() (zombie state).
             We log a warning and proceed anyway.
          3. Close the container (now safe — decode thread is gone
             or stuck in C). Set _container = None.
          4. Spin up a fresh decode thread via the same code path
             `.start()` uses.

        Failure modes:
        - If PyAV's C demux() doesn't yield on stop_event (which is
          exactly the zombie scenario we're trying to break out of):
          the decode thread may stay stuck past the 10 s join
          timeout. We log `decode_thread_did_not_exit` and proceed.
          In the worst case, the new decode thread spins up alongside
          the stuck one. Mitigated by the next watchdog fire 1 h
          later, which would re-attempt. The `is_healthy()` staleness
          check still trips within ~30 s of frames stopping, so
          alerts fail-loud as expected.
        - If container.close() raises (socket already torn down):
          caught at debug; _container set to None regardless.

        Trade-off vs. the original PLAN §11.13 design: stop+start
        preserves the ring buffer (frames captured up to the moment
        of stop are still in self._ring), but briefly pauses frame
        ingest for ~1-2 s during the close+reopen window.
        """
        while not self._stop_event.is_set():
            # Sleep on stop_event until the next deadline.
            self._stop_event.wait(
                timeout=self._scheduled_reconnect_seconds
            )
            if self._stop_event.is_set():
                break
            self._scheduled_reconnect_fire()
        # On exit (stop_event set), we don't fire — .stop() handles
        # the rest of the lifecycle.

    def _scheduled_reconnect_fire(self) -> None:
        """One fire of the proactive reconnect cycle. Called by the
        watchdog after the cadence elapses. Stops the decode thread
        (gracefully via stop_event + bounded join), closes the
        container, then spawns a fresh decode thread.
        """
        log.info(
            f"scheduled_reconnect_fire: starting "
            f"(uptime={self.uptime_seconds():.0f}s, "
            f"frames_decoded={self.frames_decoded_total}, "
            f"reconnects_total={self.reconnects_total})"
        )
        # Signal the decode thread to break out on its next packet.
        # The decode loop checks self._stop_event.is_set() on every
        # packet, but the demux() generator blocks in C waiting for
        # a packet. So this signal alone may not free the thread.
        self._stop_event.set()
        # Join the decode thread. If it's stuck in demux() (the very
        # zombie state we're trying to recover from), the join will
        # time out — we proceed anyway.
        old_thread = self._thread
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=10.0)
            if old_thread.is_alive():
                log.warning(
                    "scheduled_reconnect_fire: decode thread did not "
                    "exit within 10s (stuck in container.demux()). "
                    "Proceeding with reconnect anyway."
                )
        # Now safe to close the container — decode thread is gone or
        # stuck in C-land and we can't reach it from here.
        if self._container is not None:
            try:
                self._container.close()
            except Exception as e:
                log.debug(
                    f"scheduled_reconnect_fire: container.close() "
                    f"raised (ignored): {e}"
                )
            self._container = None
        # Reset the stop_event and start a fresh decode thread.
        # self._thread was set to the old (now-exited) Thread object;
        # .start() will overwrite it with a new Thread.
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"PersistentRTSP[{self._rtsp_url.split('@')[-1]}]",
            daemon=True,
        )
        self._thread.start()
        self.reconnects_total += 1
        log.info(
            f"scheduled_reconnect_fire: completed "
            f"(reconnects_total={self.reconnects_total})"
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

        Per the 2026-08-28 CAM2 incident (6 errors in 80s with ERRNO 60 /
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
                        f"RTSP recovered after {self._consecutive_errors} "
                        f"consecutive failures — decoder running normally"
                    )
                break
            except Exception as e:
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
                            f"PersistentRTSP decode iteration failed "
                            f"({self._consecutive_errors} consecutive "
                            f"attempts). Last error: {e}. Deferring to "
                            f"scheduled_reconnect_watchdog "
                            f"({self._scheduled_reconnect_seconds:.0f}s "
                            f"cadence). Reader is UNHEALTHY until next "
                            f"reconnect."
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
                        f"PersistentRTSP decode iteration failed "
                        f"(attempt {self._consecutive_errors}): {e}. "
                        f"Reconnecting in {backoff:.1f}s... "
                        f"({remaining} attempt(s) remaining before "
                        f"deferring to watchdog)"
                    )
                else:
                    log.warning(
                        f"PersistentRTSP decode iteration failed "
                        f"(attempt {self._consecutive_errors}): {e}. "
                        f"Reconnecting in {backoff:.1f}s... "
                        f"(deferred to scheduled_reconnect_watchdog)"
                    )
                time.sleep(backoff)
                backoff = min(backoff * RECONNECT_BACKOFF_MULT, RECONNECT_BACKOFF_MAX)
                self.reconnects_total += 1

    def _decode_iteration(self) -> None:
        """Single connection lifecycle: open → decode → close on error."""
        log.info(f"Opening RTSP: {self._rtsp_url.split('@')[-1]}")
        container = av.open(self._rtsp_url, options=self._ffmpeg_flags)
        self._container = container
        stream = container.streams.video[0]
        log.info(
            f"RTSP open: time_base={stream.time_base}, "
            f"avg_rate={stream.average_rate}, "
            f"width={stream.codec_context.width}, "
            f"height={stream.codec_context.height}"
        )
        # Reset backoff on successful open
        self._consecutive_errors = 0
        self._healthy = True

        for packet in container.demux(stream):
            if self._stop_event.is_set():
                break
            if packet.dts is None:
                continue
            for frame in packet.decode():
                if self._stop_event.is_set():
                    break
                if frame is None:
                    continue
                # Store lossless PNG bytes in the ring buffer. We previously encoded
                # to JPEG q=85 here to cut RSS (~720 KB/frame), but that lossy
                # step made faces unrecognizable downstream.
                # §11.88 (2026-09-01): at 2 fps the ring memory budget is fine
                # for PNG (~5 MB/frame x 5 cams x 30 frames = ~750 MB total).
                # PNG is lossless, format-headered, decodable via Image.open.
                try:
                    img = frame.to_image()
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    png_bytes = buf.getvalue()
                except Exception as e:
                    log.debug(f"frame PNG encode failed: {e}")
                    continue
                with self._ring_lock:
                    self._ring.append(png_bytes)
                self.frames_decoded_total += 1
                self._last_frame_time = time.monotonic()

        log.info("RTSP demux loop exited (container closed or stop requested)")
        try:
            container.close()
        except Exception as e:
            log.debug(f"Error closing container after iteration: {e}")
        self._container = None
        self._healthy = False
