"""
pipeline_cooldown.py — Per-(camera_id, classification) cooldown suppression.

STATUS: stable
THREAD SAFETY: uses threading.Lock (single lock guards both maps)

INPUTS:
    - function arg `camera_id: str` (required)
    - function arg `classification: str` (required)
    - function arg `window_seconds: int` (optional, defaults per-class)

OUTPUTS:
    - return value: bool (should_suppress) — True if within cooldown
    - side effect: in-memory dict updates under a lock

PUBLIC API:
    should_suppress(camera_id, classification, window_seconds=0) -> bool
        Return True if within cooldown; records timestamp on miss.
    record_hit(camera_id, classification) -> None
        Record timestamp for (camera_id, classification) without checking.
    clear() -> None
        Test helper. Drop both maps. Never call in production.

DOES NOT DO:
    - Persist cooldowns to disk — in-memory only; resets on restart
    - Send Telegram messages — that lives in infra.notifier
    - Decide WHICH alerts to suppress — that's the caller's job

WHY HERE:
    Phase 6B pipeline-level cooldown for the new unified pipeline.
    Distinct from gate_cooldown.py (gate-level) and cooldown.py
    (alert-level). Covers the full pipeline path.

CALLED BY:
    - (to be wired by US-002 or later — not yet connected to listener)

CALLS INTO:
    - threading.Lock: guards both maps
    - time.monotonic(): cooldown window comparison

RELATED:
    - infra/gate_cooldown.py — gate-level (camera, event_type) cooldown
    - infra/cooldown.py — alert-level (alert_id) and bucket cooldowns
"""

from __future__ import annotations

import threading
import time

DEFAULT_WINDOWS: dict[str, int] = {
    "vehicle": 60,
    "person": 30,
    "motion": 120,
    "animal": 0,
}


class PipelineCooldown:
    """Per-(camera_id, classification) cooldown suppression.

    Two maps under a single lock: _last_hit (timestamps) and
    _window (per-class window sizes). Thread-safe for the
    listener's 4-thread worker pool.

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
