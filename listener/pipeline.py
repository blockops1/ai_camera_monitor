"""
pipeline.py — 11-stage linear alert pipeline (stub for US-009).

STATUS: provisional (stub — full implementation in US-010)
THREAD SAFETY: single-threaded (async function, called from single-threaded daemon)

INPUTS:
    - alert: dict (required) — webhook alert payload from camera

OUTPUTS:
    - return value: dict with classification results and formatted message dicts
      for TG#1, TG#2, TG#3 (empty dict keys when stages are skipped)

PUBLIC API:
    run(alert: dict) -> dict
        Execute the 11-stage pipeline on a camera webhook alert.
        Returns a dict with 'classification', 'tg1', 'tg2', 'tg3' keys.

DOES NOT DO:
    - Send Telegram messages (that lives in handle_webhook)
    - Run as a daemon (that lives in the listener daemon, US-011)
    - Expose an HTTP endpoint (that lives in US-011)

CALLED BY:
    - listener.listener: handle_webhook() (US-009, deferred US-010)

CALLS INTO:
    - (deferred: infra.gate, infra.vision_analyzer, infra.pipeline_cooldown,
      telegram_formatter.alert, telegram_formatter.detail, telegram_formatter.match)
"""

from __future__ import annotations


async def run(alert: dict) -> dict:
    """Execute the 11-stage pipeline on a camera webhook alert.

    Args:
        alert: webhook alert payload from camera.

    Returns:
        Dict with 'classification' and stage output keys (tg1, tg2, tg3).
    """
    # Stub — full implementation in US-010.
    # The pipeline runs: capture -> crops -> gate -> cooldown -> VM1 -> TG1
    # -> VM2 -> TG2 -> match -> TG3 -> record_hit.
    return {"classification": "vehicle", "tg1": {}, "tg2": {}, "tg3": {}}
