"""
alert_overrides_baseline.py — 4 baseline-noise suppressions for the alert pipeline.

STATUS: stable
THREAD SAFETY: thread-safe (config loaded once at import, sets are
    frozenset-immutable, all functions are pure)

INPUTS:
    - config/alert_overrides.json (required) — per-camera baseline sets
    - function arg alert: dict (required) — pre-built alert dict with
      threat_level, title, description, suppressed_by fields
    - function arg vision_result: dict (required) — Vision Analysis JSON
    - function arg camera_name: str (required) — used to look up the
      baseline camera set
    - function arg timestamp: str | None (optional) — guards off-hours-only
      overrides; missing = skip the override (conservative)

OUTPUTS:
    - return value (_apply_*_baseline_override): dict — alert dict, possibly
      modified to downgrade threat_level from 1 → 0 and stamp suppressed_by
    - side effect: raises FileNotFoundError / TypeError if
      config/alert_overrides.json is missing or malformed at import time
      (intentional — listener won't start without valid config)

PUBLIC API:
    _apply_parked_vehicle_baseline_override(alert, vision_result,
        camera_name, timestamp=None) -> dict
        Downgrade L1 → L0 on parking-area cameras when vision did NOT
        describe a person (kills the IR-reflection false-positive class).
        Off-hours only.
    _apply_distant_vehicle_baseline_override(alert, vision_result,
        camera_name, timestamp=None) -> dict
        Downgrade L1 → L0 when vision sees a vehicle but flags it as
        ambiguous / distant / reflection (the north-pasture-road headlights
        pattern). Off-hours only.
    _apply_static_object_baseline_override(alert, vision_result,
        camera_name, timestamp=None) -> dict
        Downgrade L1 → L0 on cameras where static environmental objects
        (tarp, equipment, debris, distant county-road lights) are the
        baseline state. Off-hours only.
    _apply_vision_none_baseline_override(alert, vision_result,
        camera_name, timestamp=None) -> dict
        Downgrade L1 → L0 on cameras where vision LLM consistently
        returns primary_subject='none' (saw nothing identifiable) but
        the alert LLM fabricates an L1 verdict. Off-hours only.
    _apply_baseline_overrides(alert, vision_result, camera_name,
                              timestamp=None) -> dict
        Apply all 4 baseline overrides in sequence. Idempotent (first
        demote wins).
    _get_parked_vehicle_cameras() -> frozenset[str]
    _get_distant_vehicle_cameras() -> frozenset[str]
    _get_static_object_cameras() -> frozenset[str]
    _get_vision_none_cameras() -> frozenset[str]
        Per-camera baseline set getters (loaded from config once at import).

DOES NOT DO:
    - HTTP transport → infra.alert_client
    - Build the prompt → infra.alert_prompt
    - Apply off-hours escalation → infra.alert_overrides_offhours
      (note: off-hours and baselines share _is_off_hours + _vision_sees_person;
      those helpers live in offhours module, this module imports them)
    - Override L2 — real critical threats always pass through
    - Override L1 when a person is detected — that scenario is genuinely
      suspicious per the camera's guardrail

WHY HERE:
    Four distinct noise patterns target four different failure modes:
      1. parked_vehicle_baseline — IR-reflection hallucination on parking
         cameras. Vision sees parked vehicles; alert LLM invents "headlights
         on" / "active vehicle" from IR reflections off headlight lenses.
      2. distant_vehicle_baseline — vision sees a vehicle but flags it as
         ambiguous (faint, indistinct, reflection, light source, across
         the road, etc.).
      3. static_object_baseline — environmental static noise (tarp,
         equipment, debris, distant lights) elevated to L1.
      4. vision_none_baseline — vision returns primary_subject='none'
         but alert LLM fabricates a generic L1 verdict.
    Each rule needs its own camera set (different cameras have different
    noise profiles) and its own implementation (different vision signals
    distinguish them). Keeping them in one module because they share
    config-loading + dispatch + suppression shape — splitting would force
    a config-loader helper duplicated 4 times.

    Order matters only for the suppressed_by marker; threat_level transition
    is idempotent once demoted to 0. Apply cheapest first:
    parked_vehicle → distant_vehicle → static_object → vision_none.

CALLED BY:
    - infra.alert_generator.generate_alert (every call, after off-hours
      override, on every LLM response and on error paths)

CALLS INTO:
    - infra.alert_overrides_offhours: _is_off_hours + _vision_sees_person
    - logging (stdlib): log.info on each override applied
    - json (stdlib): load config/alert_overrides.json
    - pathlib (stdlib): resolve config path

RELATED:
    - config/alert_overrides.json — the file loaded at import time.
      Adds/removes don't take effect until listener restart.
    - infra.alert_overrides_offhours: sibling module that consumes the
      same _is_off_hours + _vision_sees_person helpers for escalation
"""

