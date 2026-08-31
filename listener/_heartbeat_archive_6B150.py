"""
_heartbeat_archive_6B150.py — Heartbeat thread (ARCHIVED 2026-08-28).

the operator 2026-08-28: "let's remove heartbeat and the notifier out of
the application, and archive the script. We may want to referred to
them later but probably not."

This is the original `infra/heartbeat.py` with the per-alert arrival
helpers (`is_arrival`, `_vision_shows_person`) removed — those moved
to `infra/arrival.py` (Phase.150, PLAN §11.72) because the alert
pipeline still needs them.

What's archived here:
    - `build_heartbeat_alert()` — compose the heartbeat alert dict
    - `_is_heartbeat_off_hours()` — 22:00–06:00 suppression window
    - `is_top_of_hour_due()` — top-of-hour firing window
    - `_seconds_until_next_hour()` — sleep helper
    - `run_heartbeat_check()` — one heartbeat cycle (calls
      `infra.notifier.notify` as the default `send_fn`)
    - `start_heartbeat_thread()` — daemon thread that runs forever

Why archived: the operator locked in the pipeline shape "only webhook →
gate → vehicle/person pipeline, no other identification or
activity" (PLAN §11.73). The hourly re-evaluation Telegram is
neither, so it's gone.

To restore: `git mv listener/_heartbeat_archive_6B150.py
infra/heartbeat.py`, then re-add the `start_heartbeat_thread(...)`
call in `listener/listener.py` bootstrap (look for the
"Heartbeat thread started" log line).

STATUS: archived (was: stable)
THREAD SAFETY: uses threading.Lock (re-exported from vision_cache);
    the heartbeat thread is daemon=True and serializes its own work

INPUTS:
    - file data/last_vision.json — read via infra.vision_cache.get_all_cached_vision()
    - file data/last_person_seen.json — read via infra.vision_cache.get_last_person_seen()
    - env vars: none (caller supplies bot_token + chat_id)
    - function args: cached vision dicts, timestamps, callable for sending

OUTPUTS:
    - return value: list[dict] from run_heartbeat_check (one entry per camera)
    - side effect (when restored): a daemon thread fires every hour and may
      produce a Telegram alert via the caller-supplied send_fn

WHY HERE (historical):
    Reolink cameras push motion-triggered frames. After every motion event the
    listener runs vision analysis and caches the result via infra.vision_cache.
    This module re-evaluated that cache on the hour: if the cached vision says
    "person" (or any non-trivial subject) it sent an L1 alert — catching
    long-duration situations the motion-only model missed (e.g. someone
    standing still for 30+ minutes after motion stopped).

CALLED BY (when restored):
    - listener.listener: start_heartbeat_thread() at bootstrap

CALLS INTO (when restored):
    - infra.vision_cache: get_all_cached_vision(), get_last_person_seen(),
      seconds_since_last_person()
    - infra.notifier (lazy import, called from run_heartbeat_check via send_fn)
    - infra.paths: CLEANUP_INTERVAL_S

RELATED:
    - infra/arrival.py — owns the per-alert arrival helpers that USED to be
      in this module (is_arrival, _vision_shows_person). Extracted 6B.150.
    - infra/vision_cache.py — owns the cached state this module reads
    - data/last_vision.json — the cache file (read-only from this module)
    - data/last_person_seen.json — the person-seen log (read-only)
"""

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from infra.vision_cache import (
    _parse_iso,
    get_all_cached_vision,
)

# Heartbeat freshness window. The cached vision must have been written within
# this window for the heartbeat to alert — otherwise we assume the room is empty.
# Rationale: Reolink motion events fire constantly when someone is in the
# workshop (name one: "I'm always moving around"), so a fresh cache = present.
# A stale cache = person left and motion stopped, no need to nag.
# 5 min is comfortable: motion events fire every ~30s when someone is present,
# and the heartbeat fires hourly, so freshness is rarely borderline.
FRESHNESS_WINDOW_SECONDS = 300  # 5 minutes

