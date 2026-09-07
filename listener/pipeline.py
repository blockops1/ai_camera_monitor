"""
pipeline.py — Stages 1-3 of the 11-stage linear alert pipeline.

STATUS: provisional (stages 1-3 only; full pipeline in US-008b)
THREAD SAFETY: single-threaded

INPUTS:
    - alert: dict (required) — webhook alert payload (has camera_id, classification, frames)

OUTPUTS:
    - return dict: {status, camera_id, classification} or {status, camera_id, classification, frames}

PUBLIC API:
    run(alert: dict) -> dict
        Execute stages 1-3: extract, cooldown check, load frames.

DOES NOT DO:
    - Gate, vision, Telegram, or cooldown record (stages 4-11 in US-008b/c)
    - Use async (sync for easier extension across US-008b/US-008c)

CALLED BY:
    - listener.listener: handle_webhook() via asyncio.run(pipeline_run(alert))

CALLS INTO:
    - infra.pipeline_cooldown: PipelineCooldown.should_suppress
"""

from __future__ import annotations

from infra.pipeline_cooldown import PipelineCooldown


def run(alert: dict) -> dict:
    """Stages 1-3: extract camera_id/classification, check cooldown, load frames."""
    # Stage 1: extract camera_id + classification from alert.
    camera_id = alert.get("camera_id", "unknown")
    classification = alert.get("classification", "motion")

    # Stage 2: cooldown check via PipelineCooldown.should_suppress.
    cooldown = PipelineCooldown()
    if cooldown.should_suppress(camera_id, classification):
        return {
            "status": "suppressed",
            "camera_id": camera_id,
            "classification": classification,
        }

    # Stage 3: load 4 frames from alert.
    frames = list(alert.get("frames", []))

    return {
        "status": "ok",
        "camera_id": camera_id,
        "classification": classification,
        "frames": frames,
    }
