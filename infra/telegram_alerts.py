"""telegram_alerts — simple Telegram text-message helper for infra alerts.

STATUS: stable
THREAD SAFETY: single-threaded (calls are fire-and-forget from daemon threads)

INPUTS:
    - env TELEGRAM_BOT_TOKEN — Telegram bot token (required)
    - env TELEGRAM_HOME_CHAT_ID — target chat ID (required)

OUTPUTS:
    - None on success; logs on failure.

PUBLIC API:
    send(message: str) -> bool
        Send a plain-text message to the home chat.
        Returns True on success, False on failure.
        If send fails, logs the error — never swallows silently.

DOES NOT DO:
    - Send photos or media groups (that's telegram_formatter.dispatcher)
    - Retry on failure
    - Block the caller on long-running I/O

CALLS INTO:
    - httpx (HTTP POST to api.telegram.org)
    - logging (for error reporting)
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime state — exposed for health-heartbeat consumers (US-073i).
# ---------------------------------------------------------------------------
_last_send_timestamp: float | None = None
_send_errors: deque[float] = deque(maxlen=100)  # ring buffer of error timestamps


def send(message: str) -> bool:
    """Send a plain-text Telegram message to the home chat.

    Args:
        message: the text to send.

    Returns:
        True on success, False on failure.
        On failure, logs the error — never raises, never swallows silently.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")

    if not bot_token or not chat_id:
        log.warning(
            "telegram_alerts.send: TELEGRAM_BOT_TOKEN or TELEGRAM_HOME_CHAT_ID "
            "not set — skipping alert: %s",
            message[:120],
        )
        now = time.monotonic()
        _send_errors.append(now)
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                url,
                json={"chat_id": chat_id, "text": message},
            )
            if 400 <= resp.status_code < 600:
                log.error(
                    "telegram_alerts.send: Telegram API returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                _send_errors.append(time.monotonic())
                return False
            log.info("telegram_alerts.send: message delivered")
            global _last_send_timestamp
            _last_send_timestamp = time.monotonic()
            return True
    except (httpx.HTTPError, OSError, RuntimeError) as err:
        log.error(
            "telegram_alerts.send: failed to send alert '%s': %s",
            message[:120],
            err,
        )
        _send_errors.append(time.monotonic())
        return False
