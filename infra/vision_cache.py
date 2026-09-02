"""
vision_cache.py — Cached vision-result + person-seen state.

STATUS: stable
THREAD SAFETY: uses threading.Lock for in-process cache writes;
    reads are pure (atomic JSON file reads)

INPUTS:
    - file data/last_vision.json — atomic-write JSON cache of the most
      recent vision result per camera
    - file data/last_person_seen.json — atomic-write ISO timestamp of
      the most recent person-seen event per camera
    - function args: vision_result dicts, camera_name, ISO timestamps

OUTPUTS:
    - return value: dict | None | str | float (varies per function)
    - writes file: data/last_vision.json (atomic: tmp + os.replace)
    - writes file: data/last_person_seen.json (atomic: tmp + os.replace)
    - log line per cache write (warning level on disk failure)

PUBLIC API:
    set_last_vision(camera_name, vision_result, frame_path=None, timestamp=None) -> None
        Cache the most recent vision result for a camera.
    get_last_vision(camera_name) -> dict | None
        Read the cached vision result. None if missing.
    get_all_cached_vision() -> dict
        Return the entire cache (all cameras).
    clear_last_vision(camera_name=None) -> None
        Drop one camera's entry (or all if None). Test-only.
    record_person_seen(camera_name, when_iso=None) -> None
        Stamp "person seen at <when>" for a camera.
    get_last_person_seen(camera_name) -> str | None
        ISO timestamp of last person-seen, or None.
    seconds_since_last_person(camera_name, now_iso=None) -> float | None
        Seconds since last person-seen, or None if never.
    clear_last_person_seen(camera_name=None) -> None
        Drop one camera's person-seen record (or all). Test-only.

DOES NOT DO:
    - Decide whether to alert — infra.heartbeat owns emission policy
      (off-hours, freshness window, confidence floor)
    - Network calls to LLM or Telegram — only persists what callers pass
    - Capture frames — infra.frame_capture owns that
    - Long-term retention — files overwritten atomically on each update

WHY HERE:
    Two related state caches that share file-I/O patterns (atomic JSON,
    same data directory, same threading.Lock) and are written from the
    same call site (post-vision-analysis in the motion pipeline). They
    live together because pulling them apart would force duplicated
    file-I/O code or a third helper module to share infrastructure.
    The heartbeat emitter reads from these caches.

CALLED BY:
    - listener.listener: set_last_vision() after every analyze_frames()
    - listener.listener: get_last_vision() for /status
    - listener.listener: record_person_seen() after every vision pass
    - infra.heartbeat: get_all_cached_vision() per top-of-hour check
    - infra.heartbeat: get_last_person_seen() / seconds_since_last_person()

CALLS INTO:
    - infra.paths: DATA_DIR (for cache files)
    - threading.Lock: in-process cache guard
    - json, os.replace: atomic disk writes

RELATED:
    - data/last_vision.json — the vision cache file
    - data/last_person_seen.json — the person-seen log
"""

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# Where to persist the cache. Lives next to data/frames/ so it's part of normal
# backup scope, not in config (config is for things the user changes).
# Uses infra.paths.DATA_DIR — this repo's DATA_DIR is the refactor tree,
# NOT ~/ai_camera_monitor/data. Per AGENTS.md §3 isolation rule.
from infra.paths import DATA_DIR as _DATA_DIR
from infra.timezone import parse_iso as _parse_iso  # promoted from here (Phase 6B.99)

DATA_DIR = _DATA_DIR  # keep name for callers that import DATA_DIR from here
CACHE_FILE = os.path.join(DATA_DIR, "last_vision.json")
PERSON_SEEN_FILE = os.path.join(DATA_DIR, "last_person_seen.json")

# Lock so concurrent Flask threads don't corrupt the JSON file
_cache_lock = threading.Lock()

log = logging.getLogger("vision_cache")


def _ensure_data_dir() -> None:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)


def _read_cache_file() -> dict[str, Any]:
    """Read the on-disk cache. Returns {} if file missing or corrupt."""
    try:
        with open(CACHE_FILE, "r") as f:
            return cast(dict[str, Any], json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cache_file(cache: dict) -> None:
    """Atomically write the cache to disk. Uses temp file + rename."""
    _ensure_data_dir()
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, CACHE_FILE)


