"""
animal_event_pipeline.py — Animal event alert pipeline (scaffold).

STATUS: provisional
THREAD SAFETY: single-threaded (called by listener worker pool; one
                process_animal_event() call per alert)

INPUTS:
    - AnimalContext dataclass (required) — per-alert carrier built by
      the listener (see AnimalContext below)
    - data/frames/<alert_id>/ — gate-captured frames already on disk
      (mirror of person_event_pipeline: the gate does the capture,
      this pipeline reuses those frames)

OUTPUTS:
    - return value: dict with keys:
        - alert_id: str
        - camera_name: str
        - telegram_sent: bool   (always False in 6B.165.1 scaffold)
        - suppressed: bool      (always False in 6B.165.1 scaffold)
        - suppressed_reason: str | None
        - phase: str            (always "scaffold" in 6B.165.1)
    - log line: structured INFO log via logging.getLogger(__name__) on
      every call (audit-only — no Telegram, no Qwen, no frames written)

PUBLIC API:
    process_animal_event(ctx: AnimalContext) -> dict
        Scaffold entry point. Confirms the dispatch route works by
        logging receipt of every animal event. Real capture / Qwen /
        matching / Telegram arrive in subsequent sub-phases
        (6B.165.2 through 6B.165.6).

DOES NOT DO:
    - Qwen vision calls → arrives in §11.86.3 (6B.165.3)
    - Animal matching → arrives in §11.86.2 (6B.165.2)
    - Telegram send → arrives in §11.86.6 (6B.165.6)
    - Per-camera cooldown config → arrives in §11.86.4 (6B.165.4)
    - Known-animal enrollment / registry → arrives in §11.86.5
      (6B.165.5)
    - Frame capture → the gate already captured frames to disk;
      this scaffold does NOT re-capture (same pattern as
      person_event_pipeline, which also relies on gate frames)
    - Behavior change for person/vehicle pipelines → out of scope
    - Touching the existing event_promotion: 'animal' → 'people' gate
      logic → out of scope; that path is correct (a gate-class=person
      on an animal webhook genuinely is a person, not a bear)

WHY HERE:
    PLAN §11.86 (Phase.165) — animal pipeline as the third
    class-parallel sibling of person and vehicle. Per the operator 2026-08-29,
    all three pipelines are structurally identical
    (webhook → gate → capture → identify → match → emit → Telegram)
    and differ only in what information they carry.

    6B.165.1 (this file) is the scaffold phase: prove the dispatch
    works end-to-end before building any of the four stage functions.
    Mirrors 6B.106 (person pipeline scaffold).

CALLED BY:
    - listener.listener: _process_animal_alert() (added in 6B.165.1)
      dispatches event_type='animal' events here

CALLS INTO:
    - logging.getLogger(__name__): audit log line per animal event
    - infra.paths: ALERT_FRAME_DIR (via the listener, not directly)

RELATED:
    - listener.person_event_pipeline.PersonContext (sibling carrier)
    - listener.vehicle_event_pipeline.AlertContext (sibling carrier)
    - PLAN.md §11.86 (Phase.165)
    - docs/RESEARCH-6B165-animal-pipeline.md (design rationale)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AnimalContext:
    """Per-alert carrier for animal events.

    Mirrors PersonContext (6B.106) but is intentionally minimal in the
    scaffold phase. As subsequent sub-phases land
    (6B.165.2 matcher, 6B.165.3 prompt, 6B.165.6 Telegram), fields
    will be appended in-place rather than created from scratch — this
    keeps the diff readable per phase.

    Attributes:
        alert_id: UUID for this alert run (matches alert_id used in
            audit log + Telegram caption).
        camera_name: Human-readable camera label
            (e.g. a CAM{N} code like "CAM3").
        timestamp: ISO 8601 event timestamp from the webhook.
        event_type: "animal" (always, in 6B.165.1; lowercased by
            listener).
        rtsp_url: Full RTSP URL for this camera (used for future
            capture; the scaffold does not capture).
        output_dir: Where captured frames live
            (data/frames/<alert_id>/). The gate writes here; the
            scaffold just reads metadata about the directory.
        bot_token: Telegram bot token (loaded by listener). Not used
            in 6B.165.1; reserved for 6B.165.6.
        chat_id: Telegram chat ID. Not used in 6B.165.1.
        api_url: Vision API URL (Qwen endpoint). Not used in
            6B.165.1; reserved for 6B.165.3.

    State populated by stages (none in 6B.165.1; fields reserved):
        vision_result: dict — Qwen output (after 6B.165.3 identify).
        animal_match: Any — match_stage output (after 6B.165.2).
        structured_body: str — formatted Telegram body (after
            6B.165.6).
        result: dict — final result dict returned by
            process_animal_event.

    Side effects (none in 6B.165.1; fields reserved):
        telegram_sent: bool — whether a Telegram was sent.
        telegram_error: str | None — error message if send failed.
    """

    # --- Required inputs (set by listener) ---
    alert_id: str
    camera_name: str
    timestamp: str
    event_type: str
    rtsp_url: str
    output_dir: str
    bot_token: str
    chat_id: str
    api_url: str

    # --- Reserved state fields (populated by future sub-phases) ---
    vision_result: dict = field(default_factory=dict)
    animal_match: Any = None
    structured_body: str = ""
    result: dict = field(default_factory=dict)

    # --- Side effects (none yet) ---
    telegram_sent: bool = False
    telegram_error: str | None = None


def process_animal_event(ctx: AnimalContext) -> dict:
    """Scaffold entry point for animal events.

    Phase.165.1 (PLAN §11.86.1) — prove the dispatch route works
    by logging receipt of every animal event. No Qwen, no Telegram,
    no matching. Subsequent sub-phases add the real stages.

    The audit log line is the entire behavior of this scaffold. It is
    grep-able by alert_id and by camera, mirroring the 6B.164
    vision_attrs logging pattern.

    Returns:
        dict with keys:
            alert_id, camera_name, telegram_sent (False),
            suppressed (False), suppressed_reason (None),
            phase ("scaffold")
    """
    log.info(
        f"[{ctx.alert_id}] animal_pipeline: received "
        f"(camera={ctx.camera_name} event={ctx.event_type!r} "
        f"timestamp={ctx.timestamp}) — scaffold (no Qwen, no Telegram)"
    )

    return {
        "alert_id": ctx.alert_id,
        "camera_name": ctx.camera_name,
        "telegram_sent": False,
        "suppressed": False,
        "suppressed_reason": None,
        "phase": "scaffold",
    }
