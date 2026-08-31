"""
_state_classes_archive_6B108.py — verbatim snapshot of two listener-internal
classes and their singletons, extracted from listener.py on 2026-08-21
(Phase.108) before they were moved into separate infra modules:

  - _MotionCooldown          → infra/cooldown.py::MotionCooldown
  - _MatcherFailureTracker   → infra/matcher_failures.py::MatcherFailureTracker
  - _MOTION_COOLDOWN         → infra.cooldown.MOTION_COOLDOWN
  - _MATCHER_FAILURES        → infra.matcher_failures.MATCHER_FAILURES

The old in-listener copy was removed when the slim was committed (see
6B.108 commit message). Keep this file for:

  - Rollback: cp back to listener.py and delete the new infra modules
  - Diff archaeology: "what did the original class look like before the
    refactor?"
  - Regression: someone trying to claim the listener lost functionality
    can grep this file.

Per archive-first-workflow (2026-08-20, the operator): the archive must be written
BEFORE the slim is committed. This file is the source of truth for the
pre-6B.108 contract.

=== ORIGINAL listener.py lines 86-211 (Phase.108 archive) ===
class _MotionCooldown:
    """Dedup motion+match alerts by (camera, captured_at minute).

    The standard alert path has UUID-keyed cooldown via
    `notifier._alert_cooldown`; the new motion+match path needs an
    equivalent. Two webhooks for the same physical event within the
    window produce two motion alerts + two match alerts without this
    (LOGIC-FLOWS §F2.5c).

    Key shape: (camera_name: str, timestamp_minute: str)
        - timestamp_minute is the ISO timestamp truncated to "YYYY-MM-DDTHH:MM"
          — coarse enough to absorb IR-reflection re-triggers (~1-15s apart)
          but fine enough to allow legitimate separate events at HH:MM:01
          and HH:MM+1:00 to both fire.

    Window: 60 seconds. After the first mark, subsequent marks within
    60s are treated as "already cooled" (returns is_cool=True).

    Thread-safe: the listener runs 4 worker threads (see
    `_ClassedWebhookExecutor`). Two webhooks from the same physical
    burst can land on different workers; we need shared state.
    """

    def __init__(self, window_seconds: int = 60) -> None:
        self._window = window_seconds
        self._last_seen: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def is_cool(self, key: tuple) -> bool:
        """Return True if `key` is currently within its cooldown window.

        Returns False if `key` is unseen or its last mark is older than
        the window. Does NOT modify state — call `mark(key)` after a
        successful fire so subsequent calls within the window return True.
        """
        now = time.monotonic()
        with self._lock:
            last = self._last_seen.get(key)
            if last is None:
                return False
            return (now - last) < self._window

    def mark(self, key: tuple) -> None:
        """Record that an alert fired for `key` at this instant."""
        with self._lock:
            self._last_seen[key] = time.monotonic()

    def stats(self) -> dict[str, Any]:
        """Snapshot of cooldown state for /status."""
        with self._lock:
            now = time.monotonic()
            active = sum(
                1 for last in self._last_seen.values()
                if (now - last) < self._window
            )
            return {
                "window_seconds": self._window,
                "active_keys": active,
                "total_keys_tracked": len(self._last_seen),
            }


# ----------------------------------------------------------------------
# Phase 1.3 (2026-08-05) — matcher failure counter
# ----------------------------------------------------------------------
class _MatcherFailureTracker:
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
        """Record a matcher failure; returns the failure rate per 5min."""
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


# Singleton — module-level so all worker threads share state. Survives
# across _process_alert invocations but resets on listener restart
# (acceptable; /status surfaces the running count).
_MATCHER_FAILURES = _MatcherFailureTracker()

# Singleton — module-level so /status can read it before _process_alert
# has ever fired (test bootstrap case). Same pattern as _MATCHER_FAILURES.
_MOTION_COOLDOWN = _MotionCooldown(window_seconds=60)