import json
import logging
from pathlib import Path

from infra.alert_overrides_offhours import (
    _is_off_hours,
    _vision_sees_person,
)

log = logging.getLogger("alert_overrides_baseline")

# Cameras where a parked vehicle is the BASELINE state, not an anomaly.
# Per-camera baseline override sets are loaded from config/alert_overrides.json
# at module import time. Add a camera to the relevant list in that file
# (and restart the listener) to suppress its baseline-noise L1 alerts.
#
# Three baseline sets address DIFFERENT noise patterns:
#
# 1. parked_vehicle_baseline — IR-reflection hallucination on cameras
#    looking at the regular parking area. Vision sees parked vehicles;
#    alert LLM invents "headlights on" / "active vehicle" from IR
#    reflections off headlight lenses. Suppress when vision sees no person.
#
# 2. static_object_baseline — environmental static noise (tarp, equipment,
#    debris, distant lights from county road, distant headlights). LLM
#    elevates these to L1 because it doesn't know they're background.
#    Suppress when vision sees no person. Person → preserve.
#
# 3. vision_none_baseline — vision LLM returns primary_subject='none'
#    (saw nothing identifiable in the frame) but alert LLM fabricates an
#    L1 verdict from the empty signal — titles like "Nighttime Activity
#    at <location>", "Unknown activity at <location> during night hours",
#    "Faint motion detected at night", "Nighttime Motion Detected Near
#    Coop". Suppress when vision's primary_subject is 'none' AND
#    objects_detected has no person/vehicle. Person/vehicle in vision →
#    preserve.
#
# All three sets NEVER override L2 — real critical threats (weapons, forced
# entry) always pass through.
_CONFIG_PATH = Path(__file__).parent.parent / "config" / "alert_overrides.json"


def _load_override_config() -> dict:
    """
    Read config/alert_overrides.json. Returns the camera sets under
    "parked_vehicle_baseline" and "static_object_baseline".

    This file ships with the project and is the single source of truth for
    camera baseline overrides. The listener won't start without it — if the
    file is missing or malformed at module import time, this raises so the
    problem surfaces immediately rather than silently degrading alert behavior.
    """
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"alert_overrides.json not found at {_CONFIG_PATH}. "
            f"This file ships with the project and must exist. "
            f"Restore it from git or check the deploy."
        )
    with open(_CONFIG_PATH) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise TypeError(
            f"alert_overrides.json root must be an object, "
            f"got {type(data).__name__}."
        )
    return data


# Load once at import time. The listener reads this dict on every alert;
# if you edit the file, restart the listener (kickstart via launchd) to
# pick up the changes.
_OVERRIDE_CONFIG: dict = _load_override_config()


def _get_parked_vehicle_cameras() -> frozenset[str]:
    """Return the parked-vehicle baseline camera set from the loaded config."""
    return frozenset(
        (_OVERRIDE_CONFIG.get("parked_vehicle_baseline") or {}).get("cameras") or []
    )