# Confidence floor for heartbeat alerts. Cached vision must have at least
# this confidence to re-fire a heartbeat. Phase.14: defense-in-depth so a
# low-confidence synthetic/test entry can't spam Telegram even if it leaks
# into the cache.
HEARTBEAT_MIN_CONFIDENCE = 0.85

# Off-hours window: heartbeat is suppressed entirely during this window.
# Phase.14 — the operator: "the heartbeat is also a contributor to spam at night
# when nothing has fired." Vehicle-state alerts (Phase.11) already cover
# overnight real events; heartbeat is redundant after dark.
HEARTBEAT_OFF_HOURS_START = 22  # 22:00 local
HEARTBEAT_OFF_HOURS_END = 6    # 06:00 local

log = logging.getLogger("heartbeat")


def _vision_shows_person(vision_result: dict) -> bool:
    """
    Heartbeat-specific copy of `infra.arrival._vision_shows_person`.

    Kept inline here so this archive file stays self-contained (it can
    never import from infra.arrival because the archive is not on the
    import path). Behavior is identical to the canonical version.
    """
    if not vision_result or not isinstance(vision_result, dict):
        return False
    objects = vision_result.get("objects_detected", [])
    if isinstance(objects, list) and "error" in objects:
        return False
    primary = vision_result.get("primary_subject", "")
    primary_clean = ""
    if isinstance(primary, str) and primary.strip():
        primary_clean = primary.strip().lower()
        person_keywords = (
            "person", "people", "human", "man", "woman",
            "boy", "girl", "child", "worker", "operator",
        )
        if any(kw in primary_clean for kw in person_keywords):
            return True
    if isinstance(objects, list):
        for obj in objects:
            if isinstance(obj, str) and obj.lower() in ("person", "people", "human"):
                return True
    return False


def build_heartbeat_alert(
    camera_name: str,
    cached: dict,
    now_iso: str | None = None,
) -> dict:
    """
    Build an L1 alert from a cached vision result. Bypasses the text classifier
    because the cached vision is already a confirmed observation.

    The alert:
        - threat_level = 1 (warning — always escalate when a person is seen)
        - title = "Hourly Check — Person Still Present in {camera}"
        - description includes how stale the cached vision is (e.g. "last seen 47m ago")
        - source = "heartbeat" (so you can tell apart from motion alerts)
        - recommendations = ["Verify situation", "Check live feed if unexpected"]

    Args:
        camera_name: Friendly camera name
        cached: The cached dict from get_last_vision()
        now_iso: Current time for staleness calculation. Defaults to now.

    Returns:
        A dict matching the alert schema.
    """
    if now_iso is None:
        now_iso = datetime.now(UTC).astimezone().isoformat()

    vision_result = cached.get("vision_result", {})
    saved_at = cached.get("saved_at", now_iso)
    frame_path = cached.get("frame_path")

    # How stale is this cached vision?
    try:
        saved_dt = datetime.fromisoformat(saved_at)
        now_dt = datetime.fromisoformat(now_iso)
        stale_seconds = (now_dt - saved_dt).total_seconds()
        if stale_seconds < 60:
            stale_str = "just now"
        elif stale_seconds < 3600:
            stale_str = f"{int(stale_seconds / 60)}m ago"
        elif stale_seconds < 86400:
            stale_str = f"{stale_seconds / 3600:.1f}h ago"
        else:
            stale_str = f"{stale_seconds / 86400:.1f}d ago"
    except (ValueError, TypeError):
        stale_str = "unknown"

    primary_subject = vision_result.get("primary_subject", "person")
    scene = vision_result.get("scene_description", "")

    title = f"Hourly Check — Person Still Present in {camera_name}"
    description = (
        f"At the top of the hour, we re-checked the cached vision for {camera_name}. "
        f"The last camera-triggered analysis ({stale_str}) reported a "
        f"{primary_subject} in the scene"
    )
    if scene:
        description += f':\n\n"{scene}"'
    description += (
        "\n\nThis is an hourly safety check — the camera's motion sensor hasn't "
        "fired since, so we re-evaluated the last known state to make sure "
        "you're aware the situation is still ongoing."
    )

    return {
        # Stable per camera — no timestamp. The notifier applies a 10-min
        # cooldown keyed on alert_id, so two heartbeats 30s apart for the
        # same camera must produce the same alert_id and the second one
        # gets suppressed. Including int(time.time()) here broke that
        # dedup and caused duplicate hourly notifications (bug 2026-07-21).
        "alert_id": f"heartbeat-{camera_name.replace(' ', '_')}",
        "camera": camera_name,
        "timestamp": now_iso,
        "threat_level": 1,
        "title": title,
        "description": description,
        "recommendations": [
            "Verify the situation — check live feed if unexpected",
            "Confirm with on-site contact if available",
        ],
        "vision_summary": f"{primary_subject} (cached, {stale_str})",
        "source": "heartbeat",
        "frame_path": frame_path,
        "cached_vision": vision_result,
    }


