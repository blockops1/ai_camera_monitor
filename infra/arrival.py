"""
arrival.py — Per-camera arrival detection for the alert pipeline.

Extracted from infra.heartbeat in Phase 6B.150 (2026-08-28, PLAN §11.72).
The heartbeat thread that previously owned this logic was archived; the
per-alert arrival check stays because the alert pipeline uses it to bump
L0 → L1 on the meaningful empty→occupied transition (e.g. 9 AM arrival
after a quiet night).

STATUS: stable
THREAD SAFETY: thread-safe via threading.Lock in infra.vision_cache
    (seconds_since_last_person uses the cache lock internally)

INPUTS:
    - function arg camera_name: str (required)
    - function arg now_iso: str | None (optional, default = "now")
    - function arg gap_seconds: int | None (optional, default = ARRIVAL_GAP_SECONDS)
    - function arg vision_result: dict (required by _vision_shows_person)

OUTPUTS:
    - return value: bool

PUBLIC API:
    is_arrival(camera_name, now_iso=None, gap_seconds=None) -> bool
        True if a person was last seen on `camera_name` more than ARRIVAL_GAP_SECONDS
        ago (or never). Used by emit_result_stage to bump L0 → L1.
    _vision_shows_person(vision_result) -> bool
        Decide whether a cached vision result indicates a person is present.

DOES NOT DO:
    - Cache vision results — infra.vision_cache owns that
    - Schedule a thread — that was the heartbeat's job, archived in 6B.150
    - Send Telegram — alert routing is the pipeline's job

WHY HERE:
    Two pieces of logic, both of which run per-alert to decide whether the
    current detection is "the first person in a long while" worth bumping
    to a higher threat level. Kept separate from the heartbeat archive
    so the alert pipeline can keep importing them.

CALLED BY:
    - listener.vehicle_event_pipeline: is_arrival() / _vision_shows_person()
      in emit_result_stage (stage 6)

CALLS INTO:
    - infra.vision_cache: seconds_since_last_person()

RELATED:
    - data/last_person_seen.json — the person-seen log (read-only)
    - infra/vision_cache.py — owns the cached state this module reads
"""

import logging

from infra.vision_cache import seconds_since_last_person

# Arrival gap: if this much time has passed since we last saw a person on a
# camera, the next person-detection is treated as an "arrival" and bumps to L1.
# 4 hours is the sweet spot: long enough that normal workday motion (with
# occasional gaps from standing still) doesn't trip it, short enough that a
# morning arrival / late return / overnight visit fires reliably.
ARRIVAL_GAP_SECONDS = 4 * 3600  # 4 hours

log = logging.getLogger("arrival")


def is_arrival(
    camera_name: str, now_iso: str | None = None, gap_seconds: int | None = None
) -> bool:
    """
    True if this is the first person-detection on `camera_name` in a long while.

    "Long while" defaults to ARRIVAL_GAP_SECONDS (4 hours). Used by the motion
    pipeline to bump L0 → L1 on the meaningful empty→occupied transition (e.g.
    the 9 AM arrival after a quiet night), without nagging during normal work.
    """
    if gap_seconds is None:
        gap_seconds = ARRIVAL_GAP_SECONDS
    elapsed = seconds_since_last_person(camera_name, now_iso=now_iso)
    if elapsed is None:
        # No previous record — first ever detection → arrival
        return True
    return elapsed >= gap_seconds


def _vision_shows_person(vision_result: dict) -> bool:
    """
    Decide whether a cached vision result indicates a person is present.

    Returns True if:
        - primary_subject looks like a person ("person", "man", "woman", etc.)
        - objects_detected contains "person", "people", or "human"
    Returns False otherwise (empty room, animals only, error result, no data).

    The function does NOT just check "is primary_subject non-empty" because the
    vision model reports things like "dog" or "car" as primary subjects, and
    we don't want heartbeat alerts for those.
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
        # Person indicators in the primary subject string
        person_keywords = (
            "person",
            "people",
            "human",
            "man",
            "woman",
            "boy",
            "girl",
            "child",
            "worker",
            "operator",
        )
        if any(kw in primary_clean for kw in person_keywords):
            return True

    # Either primary_subject is "none"/empty, OR it's something like "dog" but
    # the objects list might still contain "person" as a secondary subject.
    # In both cases, fall back to checking objects_detected.
    if isinstance(objects, list):
        for obj in objects:
            if isinstance(obj, str) and obj.lower() in ("person", "people", "human"):
                return True

    return False
