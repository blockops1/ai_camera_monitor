"""
frame_selector.py — Adaptive frame selection for vision analysis.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - function arg all_frames: list[str] (required) — paths to captured
      frames from this alert's capture batch
    - function arg vision_result: dict (required) — output from the
      first-frame analysis pass
    - function arg confidence_threshold: float (optional, default 0.85)

OUTPUTS:
    - return value: list[str] — subset of all_frames to use for the
      final analysis pass. Empty list means "use the first frame."

PUBLIC API:
    select_frames(all_frames: list[str], vision_result: dict,
                  confidence_threshold: float = 0.85) -> list[str]
        Decide which captured frames are worth re-analyzing. Returns
        [first] for high-confidence single-subject, [first, last] for
        everything else, [first, middle, last] only for low-confidence
        multi-subject cases (escalation path).

DOES NOT DO:
    - Read or write any file — pure function
    - Network calls — pure function
    - Decide which camera to process — caller decides
    - Build the vision prompt — owned by infra.vision_analyzer

WHY HERE:
    Sending more frames to Qwen costs ~2-3s per frame (warm). The first
    frame alone is usually enough — sending 6 frames when 1 would do
    wastes 10-15s per alert. This module encapsulates the rule so it
    can be tested in isolation (no IO, no LLM calls).

CALLED BY:
    - listener.listener: select_frames() in _process_alert()

CALLS INTO:
    - stdlib only (typing, set membership)

RELATED:
    - infra.frame_capture — emits the all_frames list
    - infra.vision_analyzer — consumer of the returned frame subset
"""

from __future__ import annotations

from collections.abc import Iterable

# Categories that warrant sending more frames to confirm
INTERESTING_OBJECTS: set[str] = {
    "person",
    "people",  # plural / category label variants from vision model
    "human",
    "man",
    "woman",
    "child",
    "dog",
    "cat",
    "animal",
    "vehicle",
    "car",
    "truck",
    "suv",
    "package",
    "weapon",
    "gun",
    "knife",
    "crowbar",
    "intruder",
}


def _normalize_objects(objects: Iterable[str]) -> set[str]:
    """Lowercase + strip, return set."""
    return {(o or "").strip().lower() for o in objects if o}


def _is_multi_subject(objects: Iterable[str]) -> bool:
    """Return True if 2+ of the objects are 'interesting' (people/animals/vehicles/etc)."""
    norm = _normalize_objects(objects)
    interesting_hits = norm & INTERESTING_OBJECTS
    return len(interesting_hits) >= 2


def select_frames(
    all_frames: list[str],
    vision_result: dict,
    confidence_threshold: float = 0.85,
) -> list[str]:
    """
    Decide which frames to send to the vision model for the final analysis.

    Args:
        all_frames: All captured frames in order (typically 3).
        vision_result: Dict from `analyze_frames` on the first frame.
            Must have keys: `objects_detected` (list[str]), `confidence` (float).
        confidence_threshold: Vision confidence must be at or above this
            to consider single-frame sufficient. Default 0.85.

    Returns:
        List of frame paths to use:
        - `[all_frames[0]]` -- single frame, high confidence, single subject
        - `[all_frames]` -- escalate, send the entire capture window.

    Phase.13 (2026-07-26): for `event_hint == "vehicle"` the
    alert_listener bypasses this function entirely and sends all
    captured frames using VEHICLE_MOTION_PROMPT_TEMPLATE. The
    multi-frame vehicle pipeline never reaches `select_frames()`. This
    function is only called for non-vehicle events (person / motion /
    animal). The Phase.8 6-frame/4s capture window is still correct
    for those escalations.
    """
    if not all_frames:
        return []

    # Only one frame? Can't escalate. Return it.
    if len(all_frames) == 1:
        return [all_frames[0]]

    objects = vision_result.get("objects_detected", []) or []
    confidence = float(vision_result.get("confidence", 0.0) or 0.0)
    multi_subject = _is_multi_subject(objects)

    # Decision: escalate if low confidence OR multi-subject.
    # Phase.8 — send the ENTIRE capture window. Qwen reasons over
    # the full temporal sequence and returns `best_frame_index` itself.
    # This is what handles the slow tractor (frames 4-6 are close-range)
    # and the fast car (frame 1 still catches it).
    if confidence < confidence_threshold or multi_subject:
        return list(all_frames)

    # Confident single-subject (or no subject): single frame is enough
    return [all_frames[0]]