def _is_heartbeat_off_hours(now: datetime | None = None) -> bool:
    """
    Phase.14 — Return True if `now` falls in the heartbeat off-hours
    window (HEARTBEAT_OFF_HOURS_START .. HEARTBEAT_OFF_HOURS_END, local
    time). Off-hours means heartbeat is suppressed entirely; vehicle-state
    alerts still fire (they're a different system).

    Window wraps midnight: e.g. 22:00 .. 06:00 covers 22, 23, 0, 1, 2, 3, 4, 5.

    Args:
        now: Defaults to local time now. Accepts a tz-aware or naive
             datetime; if tz-aware, converts to local before reading the
             hour (mirrors `_is_off_hours` in alert_generator.py to avoid
             the UTC-vs-local trap from the 2026-07-20 burn).
    """
    if now is None:
        now = datetime.now(UTC).astimezone()
    if now.tzinfo is not None:
        local_hour = now.astimezone().hour
    else:
        local_hour = now.hour
    if HEARTBEAT_OFF_HOURS_START > HEARTBEAT_OFF_HOURS_END:
        # Wraps midnight (e.g. 22 -> 6)
        return (
            local_hour >= HEARTBEAT_OFF_HOURS_START
            or local_hour < HEARTBEAT_OFF_HOURS_END
        )
    # Same-day window (e.g. 9 -> 17)
    return HEARTBEAT_OFF_HOURS_START <= local_hour < HEARTBEAT_OFF_HOURS_END


def is_top_of_hour_due(
    now: datetime | None = None, window_seconds: int = 60
) -> bool:
    """
    Decide if the heartbeat should fire now.

    We fire at the top of every hour. To be tolerant of scheduler drift, we
    accept a 60s window after the hour mark. (The background loop runs every
    30s, so we'll catch it within 30s of the top of the hour.)

    Args:
        now: Current time. Defaults to local time.
        window_seconds: How long after the hour mark we still consider it "on the hour".
    """
    if now is None:
        now = datetime.now(UTC).astimezone()
    return now.minute == 0 and now.second < window_seconds


def _seconds_until_next_hour(now: datetime | None = None) -> float:
    """How many seconds until the top of the next hour."""
    if now is None:
        now = datetime.now(UTC).astimezone()
    # Seconds remaining in this hour
    return (60 - now.minute) * 60 - now.second


