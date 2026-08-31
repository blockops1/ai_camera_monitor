"""
alert_overrides_offhours.py — Off-hours escalation safety net.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - function arg timestamp: str (required for _apply_off_hours_override,
      optional for _is_off_hours which returns False on missing input)
    - function arg vision_result: dict (required for _apply_*; structure
      matches the Vision Analysis JSON schema — primary_subject, objects_detected)
    - function arg alert: dict (required) — pre-built alert dict with
      threat_level, title, description fields

OUTPUTS:
    - return value (_is_off_hours): bool — True if timestamp falls in
      the off-hours window (20:00 – 06:00 local time)
    - return value (_vision_sees_person): bool — True if vision saw a person
    - return value (_apply_off_hours_override): dict — alert dict, possibly
      modified to escalate threat_level to OFF_HOURS_MIN_LEVEL

PUBLIC API:
    OFF_HOURS_START_HOUR = 20  (8 PM local)
    OFF_HOURS_END_HOUR = 6     (6 AM local)
    OFF_HOURS_MIN_LEVEL = 1
        Threat level any alert with off-hours person-detection must reach.
    _is_off_hours(timestamp: str) -> bool
        Return True if the timestamp falls in the off-hours window
        (20:00 – 06:00 local time). Naive timestamps (no tzinfo) are
        treated as local time. Returns False on missing/malformed input.
    _vision_sees_person(vision_result: dict) -> bool
        Return True if the vision result indicates a person is present
        in the frame. Used to decide whether the off-hours rule applies.
    _apply_off_hours_override(alert: dict, vision_result: dict,
                              timestamp: str) -> dict
        Deterministic safety net: if vision detected a person during
        off-hours and the model returned threat_level 0 (or an error),
        escalate to Level 1 and overwrite the description.

DOES NOT DO:
    - HTTP transport → infra.alert_client
    - Build the prompt → infra.alert_prompt
    - Apply baseline overrides (parked/distant/static/vision-none) →
      infra.alert_overrides_baseline
    - Decide whether a person in the frame is "suspicious" → the alert
      LLM owns that decision

WHY HERE:
    The off-hours rule is a deterministic safety net: "person detected
    at 3 AM = at least Level 1, regardless of what the LLM said." It
    MUST be applied after every alert generation (success or failure)
    because the LLM is advisory, not authoritative. Splitting the off-hours
    logic from the baseline suppressions clarifies the asymmetry:
      - off-hours = ESCALATION (move threat_level UP)
      - baselines = SUPPRESSION (move threat_level DOWN)
    Both share _is_off_hours and _vision_sees_person as inputs. These
    helpers live in this module because off-hours is the simpler
    consumer; baseline imports them.

CALLED BY:
    - infra.alert_generator.generate_alert (every call, after every LLM
      response and on error paths)

CALLS INTO:
    - logging (stdlib): log.warning on escalation events

RELATED:
    - infra.alert_overrides_baseline: sibling module that consumes
      _is_off_hours + _vision_sees_person to gate its 4 baseline rules
    - infra.heartbeat: has its own (independent) off-hours check at
      HEARTBEAT_OFF_HOURS_START (22) — different window by design,
      do not unify
"""

import logging
from datetime import datetime

# Off-hours window: property should be empty/secured. Any person detected
# during these hours is automatically escalated to at least Level 1, regardless
# of what the text model decides. This is a deterministic safety net — the LLM
# is advisory; the rule is absolute.
OFF_HOURS_START_HOUR = 20  # 8 PM
OFF_HOURS_END_HOUR = 6  # 6 AM
OFF_HOURS_MIN_LEVEL = 1

log = logging.getLogger("alert_overrides_offhours")


def _is_off_hours(timestamp: str) -> bool:
    """
    Return True if the timestamp falls in the off-hours window (20:00 – 06:00
    local time). Naive timestamps (no tzinfo) are treated as local time.

    Returns False if the timestamp cannot be parsed.

    Burned 2026-07-20: Reolink webhooks send ISO-8601 timestamps with
    an explicit `+0000` tzinfo, so `datetime.fromisoformat(timestamp).hour`
    returns the *UTC* hour on a Python process using local time elsewhere.
    Reading dt.hour directly meant a 16:25 EDT work-hour alert (20:25 UTC)
    was wrongly flagged as off-hours and escalated to Level 1. Fix:
    convert tz-aware datetimes to local before reading the hour. Naive
    timestamps stay as-is (existing behavior).
    """
    if not timestamp or not isinstance(timestamp, str):
        return False
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    # If timezone-aware (e.g. Reolink sends +00:00), convert to local time.
    # If naive, treat as local time (matches prior behavior).
    if dt.tzinfo is not None:
        local_dt = dt.astimezone()
    else:
        local_dt = dt
    hour = local_dt.hour
    # Off-hours: hour >= 20 OR hour < 6
    return hour >= OFF_HOURS_START_HOUR or hour < OFF_HOURS_END_HOUR


def _vision_sees_person(vision_result: dict) -> bool:
    """
    Return True if the vision result indicates a person is present in the frame.
    Used to decide whether the off-hours rule applies.
    """
    if not isinstance(vision_result, dict):
        return False
    primary = (vision_result.get("primary_subject") or "").strip().lower()
    if primary in ("person", "people", "man", "woman", "child", "human"):
        return True
    objects = vision_result.get("objects_detected") or []
    interesting_persons = {
        "person",
        "people",
        "man",
        "woman",
        "child",
        "human",
        "intruder",
        "suspect",
        "unidentified person",
    }
    return any((o or "").strip().lower() in interesting_persons for o in objects)


def _apply_off_hours_override(alert: dict, vision_result: dict, timestamp: str) -> dict:
    """
    Deterministic safety net: if vision detected a person during off-hours and
    the model returned threat_level 0 (or produced an error/parse failure),
    escalate to Level 1 and overwrite the description so the Telegram alert
    is clearly framed as off-hours activity.

    The model's other fields (title, recommendations) are preserved when
    available; only threat_level and description are guaranteed to reflect
    the off-hours escalation.
    """
    if not _is_off_hours(timestamp):
        return alert
    if not _vision_sees_person(vision_result):
        return alert
    current_level = alert.get("threat_level", -1)
    if current_level >= OFF_HOURS_MIN_LEVEL:
        # Model already escalated — trust it.
        return alert
    log.warning(
        f"Off-hours person detected (timestamp={timestamp}); escalating Level "
        f"{current_level} → {OFF_HOURS_MIN_LEVEL} deterministically"
    )
    alert["threat_level"] = OFF_HOURS_MIN_LEVEL
    alert["description"] = (
        f"Person detected on property during off-hours ({timestamp}). "
        "Workshop should be empty and secured between 8 PM and 6 AM. "
        + (alert.get("description") or "Vision model confirmed a person in frame.")
    ).strip()
    if not alert.get("title") or alert.get("title") == "error":
        alert["title"] = "Person detected during off-hours"
    return alert