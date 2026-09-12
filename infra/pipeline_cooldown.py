"""
pipeline_cooldown.py — Per-(camera_id, classification) cooldown suppression.

STATUS: stable
THREAD SAFETY: uses threading.Lock (single lock guards the state dict)

INPUTS:
    - function arg `camera_id: str` (required)
    - function arg `classification: str` (required)
    - function arg `now: float` (required) — monotonic timestamp from caller

OUTPUTS:
    - should_suppress(camera_id, classification, now) -> bool
        Return True if within cooldown; does NOT record on miss.
    - record_hit(camera_id, classification, now) -> None
        Record timestamp for (camera_id, classification) without checking.

PUBLIC API:
    should_suppress(camera_id, classification, now) -> bool
        Pure function: reads state, returns boolean, does NOT write.
    record_hit(camera_id, classification, now) -> None
        Pure write: takes now as argument, only writes state. Returns None.
    should_suppress_default(camera_id, classification) -> bool
        Production wrapper: calls should_suppress with time.monotonic().
    record_hit_default(camera_id, classification) -> None
        Production wrapper: calls record_hit with time.monotonic().
    clear() -> None
        Test helper. Drop state. Never call in production.
    PipelineCooldown  — backward-compatible class wrapping module-level state.

DOES NOT DO:
    - Persist cooldowns to disk — in-memory only; resets on restart
    - Send Telegram messages — that lives in infra.notifier
    - Decide WHICH alerts to suppress — that's the caller's job

WHY HERE:
    Phase 6B pipeline-level cooldown for the new unified pipeline.
    Distinct from gate_cooldown.py (gate-level) and cooldown.py
    (alert-level). Covers the full pipeline path.

CALLED BY:
    - listener/pipeline.py (via PipelineCooldown class)

CALLS INTO:
    - threading.Lock: guards the state dict
    - time.monotonic(): production callers supply now from this

RELATED:
    - infra/gate_cooldown.py — gate-level (camera, event_type) cooldown
    - infra/cooldown.py — alert-level (alert_id) and bucket cooldowns
"""

from __future__ import annotations

import threading
import time

# ---------------------------------------------------------------------------
# Module-level constant — default cooldown window in seconds.
# Tests can monkeypatch this at module level.
# ---------------------------------------------------------------------------
COOLDOWN_WINDOW_SECONDS: int = 30

# ---------------------------------------------------------------------------
# Per-class default windows (used by PipelineCooldown class only).
# The pure module functions use COOLDOWN_WINDOW_SECONDS.
# ---------------------------------------------------------------------------
DEFAULT_WINDOWS: dict[str, int] = {
    "vehicle": 60,
    "person": 30,
    "motion": 120,
    "animal": 0,
}

# ---------------------------------------------------------------------------
# Module-level state — a single shared dict guarded by a lock.
# Pure functions read/write this dict; the PipelineCooldown class wraps it.
# ---------------------------------------------------------------------------
_state: dict[str, float] = {}  # (camera_id, classification) -> last_hit timestamp
_state_lock = threading.Lock()


def should_suppress(
    camera_id: str,
    classification: str,
    now: float,
) -> bool:
    """Return True if (camera_id, classification) is within cooldown.

    PURE FUNCTION: reads state, returns boolean, does NOT write.
    The previous side-effect of recording on miss is GONE — the caller
    must explicitly call record_hit() after deciding to proceed.

    Window resolution: uses COOLDOWN_WINDOW_SECONDS module constant.
    """
    key = f"{camera_id}:{classification}"

    with _state_lock:
        last = _state.get(key)

    if last is None:
        return False

    elapsed = now - last
    return elapsed < COOLDOWN_WINDOW_SECONDS


def record_hit(
    camera_id: str,
    classification: str,
    now: float,
) -> None:
    """Record timestamp for (camera_id, classification).

    PURE WRITE: takes now as an argument, only writes state. Returns None.
    Does NOT check suppression — pure timestamp writer.
    """
    key = f"{camera_id}:{classification}"

    with _state_lock:
        _state[key] = now


def should_suppress_default(
    camera_id: str,
    classification: str,
) -> bool:
    """Production wrapper: calls should_suppress with time.monotonic()."""
    return should_suppress(camera_id, classification, time.monotonic())


def record_hit_default(
    camera_id: str,
    classification: str,
) -> None:
    """Production wrapper: calls record_hit with time.monotonic()."""
    record_hit(camera_id, classification, time.monotonic())


def clear() -> None:
    """Test helper. Drop all cooldown state."""
    with _state_lock:
        _state.clear()


# ---------------------------------------------------------------------------
# Backward-compatible class wrapper (used by existing callers).
# ---------------------------------------------------------------------------
class PipelineCooldown:
    """Per-(camera_id, classification) cooldown suppression.

    Wraps the module-level _state dict so existing PipelineCooldown()
    callers continue to work unchanged. The class uses DEFAULT_WINDOWS
    for per-class window sizes (not COOLDOWN_WINDOW_SECONDS) to preserve
    existing behavior for callers that pass window_seconds arguments.

    Default windows (seconds): vehicle=60, person=30, motion=120, animal=0.
    """

    def __init__(self) -> None:
        self._last_hit: dict[tuple[str, str], float] = {}
        self._window: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def should_suppress(
        self,
        camera_id: str,
        classification: str,
        window_seconds: int = 0,
    ) -> bool:
        """Return True if (camera_id, classification) is within cooldown.

        Records the current timestamp on a miss so a follow-up within
        the window returns True.

        Window resolution (first wins): explicit arg > stored window >
        DEFAULT_WINDOWS[classification] > 0.
        """
        key = (camera_id, classification)
        now = time.monotonic()

        with self._lock:
            last = self._last_hit.get(key)
            if last is None:
                self._window[key] = window_seconds or self._default_window(
                    classification
                )
                self._last_hit[key] = now
                return False

            stored_window = self._window.get(key, 0)
            effective = window_seconds if window_seconds > 0 else stored_window

            if effective > 0 and (now - last) < effective:
                return True

            self._last_hit[key] = now
            return False

    def record_hit(self, camera_id: str, classification: str) -> None:
        """Record timestamp for (camera_id, classification).

        Sets the cooldown window if not already set. Does NOT check
        suppression — pure timestamp writer.
        """
        key = (camera_id, classification)
        now = time.monotonic()

        with self._lock:
            self._last_hit[key] = now
            if key not in self._window:
                self._window[key] = self._default_window(classification)

    def clear(self) -> None:
        """Test helper. Drop both cooldown maps."""
        with self._lock:
            self._last_hit.clear()
            self._window.clear()

    @staticmethod
    def _default_window(classification: str) -> int:
        """Return the default window for a classification string."""
        return DEFAULT_WINDOWS.get(classification, 0)