def run_heartbeat_check(
    bot_token: str,
    chat_id: str,
    cooldown_seconds: int = 600,  # 10 min — heartbeat shouldn't double-fire
    send_fn: Callable | None = None,
    on_alert_built: Callable | None = None,
    now_iso: str | None = None,
) -> list:
    """
    Iterate all cached vision results. For each camera whose cached vision
    shows a person, build a heartbeat alert and send it to Telegram.

    Args:
        bot_token: Telegram bot token
        chat_id: Telegram chat ID
        cooldown_seconds: Per-alert cooldown so heartbeat doesn't spam within an hour
        send_fn: Function to call for sending (defaults to notifier.notify)
        on_alert_built: Optional callback(alert) — called for each alert BEFORE send
        now_iso: Override current time for tests

    Returns:
        List of dicts: each entry has {camera, alert_id, sent: bool, reason: str}
    """
    if send_fn is None:
        from infra.notifier import notify

        send_fn = notify

    if now_iso is None:
        now_iso = datetime.now(UTC).astimezone().isoformat()

    # Phase.14 — short-circuit during off-hours. The heartbeat is a
    # "remind me this is still going" feature, not an hourly nag; overnight
    # vehicle-state alerts (Phase.11) already cover real events. the operator
    # flagged the overnight 22:00/00:00/etc. Telegrams as pure noise.
    now_dt = _parse_iso(now_iso) or datetime.now(UTC).astimezone()
    if _is_heartbeat_off_hours(now_dt):
        log.info(
            f"[heartbeat] suppressed: off-hours "
            f"({HEARTBEAT_OFF_HOURS_START:02d}:00–"
            f"{HEARTBEAT_OFF_HOURS_END:02d}:00 local)"
        )
        return [{
            "camera": "*",
            "alert_id": None,
            "sent": False,
            "reason": "off-hours suppression",
        }]

    cached_all = get_all_cached_vision()
    results = []

    for camera_name, cached in cached_all.items():
        vision_result = cached.get("vision_result", {})

        if not _vision_shows_person(vision_result):
            log.debug(
                f"[heartbeat] {camera_name}: no person in cached vision, skipping"
            )
            results.append(
                {
                    "camera": camera_name,
                    "alert_id": None,
                    "sent": False,
                    "reason": "no person in cached vision",
                }
            )
            continue

        # Phase.14 — confidence floor. Defense-in-depth: even if a stale
        # or low-quality record leaks into the cache (e.g. a test fixture
        # that bypassed _isolate_heartbeat_cache, or a synthetic webhook),
        # a low-confidence or ungraded person sighting won't re-fire.
        confidence = vision_result.get("confidence")
        if confidence is None or confidence < HEARTBEAT_MIN_CONFIDENCE:
            log.info(
                f"[heartbeat] {camera_name}: cached vision confidence "
                f"{'missing' if confidence is None else f'{confidence:.2f}'} "
                f"< {HEARTBEAT_MIN_CONFIDENCE} floor, skipping"
            )
            results.append(
                {
                    "camera": camera_name,
                    "alert_id": None,
                    "sent": False,
                    "reason": (
                        f"low confidence ({'missing' if confidence is None else f'{confidence:.2f}'} "
                        f"< {HEARTBEAT_MIN_CONFIDENCE})"
                    ),
                }
            )
            continue

        # Freshness check: if the cached vision is stale, assume the room is empty.
        # Motion events fire constantly when someone is present, so an old cache
        # means they left. Without this, heartbeat would keep alerting after departure.
        # Uses `timestamp` (when the event happened), not `saved_at` (when we wrote it),
        # because in production they're ~equal, but timestamp is the semantic truth.
        try:
            ts = cached.get("timestamp") or cached.get("saved_at") or now_iso
            saved_dt = _parse_iso(ts)
            if now_iso:
                parsed_now: datetime | None = _parse_iso(now_iso)
            else:
                parsed_now = datetime.now(UTC).astimezone()
            assert parsed_now is not None
            now_dt = parsed_now
            if saved_dt is None or now_dt is None:
                raise ValueError(
                    f"unparseable timestamp: ts={ts!r} now_iso={now_iso!r}"
                )
            age_seconds = (now_dt - saved_dt).total_seconds()
            if age_seconds > FRESHNESS_WINDOW_SECONDS:
                results.append(
                    {
                        "camera": camera_name,
                        "alert_id": None,
                        "sent": False,
                        "reason": f"cached vision stale ({age_seconds / 60:.0f}m old, "
                        f"window {FRESHNESS_WINDOW_SECONDS}s) — likely departed",
                    }
                )
                continue
        except (ValueError, TypeError):
            # Bad timestamp — skip safely
            results.append(
                {
                    "camera": camera_name,
                    "alert_id": None,
                    "sent": False,
                    "reason": "cached vision has no valid timestamp — skipping",
                }
            )
            continue

        # Build the heartbeat alert
        alert = build_heartbeat_alert(camera_name, cached, now_iso=now_iso)

        if on_alert_built:
            try:
                on_alert_built(alert)
            except Exception as e:
                log.warning(f"[heartbeat] on_alert_built callback raised: {e}")

        # Send via the standard notifier (3-message flow)
        try:
            sent = send_fn(
                alert=alert,
                bot_token=bot_token,
                chat_id=chat_id,
                cooldown_seconds=cooldown_seconds,
                vision_result=vision_result,
            )
            log.info(f"[heartbeat] {camera_name}: sent={sent}")
            results.append(
                {
                    "camera": camera_name,
                    "alert_id": alert["alert_id"],
                    "sent": sent,
                    "reason": "person detected in cached vision",
                }
            )
        except Exception as e:
            log.exception(f"[heartbeat] {camera_name}: send failed")
            results.append(
                {
                    "camera": camera_name,
                    "alert_id": alert["alert_id"],
                    "sent": False,
                    "reason": f"send failed: {e}",
                }
            )

    return results


