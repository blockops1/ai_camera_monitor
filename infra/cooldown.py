"""
cooldown.py — Per-alert_id and per-(camera, title-bucket) cooldown suppression
plus the MotionCooldown class for dedup of motion+match alerts.

STATUS: stable
THREAD SAFETY: uses threading.Lock (single internal lock guards both maps)

INPUTS:
    - env var FARM_BUCKET_COOLDOWN_SECONDS (optional, default 1800 = 30 min)
    - function arg `window_seconds: int = 60` to MotionCooldown (per-class
      cooldown window for motion+match dedup)

OUTPUTS:
    - side effect: in-memory dict updates under a lock
    - no file writes, no network calls

PUBLIC API:
    is_in_cooldown(alert_id: str, cooldown_seconds: int) -> bool
        Return True if `alert_id` is still within `cooldown_seconds` of its
        last notification; record the current timestamp on a miss.
    is_in_bucket_cooldown(bucket_key: str, bucket_cooldown_seconds: int) -> bool
        Same pattern, keyed by `(camera, title_bucket)` instead of alert_id.
        Suppresses overnight floods where the same camera generates many
        alerts with different UUIDs but the same underlying event type.
    is_in_vision_block_cooldown(cooldown_seconds: int = 1800) -> bool
        Global rate-limit for the per-alert "🔍 VISION_CAM4ERVATIONS" Telegram.
        Returns True if a vision block was sent within the window — caller
        should skip the vision-message send. Independent of the alert and
        bucket cooldowns; suppressing here never affects the alert body or
        photo. Default 1800s (30 min). Phase.101.
    make_bucket_key(alert: dict) -> str
        Build the bucket key from an alert dict. Returns "" if the alert
        doesn't have the fields needed to build one.
    clear_all_cooldowns() -> None
        Test helper. Drop all three cooldown maps. Never call in production.
    DEFAULT_BUCKET_COOLDOWN: int
    DEFAULT_VISION_BLOCK_COOLDOWN: int
        Module-level default for vision-block cooldown (1800s = 30 min).
    MotionCooldown(window_seconds: int = 60)
        Dedup motion+match alerts by (camera, captured_at minute). Two
        webhooks for the same physical event within the window produce
        two motion alerts + two match alerts without this. Extracted from
        listener.py Phase.108 (2026-08-21). The standard alert path has
        UUID-keyed cooldown via is_in_cooldown; this class is a separate
        motion+match dedup with its own state.

DOES NOT DO:
    - Send Telegram messages → that lives in infra.notifier
    - Persist cooldowns to disk → in-memory only by design; resets on restart
    - Decide WHICH alerts to suppress → that's the listener's job, not this module's
    - Track matcher failures → that lives in infra.matcher_failures (Phase.108)

WHY HERE:
    Phase.108 extraction (2026-08-21). Originally `MotionCooldown` lived
    inside listener.py at L86-211 alongside `_MatcherFailureTracker`. The
    cooldown concept and the matcher-failure-counting concept are
    different concerns, so the failure tracker moved to its own module
    (infra/matcher_failures.py). The cooldown concept belonged in this
    file because the listener already used `is_in_cooldown` /
    `is_in_bucket_cooldown` from this module; putting MotionCooldown here
    means both suppression mechanisms share the same mental model.

    Note: MotionCooldown uses `time.monotonic()` (immune to wall-clock
    jumps) while is_in_cooldown uses `time.time()` (cooldown windows
    relative to send time, not blocking). Different clocks for different
    concerns — they're both cooldowns but they shouldn't share state.

CALLED BY:
    - infra.notifier.notify() — is_in_cooldown + is_in_bucket_cooldown
    - listener.listener._process_alert_safe() — MOTION_COOLDOWN.is_cool / mark
    - listener.listener.create_app() — MOTION_COOLDOWN.stats() for /status

CALLS INTO:
    - threading.Lock: guards both maps + MotionCooldown state
    - time.time(): for cooldown window comparison (wall clock)
    - time.monotonic(): for MotionCooldown (immune to clock jumps)
    - os.environ: reads FARM_BUCKET_COOLDOWN_SECONDS at import time
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

# Default window: 30 minutes (1800s). Long enough to suppress the overnight
# flood (00:00 – 06:00), short enough that a real second vehicle showing up
# at 00:35 still triggers an alert.
DEFAULT_BUCKET_COOLDOWN = int(os.environ.get("FARM_BUCKET_COOLDOWN_SECONDS", "1800"))

# Phase.101 — global rate-limit on the per-alert "🔍 VISION_CAM4ERVATIONS"
# Telegram block. Default 30 min. Independent of the alert and bucket
# cooldowns; only throttles the optional vision-text message that goes
# alongside the alert body + photo. The alert body always sends.
DEFAULT_VISION_BLOCK_COOLDOWN = int(
    os.environ.get("FARM_VISION_BLOCK_COOLDOWN_SECONDS", "1800")
)

# First 30 chars of title form a stable bucket — same alert type from the same
# camera within the window gets suppressed, different alerts do not.
TITLE_BUCKET_PREFIX_LEN = 30


# ---------------------------------------------------------------------------
# Module-level state (in-memory only; resets on listener restart)
# ---------------------------------------------------------------------------

# {alert_id: timestamp_sent} — per-alert suppression (the listener's UUID)
_alert_cooldown: dict[str, float] = {}

# {bucket_key: timestamp_sent} — per-(camera, title_bucket) suppression
# Used to kill the CAM4 overnight flood: 80+ webhooks with
# different UUIDs but the same underlying event type.
_bucket_cooldown: dict[str, float] = {}

# Phase.101 — single global key (the alert body always sends; this map
# only throttles the optional "🔍 VISION_CAM4ERVATIONS" Telegram block).
_vision_block_cooldown: dict[str, float] = {"_last_sent": 0.0}

# Single lock for both maps. Cooldown check + record is atomic; we never want
# a check-then-record race between threads.
_cooldown_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API — function-style cooldowns (used by notifier.notify)
# ---------------------------------------------------------------------------


def is_in_cooldown(alert_id: str, cooldown_seconds: int) -> bool:
    """Return True if alert_id was sent within cooldown_seconds, else False.

    Side effect: on a miss (not in cooldown), record the current timestamp
    so the next call within the window returns True.
    """
    now = time.time()
    with _cooldown_lock:
        last_sent = _alert_cooldown.get(alert_id)
        if last_sent is not None and (now - last_sent) < cooldown_seconds:
            return True
        _alert_cooldown[alert_id] = now
        return False


def is_in_bucket_cooldown(bucket_key: str, bucket_cooldown_seconds: int) -> bool:
    """Return True if bucket_key was sent within bucket_cooldown_seconds, else False.

    Same pattern as is_in_cooldown() but keyed by (camera, title_bucket)
    instead of alert_id. Used by notifier to kill alert floods where the
    same camera generates many webhooks with different UUIDs.
    """
    if not bucket_key:
        return False
    now = time.time()
    with _cooldown_lock:
        last_sent = _bucket_cooldown.get(bucket_key)
        if last_sent is not None and (now - last_sent) < bucket_cooldown_seconds:
            return True
        _bucket_cooldown[bucket_key] = now
        return False


def is_in_vision_block_cooldown(cooldown_seconds: int = DEFAULT_VISION_BLOCK_COOLDOWN) -> bool:
    """Global rate-limit for the per-alert "🔍 VISION_CAM4ERVATIONS" Telegram.

    Phase.101. Single key ("_last_sent") — true rate-limit, not per-camera.
    Returns True when a vision block was sent within the window (caller
    should skip the send). Returns False on a miss and records the timestamp.

    Independent of is_in_cooldown / is_in_bucket_cooldown — the alert body
    and bucket suppression use their own maps. Suppressing here only drops
    the optional vision-text Telegram; the alert body + photo always send.
    """
    now = time.time()
    with _cooldown_lock:
        last_sent = _vision_block_cooldown["_last_sent"]
        if last_sent > 0 and (now - last_sent) < cooldown_seconds:
            return True
        _vision_block_cooldown["_last_sent"] = now
        return False


def make_bucket_key(alert: dict) -> str:
    """Build a bucket key from an alert dict.

    Returns "" if the alert doesn't have the fields needed (camera, title).
    Empty key means "no bucket suppression" — caller should skip the bucket
    cooldown check.
    """
    camera = alert.get("camera_name") or alert.get("camera") or ""
    title = alert.get("title") or alert.get("alert_title") or ""
    if not camera or not title:
        return ""
    return f"{camera}:{title[:TITLE_BUCKET_PREFIX_LEN]}"


def clear_all_cooldowns() -> None:
    """Test helper. Drop all three cooldown maps.

    Never call in production — cooldowns exist specifically to suppress
    duplicate alerts across the runtime window.
    """
    with _cooldown_lock:
        _alert_cooldown.clear()
        _bucket_cooldown.clear()
        _vision_block_cooldown["_last_sent"] = 0.0


# ---------------------------------------------------------------------------
# MotionCooldown — class-style dedup for the motion+match path
# (Phase.108, extracted from listener.py 2026-08-21)
# ---------------------------------------------------------------------------


class MotionCooldown:
    """Dedup motion+match alerts by (camera, captured_at minute).

    The standard alert path has UUID-keyed cooldown via
    `notifier.is_in_cooldown`; the motion+match path needs an
    equivalent. Two webhooks for the same physical event within the
    window produce two motion alerts + two match alerts without this
    (LOGIC-FLOWS §F2.5c).

    Key shape: (camera_name: str, timestamp_minute: str)
        - timestamp_minute is the ISO timestamp truncated to "YYYY-MM-DDTHH:MM"
          — coarse enough to absorb IR-reflection re-triggers (~1-15s apart)
          but fine enough to allow legitimate separate events at HH:MM:01
          and HH:MM+1:00 to both fire.

    Window: 60 seconds (default). After the first mark, subsequent marks
    within 60s are treated as "already cooled" (returns is_cool=True).

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


# Singleton — module-level so all worker threads share state. Survives
# across _process_alert invocations but resets on listener restart
# (acceptable; /status surfaces the running count).
MOTION_COOLDOWN = MotionCooldown(window_seconds=60)