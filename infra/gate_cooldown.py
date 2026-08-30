"""
gate_cooldown.py — Per-(camera, event_type) cooldown suppression at the gate.

STATUS: stable
THREAD SAFETY: uses threading.Lock (single internal lock guards the map)

INPUTS:
    - file config/motion_gate_thresholds.json (optional, may be absent)
        - per-camera "gate_cooldown" field, e.g.:
          "Outside Front Garage": {
            "gate_cooldown": {
              "vehicle": 60,    # seconds
              "person": 30,
              "motion": 120
            }
          }
        - duration in seconds; 0 or absent = no cooldown (backward-compatible)
        - per-camera × per-event_type; default for unknown event_type = 0
    - function arg `window_seconds: int = 0` overrides config (rare; mostly
      for testing)

OUTPUTS:
    - side effect: in-memory dict updates under a lock
    - no file writes, no network calls
    - no Telegram messages

PUBLIC API:
    is_in_gate_cooldown(camera_name: str, event_type: str, window_seconds: int = 0)
        -> tuple[bool, float]
        Return (in_cooldown, last_seen_monotonic). When `in_cooldown` is True,
        the gate should skip the entire alert pipeline for this
        (camera, event_type) — no frames, no YOLO, no Telegram. When False,
        the call records the current timestamp so a follow-up alert within
        the cooldown window is suppressed.

        `window_seconds` precedence:
          1. Explicit arg (caller-provided; rare)
          2. config/motion_gate_thresholds.json [camera][gate_cooldown][event_type]
          3. config/motion_gate_thresholds.json [camera][gate_cooldown][default]
          4. 0 (no cooldown; full backward-compatibility)

        Normalized event_type keys: "people" → "person" (Reolink payload alias
        — same convention as motion_gate_pipeline.is_gate_enabled).

    get_gate_cooldown_seconds(camera_name: str, event_type: str) -> int
        Read-only accessor that returns the resolved cooldown window for a
        (camera, event_type) without recording a timestamp. Useful for
        /status endpoints and for tests.

    clear_all_gate_cooldowns() -> None
        Test helper. Drop the map. Never call in production.

    DEFAULT_GATE_COOLDOWN_SECONDS: int
        Module-level default (0 = no cooldown; backward-compatible).

DOES NOT DO:
    - Persist cooldowns to disk → in-memory only by design; resets on restart
    - Send Telegram messages → that's the pipeline's job, not this module's
    - Decide WHICH alerts to suppress → that's the listener's job
    - Track matcher failures → that lives in infra.matcher_failures

CALLED BY:
    - listener.listener._process_alert() — early-suppression check before
      the motion gate runs. Phase.154 (PLAN §11.77).

CALLS INTO:
    - infra.paths.PROJECT_ROOT — locate motion_gate_thresholds.json
    - threading.Lock — guards the in-memory map

RELATED:
    - config/motion_gate_thresholds.json — the source of cooldown windows
    - infra/cooldown.py — sibling module for per-alert-id and per-bucket
      cooldowns (different concerns, different key shapes, different firing
      points in the pipeline). See PLAN §11.77.
    - listener/motion_gate_pipeline.py — `is_gate_enabled()` is the
      sibling per-camera × per-event-type matrix (Phase.152). This module
      is its rate-limit cousin.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("gate_cooldown")

# Module-level default (backward-compatible — opt-in via config).
DEFAULT_GATE_COOLDOWN_SECONDS = 0

# In-memory map: {(camera_name, event_type_normalized): monotonic_timestamp}
_last_seen: dict[tuple[str, str], float] = {}

_lock = threading.Lock()

# Cached config payload (reloaded on cache miss). Reset by clear_all_gate_cooldowns().
_cached_thresholds: dict | None = None


# Normalized event_type keys. "people" is the Reolink payload form; we
# collapse it to "person" for cooldown lookups (matches motion_gate_pipeline
# convention — single config key, single source of truth).
_PEOPLE_TO_PERSON = {"people": "person"}


def _normalize_event_type(event_type: str | None) -> str:
    """Normalize event_type for cooldown key lookups.

    Reolink's webhook payload uses "people" (plural) for the person-class
    motion event. The motion_gate_pipeline normalizes "people" → "person" for
    threshold lookup. We mirror that convention here so a single
    `gate_cooldown: {person: 30}` entry covers both webhook spellings.

    Unknown / None → "motion" (the most common Reolink default event).
    """
    if not event_type:
        return "motion"
    s = event_type.strip().lower()
    return _PEOPLE_TO_PERSON.get(s, s)


def _load_thresholds_config() -> dict:
    """Load config/motion_gate_thresholds.json from PROJECT_ROOT.

    Returns the parsed JSON dict. If the file is missing or malformed, returns
    an empty dict. Cached after first successful read; cache reset by
    `clear_all_gate_cooldowns()` (tests).
    """
    global _cached_thresholds
    cached = _cached_thresholds
    if cached is not None:
        return cached

    try:
        from infra.paths import PROJECT_ROOT
        config_path = Path(PROJECT_ROOT) / "config" / "motion_gate_thresholds.json"
        if not config_path.exists():
            _cached_thresholds = {}
            return {}
        with open(config_path) as f:
            loaded: dict = __import__("json").load(f)
        _cached_thresholds = loaded
        return loaded
    except Exception as e:
        log.warning(f"gate_cooldown: failed to load motion_gate_thresholds.json: {e}")
        _cached_thresholds = {}
        return {}


def get_gate_cooldown_seconds(camera_name: str, event_type: str) -> int:
    """Read-only accessor for the resolved cooldown window.

    Resolution order (first non-None wins):
      1. Per-(camera, event_type) config value
      2. Per-camera "default" config value
      3. Module-level default (0 = no cooldown)

    Returns the window in seconds. 0 means "no cooldown; do not suppress".
    Does NOT touch the in-memory map.
    """
    cfg = _load_thresholds_config()
    cam_cfg = cfg.get(camera_name, {})
    if not isinstance(cam_cfg, dict):
        return DEFAULT_GATE_COOLDOWN_SECONDS

    gc = cam_cfg.get("gate_cooldown", {})
    if not isinstance(gc, dict):
        return DEFAULT_GATE_COOLDOWN_SECONDS

    normalized = _normalize_event_type(event_type)

    # 1. exact key (normalized)
    val = gc.get(normalized)
    if isinstance(val, (int, float)) and val > 0:
        return int(val)

    # 2. per-camera default
    val = gc.get("default")
    if isinstance(val, (int, float)) and val > 0:
        return int(val)

    # 3. module default (0)
    return DEFAULT_GATE_COOLDOWN_SECONDS


def is_in_gate_cooldown(
    camera_name: str,
    event_type: str,
    window_seconds: int = 0,
) -> tuple[bool, float]:
    """Check + record cooldown for (camera, event_type).

    Returns:
      (in_cooldown, last_seen_monotonic)

      in_cooldown=True  → gate should skip the entire alert pipeline for
                          this combination (no frames, no YOLO, no Telegram).
                          Caller should log + return immediately.
      in_cooldown=False → not in cooldown; the call records `now` as the
                          last-seen timestamp so a follow-up alert within
                          the window is suppressed.

      last_seen_monotonic: the previously-recorded timestamp (0.0 if first
                           ever seen). Useful for logging "last alert N
                           seconds ago".

    Side effects:
      On a miss (in_cooldown=False), records `time.monotonic()` against the
      (camera, event_type) key. On a hit, does NOT update the timestamp —
      the cooldown clock keeps ticking from the original event.

    Thread safety:
      All map mutations + reads happen under `_lock`.

    `window_seconds` precedence:
      1. Explicit arg (caller-provided; rare; takes precedence so tests
         can inject any window without touching config)
      2. config/motion_gate_thresholds.json [camera][gate_cooldown][event_type]
      3. config/motion_gate_thresholds.json [camera][gate_cooldown][default]
      4. 0
    """
    normalized = _normalize_event_type(event_type)

    # Resolve window: caller-provided overrides config.
    if window_seconds > 0:
        window = window_seconds
    else:
        window = get_gate_cooldown_seconds(camera_name, normalized)

    key = (camera_name, normalized)
    now = time.monotonic()

    with _lock:
        last = _last_seen.get(key, 0.0)
        if window > 0 and last > 0.0:
            elapsed = now - last
            if elapsed < window:
                # Hit — return True, do NOT update timestamp (clock keeps
                # ticking from the original event so the cooldown is measured
                # from the FIRST alert of the run, not each suppressed one).
                return True, last

        # Miss — record timestamp.
        _last_seen[key] = now
        return False, last


def clear_all_gate_cooldowns() -> None:
    """Test helper. Drop the in-memory map and config cache.

    Never call in production — cooldowns exist specifically to suppress
    duplicate alerts across the runtime window. Resets on listener restart
    naturally (in-memory only).
    """
    global _cached_thresholds
    with _lock:
        _last_seen.clear()
    _cached_thresholds = None