def _get_static_object_cameras() -> frozenset[str]:
    """Return the static-object baseline camera set from the loaded config."""
    return frozenset(
        (_OVERRIDE_CONFIG.get("static_object_baseline") or {}).get("cameras") or []
    )


def _get_vision_none_cameras() -> frozenset[str]:
    """Return the vision-none baseline camera set from the loaded config."""
    return frozenset(
        (_OVERRIDE_CONFIG.get("vision_none_baseline") or {}).get("cameras") or []
    )


def _get_distant_vehicle_cameras() -> frozenset[str]:
    """Return the distant-vehicle baseline camera set from the loaded config."""
    return frozenset(
        (_OVERRIDE_CONFIG.get("distant_vehicle_baseline") or {}).get("cameras") or []
    )


def _code_in_set(camera_name: str, codes: frozenset[str]) -> bool:
    """Phase.167 §13.4 Commit 17 (T3 C17): translate camera_name
    (friendly name or direct CAM{N}) to CAM{N}, then check membership.

    config/alert_overrides.json stores CAM{N} codes after §13.4
    migration; callers pass either the friendly name
    (Front Door Outside, Back Door Inside, etc.) or a direct
    CAM{N} code. This helper resolves either form via
    infra.cameras.code_for() and tests frozenset membership.

    Returns False if camera_name is empty, code_for() returns
    something unrecognized, or the resolved code is not in codes.
    """
    if not camera_name:
        return False
    from infra.cameras import code_for  # §13.4: avoids circular at module top
    return code_for(camera_name) in codes


# Keywords in vision's scene_description that distinguish "vehicle-shaped
# signal but probably distant / off-property" from "real vehicle on property."
# Matched case-insensitively. We do NOT need an exhaustive list — these are
# the failure-mode words from the 2026-07-23 21:35–21:57 alert burst and
# similar documented BB Solar IR-reflection events.
_DISTANT_VEHICLE_KEYWORDS: tuple[str, ...] = (
    "faint",
    "indistinct",
    "distant",
    "reflection",
    "light source",
    "light-source",
    "across the road",
    "off the property",
    "off-property",
    "far away",
    "moving lights",
    "possibly",
    "no clear",
    "not identifiable",
    "hard to make out",
    "barely visible",
)


def _vision_signals_distant_vehicle(vision_result: dict) -> bool:
    """
    Return True when vision sees a vehicle but flags it as ambiguous /
    distant / reflection (i.e. likely not a real vehicle on the property).

    Used by _apply_distant_vehicle_baseline_override. Triggers when:
      - objects_detected contains "vehicle" (or "car"/"truck"/"suv"/etc.),
        OR primary_subject is a vehicle type
      - scene_description contains any of the _DISTANT_VEHICLE_KEYWORDS

    Returns False if vision sees NO vehicle -- that's a different override's
    territory (vision_none_baseline).
    """
    if not isinstance(vision_result, dict):
        return False

    objects = [str(o).lower() for o in (vision_result.get("objects_detected") or [])]
    primary = (vision_result.get("primary_subject") or "").strip().lower()
    scene = (vision_result.get("scene_description") or "").lower()

    VEHICLE_WORDS = ("vehicle", "car", "truck", "suv", "pickup", "van", "headlights")

    saw_vehicle = any(w in obj for obj in objects for w in VEHICLE_WORDS) or any(
        w in primary for w in VEHICLE_WORDS
    )
    if not saw_vehicle:
        return False

    return any(kw in scene for kw in _DISTANT_VEHICLE_KEYWORDS)


