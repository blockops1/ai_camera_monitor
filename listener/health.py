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
        api_health: port=8092 bound=<bool> uptime_seconds=<n>
                    token_len=<n> last_healthz=<ISO-timestamp>
        mcp_health: port=8091 bound=<bool> uptime_seconds=<n>
                    token_len=<n> tools_count=<n>

PUBLIC API:
    HealthReporter — start/stop wrapping a daemon thread.
    format_listener_health(uptime_s, port, env_total, mcp_enabled) -> str
    format_camera_health(camera_id, stats_dict) -> str
    format_vision_health(up, model, base_url, response_ms, last_call_age_s) -> str
    format_cleanup_health(alive, last_tick_age_s, free_gb, cap_gb, reclaimed) -> str
    format_telegram_health(configured, bot_username, last_send_age_s, errors_last_hour) -> str
    format_api_health(bound, uptime_s, token_len, last_healthz) -> str
    format_mcp_health(bound, uptime_s, token_len, tools_count) -> str

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

import datetime
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

# US-092a: import for TG-on-fail alert.
# Direct import from the submodule to avoid triggering infra.__init__.py
# which pulls in env_required machinery.
def send_health_alert(block: str, summary: str) -> tuple[int, str]:
    """Re-export from infra.telegram_health_alert."""
    from infra.telegram_health_alert import send_health_alert as _sha

    return _sha(block, summary)

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

# US-092c: watchdog dispatch interval (seconds). Default 300 = 5 min.
_WATCHDOG_INTERVAL_SECONDS: int = int(
    os.environ.get("LISTENER_HEALTH_WATCHDOG_INTERVAL_SECONDS", "300")
)

# US-092c: watchdog dispatch enable flag. 1 = ON (default), 0 = OFF.
_WATCHDOG_ENABLED: bool = os.environ.get("LISTENER_HEALTH_WATCHDOG_ENABLED", "1") == "1"

