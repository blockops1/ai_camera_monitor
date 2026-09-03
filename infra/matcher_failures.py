"""
matcher_failures.py — Counter and rate-tracker for matcher exceptions on the
motion+match path.

STATUS: stable
THREAD SAFETY: uses threading.Lock (single internal lock guards all state)

INPUTS:
    - function arg `exc: BaseException` to record() — the matcher exception
      to count and remember

OUTPUTS:
    - side effect: in-memory state updates under a lock
    - no file writes, no network calls

PUBLIC API:
    record(exc: BaseException) -> int
        Record a matcher failure; returns the failure count in the rolling
        5-minute window (failures per 5 minutes, useful for rate alerts).
    stats() -> dict[str, Any]
        Snapshot of failure state for /status. Keys:
          - total_failures: int — cumulative count since listener start
          - failures_last_5min: int — count in the rolling 5-minute window
          - last_failure_at: float | None — monotonic timestamp of last failure
          - last_exception: str | None — repr() of the most recent exception
    MATCHER_FAILURES: MatcherFailureTracker
        Module-level singleton. All worker threads share this; the
        listener's /status endpoint reads from it.

DOES NOT DO:
    - Actually suppress or handle the matcher exception → that's the
      motion+match path's job; we just count
    - Persist failure history to disk → in-memory only; resets on restart
    - Alert the operator when failures spike → that's a /status consumer
      concern (could be added as a follow-on if the operator wants push
      alerts on high failure rates)
    - Track cooldown state → that lives in infra.cooldown

WHY HERE:
    Phase.108 extraction (2026-08-21). Originally
    `_MatcherFailureTracker` lived inside listener.py at L86-211 alongside
    `_MotionCooldown`. The two classes had nothing in common — one tracks
    cooldown state for motion+match dedup, the other counts matcher
    failures. Splitting them into two modules makes each one single-purpose
    and lets future tests import either without dragging the other along.

    The class was named `_MatcherFailureTracker` (underscore-prefixed) in
    listener.py because of Python's "underscore = private" convention for
    module-internal names. Renamed to `MatcherFailureTracker` (public)
    because the class is now part of an `infra/` module's public API;
    callers explicitly import it. The singleton is `MATCHER_FAILURES`
    (uppercase, no underscore) for the same reason.

CALLED BY:
    - listener.listener._process_alert_safe() — record(exc) on matcher exception
    - listener.listener.create_app() — stats() for /status output

CALLS INTO:
    - threading.Lock: guards internal state
    - time.monotonic(): immune to wall-clock jumps (same reason as MotionCooldown)
"""
from __future__ import annotations

import threading
import time
from typing import Any


class MatcherFailureTracker:
    """Count matcher exceptions during motion+match path.

    The motion+match path wraps `_match_with_details(...)` in
    `try/except Exception` so a matcher bug cannot break the lead
    motion alert (see L2265 in alert_listener.py). But silently
    swallowing exceptions is its own failure mode: if the matcher
    raises on every alert for an hour, the user gets motion alerts
    but zero match alerts and no log lines drawing attention.

    This tracker:
      1. Counts every matcher exception
      2. Remembers the last exception's repr
      3. Tracks failure timestamps for the rate calculation
      4. Exposes stats via /status
    """

    def __init__(self) -> None:
        self._total_failures: int = 0
        self._last_failure_at: float | None = None
        self._last_exception_repr: str | None = None
        # Rolling 5-minute window for the "rate" calculation
        self._recent_timestamps: list[float] = []
        self._lock = threading.Lock()

    def record(self, exc: BaseException) -> int:
        """Record a matcher failure; returns the failure count in the rolling
        5-minute window (i.e., failures per 5 minutes as an integer count).
        """
        with self._lock:
            now = time.monotonic()
            self._total_failures += 1
            self._last_failure_at = now
            self._last_exception_repr = repr(exc)
            self._recent_timestamps.append(now)
            # Trim to last 5 minutes
            cutoff = now - 300
            self._recent_timestamps = [
                t for t in self._recent_timestamps if t >= cutoff
            ]
            return len(self._recent_timestamps)

    def stats(self) -> dict[str, Any]:
        """Snapshot of failure state for /status."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - 300
            recent = [t for t in self._recent_timestamps if t >= cutoff]
            return {
                "total_failures": self._total_failures,
                "failures_last_5min": len(recent),
                "last_failure_at": self._last_failure_at,
                "last_exception": self._last_exception_repr,
            }


# Singleton — module-level so /status can read it before any failure has
# happened (test bootstrap case). Survives across _process_alert
# invocations but resets on listener restart (acceptable; /status surfaces
# the running count).
MATCHER_FAILURES = MatcherFailureTracker()