def _vision_returns_none(vision_result: dict) -> bool:
    """
    Return True when the vision model reports no identifiable subject in
    the frame. This is the determinative input for the vision_none_baseline
    override: when vision saw nothing, the alert LLM is being asked to
    classify from an empty signal and tends to fabricate a generic
    "Nighttime Activity at <location>" or "Faint motion detected at night"
    L1 verdict.

    Truth table (verified from log trace 2026-07-22/23 overnight):
        primary_subject == 'none'                                   → True
        primary_subject == '' and objects_detected == []           → True
        primary_subject == 'none' but objects_detected has person  → False
        primary_subject is 'person'/'vehicle'/'animal'/'object'    → False

    Distinct from _vision_sees_person (which only checks for person).
    Distinct from the static-object pattern: this catches the case where
    vision saw NOTHING, not where it saw a static non-person object.
    """
    if not isinstance(vision_result, dict):
        return True  # missing/malformed vision == treat as "saw nothing"
    primary = (vision_result.get("primary_subject") or "").strip().lower()
    if primary and primary != "none":
        return False
    objects = vision_result.get("objects_detected") or []
    # If primary is 'none' but objects_detected contains a person or
    # vehicle, vision actually did see something — don't override.
    for o in objects:
        if not o:
            continue
        tag = str(o).strip().lower()
        if tag in (
            "person", "people", "man", "woman", "child", "human",
            "intruder", "suspect", "unidentified person",
            "vehicle", "car", "truck", "pickup truck", "sedan",
            "suv", "van", "pickup",
        ):
            return False
    return True


def _apply_vision_none_baseline_override(
    alert: dict, vision_result: dict, camera_name: str, timestamp: str | None = None
) -> dict:
    """
    Downgrade L1 → L0 on cameras where the vision LLM consistently returns
    primary_subject='none' (saw nothing identifiable) but the alert LLM
    fabricates a noncommittal L1 verdict — the canonical burnt pattern is
    titles like "Nighttime Activity at <location>", "Unknown activity at
    front door during night hours", "Faint motion detected at night",
    "Nighttime Motion Detected Near Coop". Vision seeing nothing is the
    deterministic tell that the L1 is fabricated; demoting loses no signal
    we would otherwise act on.

    Off-hours only — during working hours (daytime) vision is more reliable
    and the alert LLM is more useful; trust its L1 in that window.

    Camera set is read from config/alert_overrides.json
    (vision_none_baseline.cameras). Add a camera to that file to suppress
    its vision=none → L1 fabrications.

    Only acts on:
        - timestamp falls in the off-hours window (20:00 – 06:00)
        - camera in the vision_none baseline set
        - current threat_level == 1 (LLM-fabricated suspicious activity)
        - vision_result.primary_subject == 'none' AND no person/vehicle
          in objects_detected (vision saw nothing identifiable)

    Does NOT override L2 — real critical threats pass through. Does NOT
    override L1 when vision detected a person, vehicle, or non-empty
    object — those scenarios retain the alert LLM's verdict and any other
    baseline override (parked_vehicle, static_object) handles them.
    """
    if not timestamp or not _is_off_hours(timestamp):
        return alert
    # Phase.167 §13.4 Commit 17 (T3 C17): _OVERRIDE_CONFIG sets are
    # keyed by CAM{N} codes (per infra.cameras._LEGACY_PREFIX_TO_CODE
    # and config/alert_overrides.json §13.4 keys). Translate
    # camera_name (friendly name) to CAM{N} before the membership
    # check.
    from infra.cameras import code_for
    _cam_code = code_for(camera_name) if camera_name else ""
    if not _code_in_set(camera_name, _get_vision_none_cameras()):
        return alert
    current_level = alert.get("threat_level", -1)
    if current_level != 1:
        return alert
    if not _vision_returns_none(vision_result):
        return alert
    log.info(
        f"[vision-none-baseline] {camera_name} (off-hours): vision returned no "
        f"identifiable subject, demoting L1 → L0 (LLM-fabricated activity "
        f"verdict); original title={alert.get('title', '?')!r}"
    )
    alert["threat_level"] = 0
    alert["suppressed_by"] = "vision_none_baseline_override"
    return alert