def start_heartbeat_thread(
    bot_token: str,
    chat_id: str,
    cooldown_seconds: int = 600,
) -> threading.Thread:
    """
    Start the background heartbeat thread. Runs forever.

    Behavior:
        - Sleep until the top of the next hour
        - Run heartbeat check
        - Sleep 30s, check again (in case we're still in the top-of-hour window)
        - Sleep until next hour
        - Repeat

    The thread is daemon=True so it dies when the listener exits.

    Args:
        bot_token, chat_id: Telegram creds
        cooldown_seconds: Per-alert cooldown

    Returns:
        The Thread object (mostly for tests; daemon thread dies with process).
    """

    def _run():
        # Wait until first top-of-hour
        wait = _seconds_until_next_hour()
        log.info(f"[heartbeat] thread started, sleeping {wait:.0f}s until next hour")
        time.sleep(wait)

        # Track whether the next loop iteration needs to fire. The loop
        # polls every 30s, but the top-of-hour window is 60s wide — without
        # this flag, the body ran once at minute=0 and again 30s later when
        # minute was still 0, producing duplicate hourly notifications
        # (bug observed 2026-07-21). We just slept into the top-of-hour
        # window so the first iteration must fire.
        need_to_fire = True

        while True:
            try:
                if need_to_fire:
                    results = run_heartbeat_check(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        cooldown_seconds=cooldown_seconds,
                    )
                    sent_count = sum(1 for r in results if r.get("sent"))
                    log.info(
                        f"[heartbeat] top-of-hour check: {sent_count} alerts sent "
                        f"across {len(results)} cameras"
                    )
                    need_to_fire = False
                else:
                    log.debug("[heartbeat] already fired this hour, skipping")
            except Exception:
                log.exception("[heartbeat] top-of-hour check failed")

            # Poll every 30s. While we stay in the top-of-hour window
            # (minute == 0), we keep skipping. When we leave it, mark
            # need_to_fire so the NEXT top-of-hour iteration fires.
            time.sleep(30)

            now = datetime.now(UTC).astimezone()
            if now.minute == 0:
                # Still in the top-of-hour window — keep skipping.
                continue
            # Left the top-of-hour window. The next loop iteration must fire.
            need_to_fire = True
            wait = _seconds_until_next_hour()
            log.debug(f"[heartbeat] sleeping {wait:.0f}s until next hour")
            time.sleep(wait)

    thread = threading.Thread(target=_run, name="heartbeat", daemon=True)
    thread.start()
    return thread