def set_last_vision(
    camera_name: str,
    vision_result: dict,
    frame_path: str | None = None,
    timestamp: str | None = None,
) -> None:
    """
    Record the latest vision result for a camera. Called after every successful
    vision analysis in the motion pipeline.

    Args:
        camera_name: Friendly name (e.g. "<FRIENDLY_NAME>")
        vision_result: The vision output dict (objects_detected, primary_subject, etc.)
        frame_path: Path to the frame the vision ran on (used as the alert photo)
        timestamp: When the event happened. Defaults to now.
    """
    if timestamp is None:
        timestamp = datetime.now(UTC).astimezone().isoformat()

    with _cache_lock:
        cache = _read_cache_file()
        cache[camera_name] = {
            "timestamp": timestamp,
            "vision_result": vision_result,
            "frame_path": frame_path,
            "saved_at": datetime.now(UTC).astimezone().isoformat(),
        }
        try:
            _write_cache_file(cache)
        except OSError as e:
            log.warning(f"Failed to persist last_vision cache: {e}")


def get_last_vision(camera_name: str) -> dict | None:
    """
    Retrieve the cached vision result for a camera. Returns None if no cache exists.
    """
    with _cache_lock:
        cache = _read_cache_file()
        return cache.get(camera_name)


def get_all_cached_vision() -> dict:
    """Return all cached vision results, keyed by camera name."""
    with _cache_lock:
        return dict(_read_cache_file())


def clear_last_vision(camera_name: str | None = None) -> None:
    """
    Clear cache. Used by tests. If camera_name is None, clears all.
    """
    with _cache_lock:
        if camera_name is None:
            try:
                os.remove(CACHE_FILE)
            except FileNotFoundError:
                pass
        else:
            cache = _read_cache_file()
            cache.pop(camera_name, None)
            try:
                _write_cache_file(cache)
            except OSError:
                pass


# ----------------------------------------------------------------------
# Person-seen tracking — used to detect "arrival after a long quiet period"
# ----------------------------------------------------------------------


def _read_person_seen_file() -> dict[str, str]:
    """Read last-person-seen timestamps keyed by camera. {} if missing/corrupt."""
    try:
        with open(PERSON_SEEN_FILE, "r") as f:
            return cast(dict[str, str], json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_person_seen_file(data: dict) -> None:
    _ensure_data_dir()
    tmp = PERSON_SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PERSON_SEEN_FILE)


def record_person_seen(camera_name: str, when_iso: str | None = None) -> None:
    """
    Mark that a person was seen on `camera_name` at `when_iso` (default: now).

    Called by the motion pipeline after every successful vision analysis. Only
    stores the timestamp — the vision result itself lives in last_vision.json.
    """
    if when_iso is None:
        when_iso = datetime.now(UTC).astimezone().isoformat()
    with _cache_lock:
        data = _read_person_seen_file()
        data[camera_name] = when_iso
        try:
            _write_person_seen_file(data)
        except OSError as e:
            log.warning(f"Failed to persist last_person_seen: {e}")


def get_last_person_seen(camera_name: str) -> str | None:
    """Return ISO timestamp of last person-detection on this camera, or None."""
    with _cache_lock:
        data = _read_person_seen_file()
        return data.get(camera_name)


def seconds_since_last_person(
    camera_name: str, now_iso: str | None = None
) -> float | None:
    """
    Seconds since this camera last saw a person. Returns None if we've never
    seen a person on this camera (first-time detection → treat as arrival).
    """
    last = get_last_person_seen(camera_name)
    if last is None:
        return None
    if now_iso is None:
        now_iso = datetime.now(UTC).astimezone().isoformat()
    try:
        last_dt = _parse_iso(last)
        now_dt = (
            _parse_iso(now_iso) if now_iso else datetime.now(UTC).astimezone()
        )
        if last_dt is None or now_dt is None:
            return None
        return (now_dt - last_dt).total_seconds()
    except (ValueError, TypeError):
        return None


def clear_last_person_seen(camera_name: str | None = None) -> None:
    """Clear person-seen records. If camera_name is None, clears all."""
    with _cache_lock:
        if camera_name is None:
            try:
                os.remove(PERSON_SEEN_FILE)
            except FileNotFoundError:
                pass
        else:
            data = _read_person_seen_file()
            data.pop(camera_name, None)
            try:
                _write_person_seen_file(data)
            except OSError:
                pass