def _apply_parked_vehicle_baseline_override(
    alert: dict, vision_result: dict, camera_name: str, timestamp: str | None = None
) -> dict:
    """
    Downgrade L1 → L0 on cameras where a parked vehicle is the baseline state,
    when vision did not describe a person. Kills the IR-reflection false-positive
    class (parked vehicle + camera IR + night = LLM invents "headlights on").

    The rule applies during off-hours ("dark outside working hours", 20:00–06:00).
    During working hours (daytime), the LLM is more reliable and IR noise is
    less prominent — trust the LLM's L1 in that window. If timestamp is missing
    we don't apply the override (conservative — better to err on signal).

    Camera set is read from config/alert_overrides.json (parked_vehicle_baseline.
    cameras), so adding a camera is a file edit.

    Only acts on:
        - timestamp falls in the off-hours window (20:00–06:00)
        - camera in the parked-vehicle baseline set
        - current threat_level == 1 (LLM-conjured suspicious activity)
        - vision_result does not show a person

    Does NOT override L2 — real critical threats (weapons, forced entry) still
    fire even on a parking camera. Does NOT override L1 when a person is present
    — that scenario is genuinely suspicious per the camera's guardrail.
    """
    if not timestamp or not _is_off_hours(timestamp):
        return alert
    if not _code_in_set(camera_name, _get_parked_vehicle_cameras()):
        return alert
    current_level = alert.get("threat_level", -1)
    if current_level != 1:
        return alert
    if _vision_sees_person(vision_result):
        return alert
    log.info(
        f"[parked-vehicle-baseline] {camera_name} (off-hours): vision shows no person, "
        f"downgrading L1 → L0 (likely IR-reflection false positive); "
        f"original title={alert.get('title', '?')!r}"
    )
    alert["threat_level"] = 0
    # Preserve LLM title/description but mark them as suppressed so a human
    # reading the alert log knows this was overridden. The original fields
    # remain available for postmortem analysis.
    alert["suppressed_by"] = "parked_vehicle_baseline_override"
    return alert


def _apply_distant_vehicle_baseline_override(
    alert: dict, vision_result: dict, camera_name: str, timestamp: str | None = None
) -> dict:
    """
    Downgrade L1 → L0 on cameras where distant / off-property / faint vehicle
    signals are baseline noise. Distinct from parked_vehicle_baseline: this
    rule fires when vision sees a VEHICLE but flags it as ambiguous (faint,
    indistinct, reflection, light source, across the road, etc.). Targets
    the 2026-07-23 21:35–21:57 alert burst on the original Building Back Solar
    where vision returned a "faint, indistinct shape, possibly a vehicle's
    reflection or light source" and the alert LLM escalated to L1 anyway;
    same pattern now handled for CAM1 (north-pasture-road
    headlights, 2026-07-29).

    Off-hours only. Camera-scoped via config/alert_overrides.json
    (distant_vehicle_baseline.cameras) — adding a camera is a config edit.

    Only acts on:
        - timestamp falls in the off-hours window (20:00 – 06:00)
        - camera in the distant-vehicle baseline set
        - current threat_level == 1
        - vision shows a vehicle AND scene_description contains a distance
          / ambiguity keyword
        - vision does NOT show a person

    Does NOT override L2 — real critical threats still fire. Does NOT
    override L1 when a person is present.

    The distinction from parked_vehicle_baseline: that one fires when
    vision sees a parked vehicle (the IR-reflection hallucination).
    This one fires when vision sees a vehicle but considers it ambiguous
    (the distance / off-property noise pattern). Both rules can apply
    on the same alert; idempotent (first demote wins).
    """
    if not timestamp or not _is_off_hours(timestamp):
        return alert
    if not _code_in_set(camera_name, _get_distant_vehicle_cameras()):
        return alert
    current_level = alert.get("threat_level", -1)
    if current_level != 1:
        return alert
    if _vision_sees_person(vision_result):
        return alert
    if not _vision_signals_distant_vehicle(vision_result):
        return alert
    log.info(
        f"[distant-vehicle-baseline] {camera_name} (off-hours): vision sees a "
        f"vehicle signal with ambiguity/distance keywords, downgrading L1 → L0 "
        f"(likely distant headlights or off-property noise); "
        f"original title={alert.get('title', '?')!r}"
    )
    alert["threat_level"] = 0
    alert["suppressed_by"] = "distant_vehicle_baseline_override"
    return alert


