"""
matcher_telemetry.py — Per-pass metrics + periodic snapshot for the matcher.

STATUS: stable
THREAD SAFETY: uses threading.Lock internally; safe to call from any thread.

INPUTS:
    - env var (none currently — see path configuration)
    - output_path: str passed to start_telemetry_snapshot_thread()
    - interval_seconds: int passed to start_telemetry_snapshot_thread()

OUTPUTS:
    - matcher_telemetry.json (atomic write via tmp + os.replace)
    - log line on each successful snapshot
    - warning log on disagreement-rate > 50% (PRD requirement)

PUBLIC API:
    _MatchTelemetry() — class; record_attempt/record_success/record_no_match/
                       record_ambiguous/record_latency/snapshot
    get_telemetry_snapshot() -> dict
        Public snapshot for /status endpoint + disk persistence.
    start_telemetry_snapshot_thread(output_path: str, interval_seconds: int = 300)
        Start a daemon thread that writes the snapshot every interval.
        Idempotent: re-calling returns the existing thread.
    stop_telemetry_snapshot_thread()
        Signal the thread to stop + wait briefly. Safe to call multiple times.
    write_snapshot_to_disk(telemetry: _MatchTelemetry, path: str)
        Atomic write. Caller is responsible for catching OSError.
    _reset_telemetry_for_tests() — drop the singleton (test fixture).
    PASS_MAKE_MODEL / PASS_MAKE_ONLY / PASS_COLOR_TYPE / PASS_COLORS_ALT /
    PASS_TYPE_GROUP_FLEX / PASS_BODY_STYLE_FLEX / PASS_TYPE_ONLY
        Pass-name constants used by both the matcher (call sites) and the
        telemetry class (snapshot keys). The names match across the system.

DOES NOT DO:
    - Match vehicles — that lives in infra.vehicle_matcher
    - Persist telemetry to a database — file-only, atomic-rename
    - Surface telemetry to Telegram — listener reads it for /status only

WHY HERE:
    Extracted from vehicle_state in the vehicle_state removal plan
    (Aug 2026). Telemetry is a matcher concern, not a state-machine
    concern. Splitting it out keeps vehicle_matcher.py focused on the
    matching rules and lets tests target telemetry in isolation.

CALLED BY:
    - infra.vehicle_matcher (per-pass record_attempt/success/no_match/etc.)
    - listener.listener._status() route — get_telemetry_snapshot()
    - listener.listener bootstrap — start_telemetry_snapshot_thread()

CALLS INTO:
    - infra.vehicle_matcher._shadow_counters_snapshot() for shadow merge
    - threading.Lock, threading.Thread, threading.Event
    - json, os.replace for atomic disk writes
    - collections.deque for bounded latency buffer
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import datetime
from typing import Any

from infra.paths import LOCAL_TZ

log = logging.getLogger(__name__)


# Pass-name constants — used by both the matcher (call sites) and the
# telemetry class (snapshot keys). Keeping them here so the names match
# across the system.
PASS_MAKE_MODEL = "make_model"
PASS_MAKE_ONLY = "make_only"
PASS_COLOR_TYPE = "color_type"
PASS_COLORS_ALT = "colors_alt"
PASS_TYPE_GROUP_FLEX = "type_group_flex"
PASS_BODY_STYLE_FLEX = "body_style_flex"
PASS_TYPE_ONLY = "type_only"


# Bounded latency buffer per pass. 1000 samples is ~16 minutes at the
# typical 1Hz capture rate; older entries are evicted.
_LATENCY_DEQUE_CAP = 1000


class _MatchTelemetry:
    """Per-pass metrics for the matcher.

    Holds four counters per pass (attempts / successes / no_match /
    ambiguous), a per-vehicle pass-win counter, and a bounded latency
    deque. Thread-safe via a single lock (cheaper than per-counter locks
    for this scale; ~50ns critical section).
    """

    def __init__(self) -> None:
        self.attempts: dict[str, int] = {}
        self.successes: dict[str, int] = {}
        self.no_match: dict[str, int] = {}
        self.ambiguous: dict[str, int] = {}
        self.successes_by_vehicle: dict[str, dict[str, int]] = {}
        # deque(maxlen=1000) caps memory; older entries are evicted.
        self.latency_ms_deque: dict[str, deque[int]] = {}
        self._lock = threading.Lock()
        self._start_time = datetime.now(LOCAL_TZ)

    def record_attempt(self, pass_name: str) -> None:
        """Called when a pass BEGINS evaluating a signature."""
        with self._lock:
            self.attempts[pass_name] = self.attempts.get(pass_name, 0) + 1

    def record_success(self, pass_name: str, vehicle_id: str) -> None:
        """Called when a pass returns a match."""
        with self._lock:
            self.successes[pass_name] = self.successes.get(pass_name, 0) + 1
            per_veh = self.successes_by_vehicle.setdefault(vehicle_id, {})
            per_veh[pass_name] = per_veh.get(pass_name, 0) + 1

    def record_no_match(self, pass_name: str) -> None:
        """Called when the LAST pass to run returns None.

        Note: only the LAST pass increments this; intermediate passes
        that fall through are tracked by `attempts` only. Avoids
        double-counting.
        """
        with self._lock:
            self.no_match[pass_name] = self.no_match.get(pass_name, 0) + 1

    def record_ambiguous(self, pass_name: str) -> None:
        """Called when a pass finds multiple candidates and falls through
        (e.g. Pass 1.7 with 2+ color+type matches)."""
        with self._lock:
            self.ambiguous[pass_name] = self.ambiguous.get(pass_name, 0) + 1

    def record_latency(self, pass_name: str, ms: int) -> None:
        """Record a pass's wall-clock latency in ms."""
        with self._lock:
            dq = self.latency_ms_deque.get(pass_name)
            if dq is None:
                dq = deque(maxlen=_LATENCY_DEQUE_CAP)
                self.latency_ms_deque[pass_name] = dq
            dq.append(int(ms))

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for /status + disk.

        Only includes passes that have fired (no static pass-name list).
        Latency P50/P95 are computed from the bounded deque.
        """
        with self._lock:
            by_pass: dict[str, dict[str, Any]] = {}
            # Union of all pass names that have any nonzero counter.
            all_pass_names = (
                set(self.attempts)
                | set(self.successes)
                | set(self.no_match)
                | set(self.ambiguous)
                | set(self.latency_ms_deque)
            )
            for name in sorted(all_pass_names):
                dq = list(self.latency_ms_deque.get(name, ()))
                p50, p95 = _percentiles(dq)
                by_pass[name] = {
                    "attempts": self.attempts.get(name, 0),
                    "successes": self.successes.get(name, 0),
                    "no_match": self.no_match.get(name, 0),
                    "ambiguous": self.ambiguous.get(name, 0),
                    "p50_ms": p50,
                    "p95_ms": p95,
                    "latency_samples": len(dq),
                }
            # Phase.26a — pull shadow counters from vehicle_matcher.
            # The spec interpreter runs in parallel with the legacy matcher;
            # disagreements between the two are counted there.
            shadow_disagreements = 0
            shadow_agreements = 0
            try:
                # Lazy import to avoid circular dep at module load time
                from infra import vehicle_matcher as vm
                shadow_counts = vm._shadow_counters_snapshot()
                shadow_disagreements = shadow_counts.get("disagreements", 0)
                shadow_agreements = shadow_counts.get("agreements", 0)
            except Exception as _vm_err:
                # If vehicle_matcher is unavailable, just skip the shadow counts.
                log.debug(f"vehicle_matcher._shadow_counters_snapshot() unavailable: {_vm_err}")

            return {
                "uptime_seconds": int(
                    (datetime.now(LOCAL_TZ) - self._start_time).total_seconds()
                ),
                "total_attempts": sum(self.attempts.values()),
                "total_successes": sum(self.successes.values()),
                "total_no_match": sum(self.no_match.values()),
                "total_ambiguous": sum(self.ambiguous.values()),
                "by_pass": by_pass,
                "by_vehicle": {
                    vid: dict(per_veh)
                    for vid, per_veh in self.successes_by_vehicle.items()
                },
                # Phase.26a — side-by-side shadow comparison counters.
                "shadow_disagreements": shadow_disagreements,
                "shadow_agreements": shadow_agreements,
            }


def _percentiles(values: list[int]) -> tuple[int | None, int | None]:
    """Compute (P50, P95) from a list of ints. Returns (None, None) if empty.

    Uses the nearest-rank method (simple, deterministic). For sample
    sizes < 20, both are returned as the max to avoid wild extrapolation.
    """
    if not values:
        return None, None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    p50_idx = max(0, min(n - 1, round(0.50 * (n - 1))))
    p95_idx = max(0, min(n - 1, round(0.95 * (n - 1))))
    return int(sorted_vals[p50_idx]), int(sorted_vals[p95_idx])


# ---------------------------------------------------------------------------
# Module-level state + public accessors
# ---------------------------------------------------------------------------

_telemetry_singleton: _MatchTelemetry | None = None
_telemetry_singleton_lock = threading.Lock()


def _get_telemetry() -> _MatchTelemetry:
    """Singleton accessor. Mirrors alert_listener.get_webhook_executor()."""
    global _telemetry_singleton
    if _telemetry_singleton is None:
        with _telemetry_singleton_lock:
            if _telemetry_singleton is None:
                _telemetry_singleton = _MatchTelemetry()
    return _telemetry_singleton


def _reset_telemetry_for_tests() -> None:
    """Drop the singleton so the next _get_telemetry() returns a fresh one.

    Used by the autouse session-scoped fixture in tests/test_matcher_telemetry.py
    to prevent cross-test counter leakage.
    """
    global _telemetry_singleton
    with _telemetry_singleton_lock:
        _telemetry_singleton = None


def get_telemetry_snapshot() -> dict[str, Any]:
    """Public snapshot for /status endpoint + disk persistence."""
    return _get_telemetry().snapshot()


# ---------------------------------------------------------------------------
# Periodic disk-snapshot thread
# ---------------------------------------------------------------------------

_telemetry_snapshot_thread: threading.Thread | None = None
_telemetry_snapshot_stop = threading.Event()


def start_telemetry_snapshot_thread(
    output_path: str,
    interval_seconds: int = 300,
) -> threading.Thread:
    """Background thread that writes the telemetry snapshot to disk
    every `interval_seconds` (default 5 min).

    Phase.25 — gives operators cross-restart visibility into matcher
    behavior without needing to scrape logs. Atomic write (tmp + os.replace
    via write_snapshot_to_disk). The thread is daemon=True so it dies
    with the listener; no shutdown ceremony needed.

    Args:
        output_path: Absolute path to the snapshot file.
        interval_seconds: How often to write. Default 300 (5 min).
            Min 30 (no point in faster than that — operators don't
            read this faster than 30s, and the disk I/O is wasted).

    Returns:
        The Thread object. Mostly for tests; daemon thread dies with
        the listener process.
    """
    global _telemetry_snapshot_thread
    if _telemetry_snapshot_thread is not None and _telemetry_snapshot_thread.is_alive():
        return _telemetry_snapshot_thread

    def _run():
        while not _telemetry_snapshot_stop.is_set():
            try:
                write_snapshot_to_disk(_get_telemetry(), output_path)
            except Exception as e:
                log.warning(
                    "matcher_telemetry snapshot write failed: %s",
                    e,
                )

            # Phase.47 — log scored disagreement rate per Note
            # preference (50% gate; warn above 50%). Reads counters
            # from vehicle_matcher module globals; never raises.
            try:
                from infra import vehicle_matcher as vm
                shadow = vm._shadow_counters_snapshot()
                sa = shadow.get("scored_agreements", 0)
                sd = shadow.get("scored_disagreements", 0)
                total = sa + sd
                if total >= 5:
                    rate = sd / total
                    if rate > 0.5:
                        log.warning(
                            "phase_6b47_scored_disagreement_rate_high "
                            "rate=%.2f agreements=%d disagreements=%d",
                            rate, sa, sd,
                        )
                    else:
                        log.info(
                            "phase_6b47_scored_disagreement_rate "
                            "rate=%.2f agreements=%d disagreements=%d",
                            rate, sa, sd,
                        )
            except Exception as _rate_err:
                log.debug("phase_6b47_rate_check_failed err=%r", _rate_err)

            _telemetry_snapshot_stop.wait(interval_seconds)

    _telemetry_snapshot_stop.clear()
    thread = threading.Thread(
        target=_run,
        name="matcher-telemetry-snapshot",
        daemon=True,
    )
    thread.start()
    _telemetry_snapshot_thread = thread
    return thread


def stop_telemetry_snapshot_thread() -> None:
    """Signal the snapshot thread to stop and wait briefly for it.

    Safe to call multiple times. No-op if the thread was never started.
    """
    _telemetry_snapshot_stop.set()
    if _telemetry_snapshot_thread is not None:
        _telemetry_snapshot_thread.join(timeout=2)


def write_snapshot_to_disk(
    telemetry: _MatchTelemetry, path: str
) -> None:
    """Atomic snapshot write: tmp file + os.replace.

    Same pattern as heartbeat.set_last_vision() — readers can never see
    a partial file. Caller is responsible for catching OSError (disk
    full, permission denied) — telemetry MUST NOT crash the listener.
    """
    snap = telemetry.snapshot()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=2, sort_keys=True)
    os.replace(tmp, path)