# US-092c: path to the watchdog script.
_WATCHDOG_SCRIPT: str = os.path.join(
    os.environ.get("FARMSURV_PROJECT_ROOT", os.path.expanduser("~/farm-surveillance-v2")),
    "bin",
    "health_watchdog.sh",
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
        api_enabled: bool = False,
    ) -> None:
        self._port = port
        self._env_total = env_total
        self._plist_keepalive = plist_keepalive
        self._api_enabled = api_enabled
        self._api_healthz_last: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # US-092c: watchdog dispatcher (separate thread from heartbeat).
        self._watchdog = _WatchdogLoop()

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
        # US-092c: start the watchdog dispatcher thread.
        self._watchdog.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the heartbeat thread to exit and wait up to *timeout* seconds."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        # US-092c: stop the watchdog dispatcher too.
        self._watchdog.stop(timeout=timeout)

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

        # API server health line (US-079).
        api_line = self._api_health_line()
        if api_line is not None:
            lines.append(api_line)

        # MCP server health line (US-079).
        mcp_line = self._mcp_health_line()
        if mcp_line is not None:
            lines.append(mcp_line)

        # Join and emit as one block so the log file doesn't interleave.
        block = "\n".join(lines)
        log.info("health: heartbeat:\n%s", block)

        # TG-on-fail (US-092a): check for unhealthy markers, alert if found.
        self._tg_on_fail(block)

    def _tg_on_fail(self, block: str) -> None:
        """Check health block for unhealthy markers; alert on failure.

        Regex targets: up=False, healthy=False, 'cameras.*unhealthy'.
        When any match: build a compact summary and call send_health_alert.
        Only fires when LISTENER_HEALTH_TG_ON_FAIL=1.
        """
        if not int(os.environ.get("LISTENER_HEALTH_TG_ON_FAIL", "0")):
            return

        unhealthy_re = re.compile(r"up=False|healthy=False|cameras.*unhealthy")
        matches = unhealthy_re.findall(block)
        if not matches:
            return

        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        label = ",".join(sorted(set(matches)))
        summary = f"Health ALERT ({now.strftime('%H:%M')} EDT): {len(matches)} issue{'s' if len(matches) != 1 else ''} -- {label}"
        rc, _ = send_health_alert(block, summary)
        log.info("health: tg_alert rc=%d", rc)

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
    # API health formatting (US-079)
    # ------------------------------------------------------------------

    @staticmethod
    def format_api_health(
        bound: bool,
        uptime_s: float,
        token_len: int,
        last_healthz: str,
    ) -> str:
        """Return the api_health line."""
        return (
            f"api_health: port=8092 bound={bound} "
            f"uptime_seconds={round(uptime_s)} "
            f"token_len={token_len} "
            f"last_healthz={last_healthz}"
        )

    # ------------------------------------------------------------------
    # MCP health formatting (US-079)
    # ------------------------------------------------------------------

    @staticmethod
    def format_mcp_health(
        bound: bool,
        uptime_s: float,
        token_len: int,
        tools_count: int,
    ) -> str:
        """Return the mcp_health line."""
        return (
            f"mcp_health: port=8091 bound={bound} "
            f"uptime_seconds={round(uptime_s)} "
            f"token_len={token_len} "
            f"tools_count={tools_count}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_port_bound(port: int) -> bool:
        """Check whether a TCP port is in LISTEN state via lsof."""
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-sTCP:LISTEN", "-n", "-P"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            # lsof returns 1 line of header + N data lines; header alone = 1
            lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
            return len(lines) > 1  # header + at least one data line
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _api_health_line(self) -> str | None:
        """Build and return the api-health line.

        Returns None when the API server is disabled.
        """
        if not self._api_enabled:
            return None

        import httpx

        token_len = len(os.environ.get("FARMSURV_API_TOKEN", ""))
        bound = self._is_port_bound(8092)

        # Uptime from infra.api_server._SERVER_START.
        try:
            from infra import api_server

            start = api_server._SERVER_START
            uptime_s = (
                datetime.datetime.now(datetime.UTC) - start
            ).total_seconds()
        except (ImportError, AttributeError):
            uptime_s = 0

        # Probe /healthz and record last successful timestamp.
        # US-101y: when API binds to Tailscale-only (100.x), 127.0.0.1
        # does not reach the listener. Try Tailscale IP first, fall back
        # to loopback. Both use the configured API port.
        import infra.net_bind

        last_healthz_str: str = "never"
        api_port = int(os.environ.get("FARMSURV_API_PORT", "8092"))
        probe_urls = [
            f"http://{infra.net_bind.TAILNET_BIND}:{api_port}/healthz",
            f"http://127.0.0.1:{api_port}/healthz",
        ]
        for probe_url in probe_urls:
            try:
                with httpx.Client(timeout=3.0) as client:
                    resp = client.get(probe_url)
                if resp.status_code == 200:
                    self._api_healthz_last = time.monotonic()
                    last_healthz_str = datetime.datetime.now(datetime.UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    break
            except httpx.HTTPError:
                continue
        else:
            # All probes failed — fall back to last known time.
            if self._api_healthz_last is not None:
                last_healthz_str = datetime.datetime.fromtimestamp(
                    self._api_healthz_last, tz=datetime.UTC
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                last_healthz_str = "never"

        return self.format_api_health(
            bound=bound,
            uptime_s=uptime_s,
            token_len=token_len,
            last_healthz=last_healthz_str,
        )

    def _mcp_health_line(self) -> str | None:
        """Build and return the mcp-health line.

        Returns None when the MCP server is disabled.
        """
        if not self._mcp_enabled:
            return None

        token_len = len(os.environ.get("FARMSURV_MCP_TOKEN", ""))
        bound = self._is_port_bound(8091)

        # Uptime from infra.mcp_server._SERVER_START.
        try:
            from infra import mcp_server

            start = mcp_server._SERVER_START
            uptime_s = (
                datetime.datetime.now(datetime.UTC) - start
            ).total_seconds()
        except (ImportError, AttributeError):
            uptime_s = 0

        # tools_count from _GLOBAL_REGISTRY.
        try:
            from infra import mcp_server

            registry = mcp_server._GLOBAL_REGISTRY[0]
            tools_count = len(registry._tools) if registry is not None else 0
        except (ImportError, AttributeError):
            tools_count = 0

        return self.format_mcp_health(
            bound=bound,
            uptime_s=uptime_s,
            token_len=token_len,
            tools_count=tools_count,
        )

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

        vision_url = getattr(vision_analyzer, "_VISION_BASE_URL", None) or "unknown"
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

        # US-093a: last_call_age_s from last_success_timestamp (time since last
        # successful /v1/chat/completions), not from the heartbeat ticker.
        last_success_ts = getattr(vision_analyzer, "_last_success_timestamp", None)
        last_call_age_s = round(time.monotonic() - last_success_ts, 1) if last_success_ts is not None else None

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
# Watchdog dispatcher (US-092c): in-process daemon thread that fires the
# health_watchdog.sh script on a regular cadence.
#
# This is a single in-process thread inside the supervised listener; it
# satisfies the AGENTS.md no-backup-plist rule because no second supervisor
# is involved. If the listener crashes, the watchdog goes with it, which is
# the correct failure mode (no orphan dispatcher).
#
# Off-switches:
#   - LISTENER_HEALTH_WATCHDOG_ENABLED=0  (dispatcher kill switch)
#   - WATCHDOG_SILENT=1                   (script-level exit)
# Either one being OFF skips the fire.
# ---------------------------------------------------------------------------


class _WatchdogLoop:
    """Periodic watchdog dispatcher daemon thread.

    Fires bin/health_watchdog.sh every _WATCHDOG_INTERVAL_SECONDS (default 300 s = 5 min).
    First fire is after one full interval — NOT immediately.

    Lifecycle:
        wl = _WatchdogLoop()
        wl.start()     # launches daemon thread, first fire at T+interval
        wl.stop()      # signals thread to exit and joins (timeout=5s)
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the watchdog dispatcher thread."""
        self._thread = threading.Thread(
            target=self._loop,
            name="WatchdogLoop",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "health: watchdog dispatcher started; interval=%ds script=%s",
            _WATCHDOG_INTERVAL_SECONDS,
            _WATCHDOG_SCRIPT,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the watchdog thread to exit and wait up to *timeout* seconds."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        """Main loop: sleep interval, fire watchdog script, repeat until stop."""
        while True:
            if self._stop_event.wait(timeout=_WATCHDOG_INTERVAL_SECONDS):
                break
            try:
                self._fire()
            except Exception:
                log.exception("health: watchdog loop raised")

    def _fire(self) -> None:
        """Execute the watchdog script and log the result."""
        # Honor BOTH off-switches.
        if not _WATCHDOG_ENABLED:
            return
        if os.environ.get("WATCHDOG_SILENT", "0") == "1":
            return

        result = subprocess.run(
            ["bash", _WATCHDOG_SCRIPT],
            capture_output=True,
            text=True,
            timeout=270,
            check=False,
        )
        stdout_head = result.stdout[:200] if result.stdout else ""
        stderr_head = result.stderr[:200] if result.stderr else ""
        log.info(
            "watchdog: rc=%d stdout_head=%s stderr_head=%s",
            result.returncode,
            stdout_head,
            stderr_head,
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
