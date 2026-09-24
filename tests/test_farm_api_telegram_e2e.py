"""test_farm_api_telegram_e2e — E2E tests for farm_api telegram endpoint.

E2E strategy:
    - Create a fake logs/ directory with daemon.log entries that mimic real
      Telegram dispatch log lines.
    - Monkeypatch FARMSURV_LOGS_DIR so infra.paths resolves to the temp dir
      BEFORE importing infra.farm_api.telegram.
    - Import and exercise get_telegram_status() against the real log parser.
    - Verify chat ID redaction, send-at extraction, error counting.

Bug classes covered:
    - Chat ID not redacted in API response.
    - Timestamp parsing fails on daemon log format.
    - Error counting broken on daemon log format.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Realistic daemon.log lines
# ---------------------------------------------------------------------------

_LOG_SUCCESS = (
    "2026-09-24 18:00:00,000 INFO daemon: tg-send: "
    "alert_id=abc123 TG#1 chat=123456789 method=sendMediaGroup "
    "url=https://api.telegram.org/response=HTTP 200\n"
)

_LOG_ERROR = (
    "2026-09-24 17:00:00,000 ERROR daemon: tg-send: "
    "alert_id=def456 TG#2 error=ConnectionError: timeout\n"
)


@pytest.fixture()
def _telegram_e2e_env(tmp_path: Path):
    """Create a fake logs/daemon.log with Telegram dispatch entries."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_file = logs_dir / "daemon.log"

    # Write realistic log lines
    log_file.write_text(
        "2026-09-24 16:00:00,000 INFO daemon: env load complete: "
        "TELEGRAM_HOME_CHAT_ID=123456789\n"
        + _LOG_SUCCESS
        + _LOG_ERROR
        + _LOG_ERROR  # two errors
    )

    # Monkeypatch env var BEFORE importing telegram module
    old_logs_dir = os.environ.get("FARMSURV_LOGS_DIR")
    os.environ["FARMSURV_LOGS_DIR"] = str(logs_dir)

    # Clear cached modules
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.paths", "infra.farm_api.telegram")):
            del sys.modules[mod_name]

    yield tmp_path

    # Restore
    if old_logs_dir is not None:
        os.environ["FARMSURV_LOGS_DIR"] = old_logs_dir
    else:
        os.environ.pop("FARMSURV_LOGS_DIR", None)
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.paths", "infra.farm_api.telegram")):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------

class TestTelegramStatusE2E:
    """E2E: get_telegram_status() parses real log lines correctly."""

    def test_returns_telegram_status(self, _telegram_e2e_env):
        from infra.farm_api.telegram import TelegramStatus, get_telegram_status

        status = get_telegram_status()
        assert isinstance(status, TelegramStatus)

    def test_chat_id_redacted(self, _telegram_e2e_env):
        """Digits in chat ID are replaced with [REDACTED-CHAT-ID]."""
        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        assert "123456789" not in status.chat_id_redacted
        assert "[REDACTED-CHAT-ID]" in status.chat_id_redacted

    def test_last_send_at_extracted(self, _telegram_e2e_env):
        """ISO-8601 timestamp extracted from last send log line."""
        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        assert status.last_send_at is not None
        assert "T" in status.last_send_at  # ISO-8601 format

    def test_last_send_status_success(self, _telegram_e2e_env):
        """HTTP 200 maps to 'success'."""
        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        assert status.last_send_status == "success"

    def test_error_count(self, _telegram_e2e_env):
        """Two ERROR lines in log → errors_last_24h = 2."""
        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()
        assert status.errors_last_24h == 2

    def test_missing_log_file(self, tmp_path):
        """No daemon.log → all fields default."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        old_logs_dir = os.environ.get("FARMSURV_LOGS_DIR")
        os.environ["FARMSURV_LOGS_DIR"] = str(logs_dir)

        # Clear cached modules
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith(("infra.paths", "infra.farm_api.telegram")):
                del sys.modules[mod_name]

        from infra.farm_api.telegram import get_telegram_status

        status = get_telegram_status()

        # Restore
        if old_logs_dir is not None:
            os.environ["FARMSURV_LOGS_DIR"] = old_logs_dir
        else:
            os.environ.pop("FARMSURV_LOGS_DIR", None)
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith(("infra.paths", "infra.farm_api.telegram")):
                del sys.modules[mod_name]

        assert status.last_send_status == "unknown"
        assert status.errors_last_24h == 0
        assert status.last_send_at is None
        assert status.chat_id_redacted == "[REDACTED-CHAT-ID]"
