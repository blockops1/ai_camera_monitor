"""
listener.py — Webhook entrypoint: run pipeline, send Telegram.

STATUS: stable
THREAD SAFETY: single-threaded

INPUTS:
    - alert: dict (required) — webhook alert payload from camera

OUTPUTS:
    - return dict: {'status': 'ok'/'error', ...}
      Error: {'status': 'error', 'stage': str, 'alert_id': str}

PUBLIC API:
    handle_webhook(alert: dict) -> dict
        Run the 11-stage pipeline on a camera alert, dispatch Telegram.

DOES NOT DO:
    - Expose an HTTP endpoint (daemon does that)
    - Validate camera request signatures (daemon does that)
    - Handle retries or queuing (daemon does that)

CALLED BY:
    - (deferred: webhook daemon wrapping this in HTTP/FastAPI)

CALLS INTO:
    - listener.pipeline: run(alert) for the 11-stage pipeline
    - telegram: POST to Telegram API via python-telegram-bot
    - os.environ: bot token + chat ID from env
"""

from __future__ import annotations

import asyncio
import os

from telegram import Bot

from listener.pipeline import run as pipeline_run

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
HOME_CHAT_ID = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")


def handle_webhook(alert: dict) -> dict:
    """Receive a camera webhook alert, run the pipeline, send Telegram.

    Wraps pipeline.run() and the Telegram send each in their own
    try/except so neither failure mode crashes the webhook handler.
    """
    try:
        coro = pipeline_run(alert)
        # pipeline.run() may be sync (returns dict) or async (returns coroutine).
        # Tests patch asyncio.run, so check for the awaitable.
        if asyncio.iscoroutine(coro):
            result = asyncio.run(coro)
        else:
            result = coro
    except Exception as exc:
        return {
            "status": "error",
            "stage": str(exc),
            "alert_id": alert.get("id"),
        }

    if BOT_TOKEN and HOME_CHAT_ID:
        try:
            _send_telegram(BOT_TOKEN, HOME_CHAT_ID, result)
        except Exception:
            pass

    return {"status": "ok", "classification": result.get("classification", "")}


def _send_telegram(bot_token: str, chat_id: str, result: dict) -> None:
    """Send Telegram messages from pipeline result dict.

    Wraps async telegram.Bot calls via asyncio.run() so
    handle_webhook stays a plain sync function.
    """
    bot = Bot(token=bot_token)

    async def _do_send():
        for stage_key in ("tg1", "tg2", "tg3"):
            msg = result.get(stage_key)
            if not msg:
                continue
            caption = msg.get("caption", "")
            if caption:
                await bot.send_message(chat_id=chat_id, text=caption)
            for photo_path in msg.get("photos", []):
                if photo_path:
                    with open(photo_path, "rb") as f:
                        await bot.send_photo(chat_id=chat_id, photo=f)

    asyncio.run(_do_send())
