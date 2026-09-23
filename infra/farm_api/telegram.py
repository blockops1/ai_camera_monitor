"""farm_api.telegram — Telegram dispatch status resource functions.

STATUS: alpha — V2-068b7
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - logs/daemon.log (Telegram dispatch log lines)

OUTPUTS:
    - TelegramStatus dataclass
    - get_telegram_status() -> TelegramStatus

DOES NOT DO:
    - Send or receive Telegram messages (read-only introspection)
    - Modify Telegram configuration
    - Connect to external services (pure filesystem reads)

CALLERS:
    - infra.mcp_server (MCP tool adapters: get_telegram_status)
    - Any code that needs a read-only view of Telegram dispatch health

USAGE:
    from infra.farm_api.telegram import get_telegram_status
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from infra.paths import LOGS_DIR

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TelegramStatus:
    """Read-only snapshot of the Telegram dispatch subsystem's status.

    Attributes:
        chat_id_redacted: The Telegram home chat ID with digits redacted
            (e.g. "-1001234567890" -> "-100[REDACTED-CHAT-ID]").
        last_send_at: ISO-8601 timestamp of the most recent Telegram send
            attempt, or None if no sends have been logged.
        last_send_status: "success" if the last send had HTTP 2xx,
            "error" if the last send had a non-2xx response,
            "unknown" if no sends have been logged.
        errors_last_24h: Number of Telegram error entries in the last 24 hours
            extracted from the daemon log.
    """

    chat_id_redacted: str
    last_send_at: str | None
    last_send_status: str
    errors_last_24h: int


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

# Matches Telegram dispatch log lines:
# 2026-09-22 20:51:31,035 INFO daemon: tg-send: alert_id=abc TG#1 chat=123456 method=sendMediaGroup ... response=HTTP 200
# Or: 2026-09-22 20:51:31,035 ERROR daemon: tg-send: alert_id=abc TG#1 error=DeliveryError: ...
_TG_SEND_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\s+"
    r"(INFO|ERROR)\s+[\w.]+:\s+"
    r"tg-send:\s+"
    r"alert_id=(\S+)\s+"
    r"TG#([123])\s+"
    r"chat=(\S+)\s+"
    r"method=\S+\s+"
    r".*?response=HTTP\s+(\d{3})"
)

_TG_ERROR_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\s+"
    r"ERROR\s+[\w.]+:\s+"
    r"tg-send:\s+"
    r"alert_id=(\S+)\s+"
    r"TG#([123])\s+"
    r"error=\S+"
)

# Matches the bot token log line:
# env load complete: total=31 (telegram_token_len=65)
# We also capture chat_id from any log line that mentions TELEGRAM_HOME_CHAT_ID
_CHAT_ID_RE = re.compile(r"TELEGRAM_HOME_CHAT_ID[^=]*=\s*(\S+)")


def _parse_ts_to_iso(ts_str: str | None) -> str | None:
    """Convert daemon log timestamp to ISO-8601.

    Args:
        ts_str: Raw timestamp from the log file.

    Returns:
        ISO-8601 string, or None.
    """
    if ts_str is None:
        return None
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
        return dt.isoformat()
    except ValueError:
        return None


def _redact_chat_id(chat_id: str) -> str:
    """Redact all digits in a chat ID, replacing them with [REDACTED-CHAT-ID].

    Args:
        chat_id: The raw chat ID string.

    Returns:
        Redacted chat ID (e.g. "-1001234567890" -> "-[REDACTED-CHAT-ID]").
    """
    # Replace each digit character with [REDACTED-CHAT-ID]
    result = []
    for ch in chat_id:
        if ch.isdigit():
            result.append("[REDACTED-CHAT-ID]")
        else:
            result.append(ch)
    return "".join(result)


def _extract_chat_id_from_log() -> str | None:
    """Extract the Telegram chat ID from the daemon log.

    Searches for TELEGRAM_HOME_CHAT_ID=<value> in the log file.

    Returns:
        Raw chat ID, or None if not found.
    """
    log_path = Path(LOGS_DIR) / "daemon.log"
    if not log_path.is_file():
        return None

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("telegram: failed to read daemon.log: %s", exc)
        return None

    for line in reversed(lines):
        match = _CHAT_ID_RE.search(line)
        if match:
            return match.group(1)
    return None


def _get_last_send_info(log_path: Path) -> tuple[str | None, str]:
    """Parse the last Telegram send entry from the daemon log.

    Args:
        log_path: Path to daemon.log.

    Returns:
        Tuple of (last_send_at ISO timestamp or None, last_send_status).
        Status is "success" for HTTP 2xx, "error" otherwise.
    """
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("telegram: failed to read daemon.log: %s", exc)
        return None, "unknown"

    last_send_at = None
    last_status = "unknown"

    for line in reversed(lines):
        match = _TG_SEND_RE.match(line)
        if match:
            ts_raw = match.group(1)
            status_code = int(match.group(6))
            last_send_at = _parse_ts_to_iso(ts_raw)
            if 200 <= status_code < 300:
                last_status = "success"
            else:
                last_status = "error"
            return last_send_at, last_status

    return None, "unknown"


def _count_errors_last_24h(log_path: Path) -> int:
    """Count Telegram error entries in the last 24 hours.

    Args:
        log_path: Path to daemon.log.

    Returns:
        Count of ERROR lines matching tg-send error pattern in last 24h.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    count = 0

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("telegram: failed to read daemon.log: %s", exc)
        return 0

    for line in lines:
        match = _TG_ERROR_RE.match(line)
        if match:
            ts_raw = match.group(1)
            try:
                ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S,%f").replace(
                    tzinfo=UTC
                )
                if ts >= cutoff:
                    count += 1
            except ValueError:
                continue

    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_telegram_status() -> TelegramStatus:
    """Return a TelegramStatus snapshot of the Telegram dispatch subsystem.

    Reads logs/daemon.log to extract the last send attempt's timestamp
    and status, counts errors in the last 24 hours, and redacts the
    chat ID.

    Returns:
        TelegramStatus with current state.
    """
    log_path = Path(LOGS_DIR) / "daemon.log"

    # Redacted chat ID
    raw_chat_id = _extract_chat_id_from_log()
    chat_id_redacted = (
        _redact_chat_id(raw_chat_id) if raw_chat_id else "[REDACTED-CHAT-ID]"
    )

    # Last send info
    if log_path.is_file():
        last_send_at, last_send_status = _get_last_send_info(log_path)
    else:
        last_send_at, last_send_status = None, "unknown"

    # Error count
    if log_path.is_file():
        errors_last_24h = _count_errors_last_24h(log_path)
    else:
        errors_last_24h = 0

    return TelegramStatus(
        chat_id_redacted=chat_id_redacted,
        last_send_at=last_send_at,
        last_send_status=last_send_status,
        errors_last_24h=errors_last_24h,
    )
