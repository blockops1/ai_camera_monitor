"""Tests for infra.farm_api.telegram.

STATUS: V2-068b7
RUN WITH: pytest tests/test_farm_api_telegram.py -v

Test strategy:
    - Create a temporary logs/ directory with a fake daemon.log.
    - Patch infra.paths.LOGS_DIR and infra.farm_api.telegram.LOGS_DIR
      to point at the temp dir.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_LOG_WITH_SUCCESS = (
    "2026-09-22 20:51:31,035 INFO daemon: env load complete: "
    "total=31 (TELEGRAM_HOME_CHAT_ID=-1001234567890)\n"
    "2026-09-22 20:52:00,000 INFO daemon: tg-send: alert_id=abc-123 "
    "TG#1 chat=-1001234567890 method=sendMediaGroup sources=2 "
    "converted=2 upload=12345 response=HTTP 200\n"
)

_LOG_WITH_ERROR_HTTP = (
    "2026-09-22 20:51:31,035 INFO daemon: tg-send: alert_id=abc-123 "
    "TG#1 chat=1234567890 method=sendMessage sources=0 "
    "converted=0 upload=0 response=HTTP 429\n"
)

_LOG_WITH_ERRORS = (
    "2026-09-22 20:51:31,035 ERROR daemon: tg-send: alert_id=abc-123 "
    "TG#1 error=DeliveryError: connection refused\n"
    "2026-09-22 20:52:00,000 ERROR daemon: tg-send: alert_id=def-456 "
    "TG#2 error=TimeoutError: timed out\n"
)

_LOG_NO_CHAT_ID = (
    "2026-09-22 20:51:31,035 INFO daemon: env load complete: total=31\n"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _test_log_dir(tmp_path: Path):
    """Create a fake logs/ directory with a daemon.log."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Patch infra.paths.LOGS_DIR so all infra functions read from temp
    # Also patch in the telegram module (it reads from infra.paths)
    with patch("infra.paths.LOGS_DIR", str(log_dir)), patch(
        "infra.farm_api.telegram.LOGS_DIR", str(log_dir)
    ):
        yield tmp_path


# ---------------------------------------------------------------------------
# test_get_telegram_status_shape
# ---------------------------------------------------------------------------


class TestGetTelegramStatusShape:
    """get_telegram_status() returns a valid TelegramStatus dataclass."""

    def test_status_shape(self, _test_log_dir):
        """When a daemon.log has Telegram entries, status is populated."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(_LOG_WITH_SUCCESS)

        from infra.farm_api.telegram import TelegramStatus, get_telegram_status

        status = get_telegram_status()

        assert isinstance(status, TelegramStatus)
        assert "[REDACTED-CHAT-ID]" in status.chat_id_redacted
        assert status.last_send_at is not None
        assert status.last_send_status == "success"
        assert status.errors_last_24h == 0

    def test_status_no_log_file(self, _test_log_dir):
        """When no daemon.log exists, status shows defaults."""
        # Don't write any log file
        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()

        assert status.chat_id_redacted == "[REDACTED-CHAT-ID]"
        assert status.last_send_at is None
        assert status.last_send_status == "unknown"
        assert status.errors_last_24h == 0

    def test_status_no_chat_id(self, _test_log_dir):
        """When log has no chat_id, redacted output is a static sentinel."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(_LOG_NO_CHAT_ID)

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        assert status.chat_id_redacted == "[REDACTED-CHAT-ID]"
        assert status.last_send_at is None
        assert status.last_send_status == "unknown"
        assert status.errors_last_24h == 0


# ---------------------------------------------------------------------------
# test_telegram_status_redacts_chat_id
# ---------------------------------------------------------------------------


class TestTelegramStatusRedactsChatId:
    """chat_id is fully redacted in output."""

    def test_redacts_digits(self, _test_log_dir):
        """All digits in chat_id are replaced with [REDACTED-CHAT-ID]."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(_LOG_WITH_SUCCESS)

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()

        # No raw digits should remain in the redacted output
        assert not any(ch.isdigit() for ch in status.chat_id_redacted)
        assert "[REDACTED-CHAT-ID]" in status.chat_id_redacted

    def test_redacts_various_chat_formats(self, _test_log_dir):
        """Redaction works for different chat ID formats."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(
            "2026-09-22 20:51:31,035 INFO daemon: TELEGRAM_HOME_CHAT_ID=1234567890\n"
        )

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()

        assert not any(ch.isdigit() for ch in status.chat_id_redacted)

    def test_no_chat_id_in_log(self, _test_log_dir):
        """When no chat_id is in the log, output is a static sentinel."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(_LOG_NO_CHAT_ID)

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()

        assert status.chat_id_redacted == "[REDACTED-CHAT-ID]"


# ---------------------------------------------------------------------------
# test_last_send_status
# ---------------------------------------------------------------------------


class TestLastSendStatus:
    """last_send_status correctly reflects the last HTTP response."""

    def test_success_status(self, _test_log_dir):
        """HTTP 2xx response is recorded as 'success'."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(
            "2026-09-22 20:51:31,035 INFO daemon: tg-send: alert_id=abc-123 "
            "TG#1 chat=1234567890 method=sendMessage sources=0 "
            "converted=0 upload=0 response=HTTP 200\n"
        )

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        assert status.last_send_status == "success"

    def test_error_http_status(self, _test_log_dir):
        """HTTP 4xx/5xx response is recorded as 'error'."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(_LOG_WITH_ERROR_HTTP)

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        assert status.last_send_status == "error"

    def test_error_log_lines_counted(self, _test_log_dir):
        """Error log lines (not just HTTP responses) are counted."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(_LOG_WITH_ERRORS)

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        assert status.errors_last_24h == 2

    def test_24h_window(self, _test_log_dir):
        """Errors older than 24 hours are not counted."""
        log_file = _test_log_dir / "logs" / "daemon.log"
        log_file.write_text(
            "2026-09-20 10:00:00,000 ERROR daemon: tg-send: alert_id=old-123 "
            "TG#1 error=DeliveryError: old error\n"
            "2026-09-22 20:51:31,035 ERROR daemon: tg-send: alert_id=abc-123 "
            "TG#1 error=DeliveryError: recent error\n"
        )

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        # Only the recent error (within 24h) should be counted
        assert status.errors_last_24h == 1
