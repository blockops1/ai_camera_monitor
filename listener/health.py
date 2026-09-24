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

DOES NOT DO:
    - Probe vision-LLM health (US-073j)
    - Log cleanup-thread ticks (US-073k)
    - Log telegram bot health (US-073l)
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
import threading
import time

log = logging.getLogger(__name__)

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
