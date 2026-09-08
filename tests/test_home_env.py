"""
test_home_env.py — US-017b: verify ~/.env loading semantics in listener/daemon.py.

The daemon must pick up TELEGRAM_BOT_TOKEN from ~/.env at boot because
the launchd plist no longer carries that secret. Existing env vars win
over the file so launchd overrides still work for ops overrides.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from listener.daemon import _load_home_env


class TestLoadHomeEnv:
    """Tests for listener.daemon._load_home_env()."""

    def test_loads_telegram_token_from_env_file(self, tmp_path, monkeypatch):
        """Real ~/.env with a real token -> token appears in os.environ."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "TELEGRAM_BOT_TOKEN=7832687030:AA-real-token-from-test\n"
            "TELEGRAM_HOME_CHAT_ID=999\n"
        )

        # Force os.path.expanduser to point at our temp file.
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(env_file) if p == "~/.env" else p)
        # Make sure no leak from outer env.
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_HOME_CHAT_ID", raising=False)

        _load_home_env()

        assert os.environ.get("TELEGRAM_BOT_TOKEN") == "7832687030:AA-real-token-from-test"
        assert os.environ.get("TELEGRAM_HOME_CHAT_ID") == "999"

    def test_existing_env_wins_over_file(self, tmp_path, monkeypatch):
        """If launchd-injected env already has the token, ~/.env does NOT overwrite."""
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=from-file\n")

        monkeypatch.setattr(os.path.expanduser, "__call__",
                            lambda p: str(env_file) if p == "~/.env" else p) if False else None
        # Simpler: monkeypatch the function called inside _load_home_env.
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(env_file) if p == "~/.env" else p,
        )
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-launchd")

        _load_home_env()

        # Launchd value preserved, file value ignored.
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-launchd"

    def test_missing_env_file_is_silent(self, monkeypatch):
        """If ~/.env does not exist, no error, no env mutation."""
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        before = dict(os.environ)
        _load_home_env()
        # Nothing new injected (or removed).
        for k in set(before) | set(os.environ):
            assert os.environ.get(k) == before.get(k), f"unexpected change to {k}"

    def test_telegram_token_length_revealed_not_value(self, tmp_path, monkeypatch, caplog):
        """The log line must NOT contain the token value, only its length."""
        import logging

        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=secret-value-1234567890abcdef\n")
        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(env_file) if p == "~/.env" else p,
        )
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        with caplog.at_level(logging.INFO):
            _load_home_env()

        # Find the log record we emitted.
        records = [r for r in caplog.records if "Loaded" in r.getMessage()]
        assert records, "expected 'Loaded ...' log line"
        msg = records[0].getMessage()
        assert "secret-value-1234567890abcdef" not in msg
        assert "26" in msg or "telegram_token_len" in msg