def _apply_static_object_baseline_override(
    alert: dict, vision_result: dict, camera_name: str, timestamp: str | None = None
) -> dict:
    """
    Downgrade L1 → L0 on cameras where static environmental objects (tarp,
    equipment, debris, distant county-road lights, parked equipment) are the
    baseline state. Vision sees a static non-person object; the alert LLM
    elevates to L1 because it doesn't know the object is permanent background.
    Demotes L1 → L0 unless vision explicitly sees a person.

    Off-hours only. During working hours (daytime), the LLM is more reliable
    and static objects are usually less ambiguous — trust the LLM's L1 in
    that window.

    Camera set is read from config/alert_overrides.json
    (static_object_baseline.cameras). Add a camera to that file to suppress
    its static-noise L1 alerts.

    Only acts on:
        - timestamp falls in the off-hours window (20:00 – 06:00)
        - camera in the static-object baseline set
        - current threat_level == 1
        - vision_result does NOT describe a person

    Does NOT override L2. Does NOT override L1 when a person is present.

    The distinction from the parked-vehicle override: that one is about the
    specific IR-reflection hallucination on parking cameras. This one is the
    broader "LLM elevates static environmental noise to L1" pattern.
    """
    if not timestamp or not _is_off_hours(timestamp):
        return alert
    if not _code_in_set(camera_name, _get_static_object_cameras()):
        return alert
    current_level = alert.get("threat_level", -1)
    if current_level != 1:
        return alert
    if _vision_sees_person(vision_result):
        return alert
    log.info(
        f"[static-object-baseline] {camera_name} (off-hours): vision shows no person, "
        f"downgrading L1 → L0 (static environmental noise); "
        f"original title={alert.get('title', '?')!r}"
    )
    alert["threat_level"] = 0
    alert["suppressed_by"] = "static_object_baseline_override"
    return alert


def _apply_baseline_overrides(
    alert: dict, vision_result: dict, camera_name: str, timestamp: str | None = None
) -> dict:
    """
    Apply all four baseline overrides in sequence. Order matters only for
    the suppressed_by marker on shared logs — the threat_level transition
    is idempotent once demoted to 0, so apply cheapest first:

    1. parked_vehicle_baseline  — IR-reflection hallucination on parking
       cameras (CAM4 — dump trailer + F-350 reflective strips).
       Most specific. Older guardrail.
    2. distant_vehicle_baseline — vision sees a vehicle but flags it as
       ambiguous / distant / off-property (CAM1 north-pasture-
       road headlights pattern; added 2026-07-29; historical pattern
       originally on Building Back Solar from 2026-07-23).
    3. static_object_baseline   — environmental static noise / distant
       lights elevated to L1.
    4. vision_none_baseline     — vision returned primary_subject='none'
       and the alert LLM fabricated an L1 verdict from nothing. Catches
       titles like "Nighttime Activity at <location>" and "Faint motion
       detected at night".
    """
    alert = _apply_parked_vehicle_baseline_override(alert, vision_result, camera_name, timestamp)
    alert = _apply_distant_vehicle_baseline_override(alert, vision_result, camera_name, timestamp)
    alert = _apply_static_object_baseline_override(alert, vision_result, camera_name, timestamp)
    alert = _apply_vision_none_baseline_override(alert, vision_result, camera_name, timestamp)
    return alert