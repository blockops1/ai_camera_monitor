"""
health.py — Periodic health heartbeat for listener + cameras.

STATUS: new
THREAD SAFETY: one daemon thread; reads shared state via locks internally.

INPUTS:
    - env var LISTENER_HEALTH_INTERVAL_SECONDS (default 600 = 10 min)

OUTPUTS:
    - One structured multi-line log block emitted every interval to daemon.log:
        listener_health: uptime=<h+m> pid=<pid> port=<port>
                         env_total=<n> plist_keepalive=<bool>
                         mcp_enabled=<bool> alerts_last_10m=<n>
        camera_health: id=<camera_id> healthy=<bool> secs_since_last_frame=<float|None>
                       frames=<int> reconnects=<int> teardowns=<int>
                       last_teardown_age=<float|None>  (one per camera)

PUBLIC API:
    HealthReporter — context manager (start/stop) wrapping a daemon thread.
    format_listener_health(uptime_s, port, env_total, mcp_enabled) -> str
    format_camera_health(camera_id, stats_dict) -> str
    format_vision_health(up, model, base_url, response_ms, last_call_age_s) -> str
    format_cleanup_health(alive, last_tick_age_s, free_gb, cap_gb, reclaimed) -> str
    format_telegram_health(configured, bot_username, last_send_age_s, errors_last_hour) -> str

DOES NOT DO:
    - Introduce new processes, endpoints, plists, or dependencies.

WHY HERE:
    Operators need a periodic, independent view of listener + camera health
    that fires even when zero alerts arrive (the bug this card fixes).

CALLS INTO:
    - CameraCaptureRegistry.stats_all() — live reader stats per camera.
    - infra.pipeline_cooldown — pipeline run timestamps for alerts_last_10m.

CALLER:
    listener/daemon.py:main() starts HealthReporter after bootstrap();
    daemon.py shutdown path calls _stop_event.set() + thread.join().
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IP redaction (shared with infra/farm_api/vision.py).
# ---------------------------------------------------------------------------
_LAN_IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")
_REDACTED = "[REDACTED-IP]"


def _redact_ip(value: str) -> str:
    """Replace all IPv4 addresses with [REDACTED-IP]."""
    return _LAN_IP_RE.sub(_REDACTED, value)

# ---------------------------------------------------------------------------
# Module-level uptime — set once at module import (≈ process start).
# ---------------------------------------------------------------------------
_START_TIME: float = time.monotonic()

# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
_INTERVAL_SECONDS: int = int(
    os.environ.get("LISTENER_HEALTH_INTERVAL_SECONDS", "600")
)

# ---------------------------------------------------------------------------
# Uptime formatting helpers
# ---------------------------------------------------------------------------


def _uptime_str(seconds: float) -> str:
    """Return uptime as 'Xh+Ym'."""
    total_minutes = int(seconds) // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    return f"{hours}h+{minutes}m"


# ---------------------------------------------------------------------------
# HealthReporter class
# ---------------------------------------------------------------------------


class HealthReporter:
    """Periodic health heartbeat daemon thread.

    Fires every _INTERVAL_SECONDS (default 600 s = 10 min).
    First heartbeat is after one full interval — NOT immediately.

    Lifecycle:
        hr = HealthReporter()
        hr.start()     # launches daemon thread, first fire at T+interval
        hr.stop()      # signals thread to exit and joins (timeout=5s)
    """

    def __init__(
        self,
        port: int = 8090,
        env_total: int = 0,
        plist_keepalive: bool = True,
    ) -> None:
        self._port = port
        self._env_total = env_total
        self._plist_keepalive = plist_keepalive
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, mcp_enabled: bool = False) -> None:
        """Start the heartbeat thread.

        Args:
            mcp_enabled: whether the MCP server thread is running.
        """
        self._mcp_enabled = mcp_enabled
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="HealthReporter",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "health: heartbeat thread started; interval=%ds port=%d",
            _INTERVAL_SECONDS,
            self._port,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the heartbeat thread to exit and wait up to *timeout* seconds."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Heartbeat core
    # ------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        """Main loop: sleep interval, emit health block, repeat until stop."""
        while True:
            if self._stop_event.wait(timeout=_INTERVAL_SECONDS):
                break
            try:
                self._emit()
            except Exception:
                log.exception("health: heartbeat loop raised")

    def _emit(self) -> None:
        """Emit one structured multi-line health block."""
        uptime_s = time.monotonic() - _START_TIME
        lines = []

        # Listener health line.
        lines.append(
            self.format_listener_health(
                uptime_s,
                self._port,
                self._env_total,
                self._mcp_enabled,
            )
        )

        # Camera health lines (one per registered camera).
        lines.extend(self._camera_health_lines())

        # Vision-LLM health line (US-073i).
        lines.append(self._vision_health_line())

        # Cleanup-thread health line (US-073i).
        lines.append(self._cleanup_health_line())

        # Telegram-bot health line (US-073i).
        lines.append(self._telegram_health_line())

        # Join and emit as one block so the log file doesn't interleave.
        block = "\n".join(lines)
        log.info("health: heartbeat:\n%s", block)

    # ------------------------------------------------------------------
    # Formatting helpers (unit-testable)
    # ------------------------------------------------------------------

    @staticmethod
    def format_listener_health(
        uptime_s: float,
        port: int,
        env_total: int,
        mcp_enabled: bool,
    ) -> str:
        """Return the listener_health line."""
        uptime = _uptime_str(uptime_s)
        return (
            f"listener_health: uptime={uptime} pid={os.getpid()} "
            f"port={port} env_total={env_total} "
            f"plist_keepalive={True} "
            f"mcp_enabled={mcp_enabled} "
            f"alerts_last_10m={_count_alerts_last_10m()}"
        )

    @staticmethod
    def format_camera_health(camera_id: str, stats: dict) -> str:
        """Return one camera_health line for the given camera stats dict.

        The stats dict is the output of
        PersistentRTSPReader.stats() — see infra/frame_capture.py:177.
        """
        healthy = stats.get("healthy_flag", False) or stats.get("is_healthy", False)
        secs_frame = stats.get("seconds_since_last_frame")
        frames = stats.get("frames_decoded_total", 0)
        reconnects = stats.get("reconnects_total", 0)
        teardowns = stats.get("teardowns_total", 0)
        teardown_age = stats.get("seconds_since_teardown")

        return (
            f"camera_health: id={camera_id} healthy={healthy} "
            f"secs_since_last_frame={secs_frame} "
            f"frames={frames} reconnects={reconnects} "
            f"teardowns={teardowns} "
            f"last_teardown_age={teardown_age}"
        )

    # ------------------------------------------------------------------
    # Vision health formatting (US-073i)
    # ------------------------------------------------------------------

    @staticmethod
    def format_vision_health(
        up: bool,
        model: str | None,
        base_url: str | None,
        response_ms: float | None,
        last_call_age_s: float | None,
    ) -> str:
        """Return the vision_health line."""
        model = model or "unknown"
        base_url_redacted = _redact_ip(base_url) if base_url else "unknown"
        return (
            f"vision_health: up={up} model={model} base_url={base_url_redacted} "
            f"response_ms={response_ms} last_call_age_s={last_call_age_s}"
        )

    # ------------------------------------------------------------------
    # Cleanup health formatting (US-073i)
    # ------------------------------------------------------------------

    @staticmethod
    def format_cleanup_health(
        alive: bool,
        last_tick_age_s: float,
        free_gb: float,
        cap_gb: int,
        bytes_reclaimed: int | None,
    ) -> str:
        """Return the cleanup_health line."""
        return (
            f"cleanup_health: alive={alive} last_tick_age_s={last_tick_age_s} "
            f"free_gb={free_gb:.2f} cap_gb={cap_gb} "
            f"bytes_reclaimed_last_run={bytes_reclaimed}"
        )

    # ------------------------------------------------------------------
    # Telegram health formatting (US-073i)
    # ------------------------------------------------------------------

    @staticmethod
    def format_telegram_health(
        configured: bool,
        bot_username: str | None,
        last_send_age_s: float | None,
        errors_last_hour: int,
    ) -> str:
        """Return the telegram_health line."""
        username = bot_username or "none"
        return (
            f"telegram_health: configured={configured} bot_username={username} "
            f"last_send_age_s={last_send_age_s} errors_last_hour={errors_last_hour}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _camera_health_lines(self) -> list[str]:
        """Return one health line per registered camera."""
        from infra.frame_capture import CameraCaptureRegistry

        lines: list[str] = []
        stats_all = CameraCaptureRegistry.stats_all()
        if not stats_all:
            return lines
        for cid in sorted(stats_all.keys()):
            lines.append(self.format_camera_health(cid, stats_all[cid]))
        return lines

    def _vision_health_line(self) -> str:
        """Build and return the vision-health line.

        Probes GET {VISION_LLM_URL}/health every tick (3.0 s timeout).
        On failure emits a separate 'vision_call_failed' log line.
        """
        import httpx

        from infra import vision_analyzer

        vision_url = getattr(vision_analyzer, "_VISION_URL", None) or "unknown"
        model = getattr(vision_analyzer, "_VISION_MODEL", None)

        up = False
        response_ms = None

        if vision_url and vision_url != "unknown":
            try:
                start = time.monotonic()
                with httpx.Client(timeout=3.0) as client:
                    resp = client.get(f"{vision_url}/health")
                elapsed_ms = (time.monotonic() - start) * 1000
                if resp.status_code == 200:
                    up = True
                    response_ms = round(elapsed_ms, 1)
                else:
                    up = False
                    vision_record_failure()
            except httpx.HTTPError:
                up = False
                vision_record_failure()

        # last_call_age_s from vision_analyzer module state.
        last_ts = getattr(vision_analyzer, "_last_call_timestamp", None)
        last_call_age_s = round(time.monotonic() - last_ts, 1) if last_ts is not None else None

        # On failure, log immediately (not waiting for next tick).
        if not up and vision_consecutive_failures() >= 3:
            log.warning("vision_health: %d consecutive failures", vision_consecutive_failures())

        return self.format_vision_health(
            up=up,
            model=model,
            base_url=vision_url,
            response_ms=response_ms,
            last_call_age_s=last_call_age_s,
        )

    def _cleanup_health_line(self) -> str:
        """Build and return the cleanup-health line."""
        from infra import cleanup, paths

        # alive: we can't easily get the thread handle here without passing
        # it in, so we infer alive from last_tick being recent.
        last_tick = getattr(cleanup, "last_tick_monotonic", None)
        last_tick_age_s = None
        if last_tick is not None:
            last_tick_age_s = round(time.monotonic() - last_tick, 1)
        else:
            last_tick_age_s = -1.0  # sentinel: never ticked

        reclaimed = getattr(cleanup, "last_reclaimed_bytes", None)

        # Free disk and cap.
        try:
            usage = shutil.disk_usage(paths.DATA_DIR)
            free_gb = usage.free / (1024 ** 3)
        except OSError:
            free_gb = 0.0

        cap_gb = paths.FRAME_MAX_BYTES // (1024 ** 3)

        return self.format_cleanup_health(
            alive=last_tick is not None,
            last_tick_age_s=last_tick_age_s,
            free_gb=free_gb,
            cap_gb=cap_gb,
            bytes_reclaimed=reclaimed,
        )

    def _telegram_health_line(self) -> str:
        """Build and return the telegram-health line."""
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")
        configured = bool(bot_token and chat_id)

        # Bot username (cached 1 hour).
        bot_username = _get_bot_username() if configured else None

        # last_send_age_s from infra/telegram_alerts module state.
        last_send = getattr(
            __import__("infra.telegram_alerts", fromlist=["_last_send_timestamp"]),
            "_last_send_timestamp",
            None,
        )
        last_send_age_s = round(time.monotonic() - last_send, 1) if last_send is not None else None

        # errors_last_hour from the ring buffer.
        errors = getattr(
            __import__("infra.telegram_alerts", fromlist=["_send_errors"]),
            "_send_errors",
            deque(maxlen=100),
        )
        now = time.monotonic()
        errors_last_hour = sum(1 for t in errors if now - t < 3600.0)

        return self.format_telegram_health(
            configured=configured,
            bot_username=bot_username,
            last_send_age_s=last_send_age_s,
            errors_last_hour=errors_last_hour,
        )


# ---------------------------------------------------------------------------
# Pipeline-run counter — counts _run_pipeline calls in the last 10 minutes.
# ---------------------------------------------------------------------------
# We use a simple in-memory list of monotonic timestamps guarded by a lock.
# Each _run_pipeline call appends its time.monotonic() snapshot.
# The counter prunes entries older than 10 minutes.


_alert_timestamps: list[float] = []
_alert_lock = threading.Lock()


def _record_alert_run() -> None:
    """Call from _run_pipeline() (or equivalent) after every pipeline invocation."""
    now = time.monotonic()
    with _alert_lock:
        _alert_timestamps.append(now)
        # Prune old entries (keep last 10 min).
        cutoff = now - 600.0
        while _alert_timestamps and _alert_timestamps[0] < cutoff:
            _alert_timestamps.pop(0)


def _count_alerts_last_10m() -> int:
    """Return the count of pipeline runs in the last 10 minutes."""
    now = time.monotonic()
    cutoff = now - 600.0
    with _alert_lock:
        return sum(1 for t in _alert_timestamps if t >= cutoff)


def _clear_alert_timestamps() -> None:
    """Test helper. Drop all alert timestamps."""
    with _alert_lock:
        _alert_timestamps.clear()


# ---------------------------------------------------------------------------
# Vision consecutive-failure tracker (US-073i).
# ---------------------------------------------------------------------------
_vision_failures: deque[float] = deque(maxlen=10)  # timestamps of consecutive failures


def vision_record_failure() -> None:
    """Call when a vision health probe fails. Returns consecutive count."""
    _vision_failures.append(time.monotonic())


def vision_consecutive_failures() -> int:
    """Return number of consecutive failures in the last 10 minutes."""
    cutoff = time.monotonic() - 600.0
    while _vision_failures and _vision_failures[0] < cutoff:
        _vision_failures.popleft()
    return len(_vision_failures)


# ---------------------------------------------------------------------------
# Telegram health helper — bot.get_me() cache (US-073i).
# ---------------------------------------------------------------------------
_bot_username_cache: str | None = None
_bot_username_cached_at: float | None = None


def _get_bot_username() -> str | None:
    """Return cached bot username (1-hour TTL). Probes via get_me on cache miss."""
    import httpx

    global _bot_username_cache, _bot_username_cached_at
    now = time.monotonic()
    if _bot_username_cache and _bot_username_cached_at and now - _bot_username_cached_at < 3600.0:
        return _bot_username_cache
    # Cache expired — probe once per hour
    try:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            return None
        # Lazy: only call get_me here (no httpx import at top to avoid cost)
        url = f"https://api.telegram.org/bot{bot_token}/getMe"
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                username = data.get("result", {}).get("username")
                if username:
                    _bot_username_cache = f"@{username}"
                    _bot_username_cached_at = now
                    return _bot_username_cache
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError):
        log.debug("health: failed to probe Telegram getMe for username")
    return _bot_username_cache


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _clear_vision_failures() -> None:
    """Test helper. Drop vision failure timestamps."""
    with _alert_lock:  # reuse existing lock; no new lock needed for deque
        _vision_failures.clear()


def _clear_telegram_username_cache() -> None:
    """Test helper. Clear the bot username cache."""
    global _bot_username_cache, _bot_username_cached_at
    _bot_username_cache = None
    _bot_username_cached_at = None
