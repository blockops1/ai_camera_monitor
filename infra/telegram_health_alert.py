"""telegram_health_alert — send health heartbeat summary to Telegram on FAIL.

STATUS: new
THREAD SAFETY: single-threaded (called from HealthReporter daemon thread)

INPUTS:
    - env TELEGRAM_BOT_TOKEN (required)
    - env TELEGRAM_HOME_CHAT_ID (required)

OUTPUTS:
    - (rc, body_sent) tuple — HTTP status code and the text that was sent.
      On failure: (0, "failed to send") with an error logged.

PUBLIC API:
    send_health_alert(block: str, summary: str) -> tuple[int, str]
        POST the summary to Telegram. The full health block is included
        in the message body so the operator can see diagnostic context.

WORKING-HOURS: not gated. Operator wants TG on FAIL regardless of time —
    failures are always actionable. See US-092.

DOES NOT DO:
    - Send on success (silent when all checks pass).
    - Retry on failure.
    - Restart the listener (that's US-092b).

CALLS INTO:
    - infra.telegram_alerts.send()
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def send_health_alert(block: str, summary: str) -> tuple[int, str]:
    """Send a health alert summary + full block to Telegram on failure.

    Args:
        block: the full structured health block (all lines).
        summary: compact summary like 'Health ALERT (14:32 EDT): 2 issues'.

    Returns:
        (rc, body_sent) — HTTP status code on success, 0 on failure.
    """
    from infra import telegram_alerts

    body = f"{summary}\n\n---\n{block}"
    ok = telegram_alerts.send(body)
    if ok:
        log.info("health: tg_alert rc=200")
        return (200, body)
    else:
        log.warning("health: tg_alert rc=0 (send returned False)")
        return (0, "failed to send")
