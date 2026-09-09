"""
test_home_env.py — US-017b + PRD-V2-022 env-consolidation: verify .env loading
semantics in listener/daemon.py.

The daemon picks up TELEGRAM_BOT_TOKEN from <repo>/.env first, then falls
back to ~/.env. The launchd plist does not carry secrets — that's why the
daemon does this work at boot. Existing env vars win over file values so
launchd overrides still work for ops overrides.

PRD-V2-022 (2026-09-09): operator directive — env file belongs INSIDE the
app directory. ~/.env remains as a fallback.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

from listener.daemon import _REPO_ENV_PATH, _load_home_env


def _redirect_repo_env(monkeypatch, target: Path) -> None:
    """Patch the module-level _REPO_ENV_PATH so it points at target."""
    monkeypatch.setattr(
        "listener.daemon._REPO_ENV_PATH",
        str(target),
    )


def _no_repo_env(monkeypatch) -> None:
    """Make the repo .env appear absent for this test."""
    monkeypatch.setattr(
        "listener.daemon._REPO_ENV_PATH",
        "/nonexistent/repo/.env",
    )


class TestLoadHomeEnv:
    """Tests for listener.daemon._load_home_env()."""

    def test_loads_telegram_token_from_env_file(self, tmp_path, monkeypatch):
        """Real .env file with a real token -> token appears in os.environ."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "TELEGRAM_BOT_TOKEN=7832687030:AA-real-token-from-test\n"
            "TELEGRAM_HOME_CHAT_ID=999\n"
        )
        # Force the repo-rooted lookup to point at our temp file.
        _redirect_repo_env(monkeypatch, env_file)
        # ~/.env redirect is no longer needed (repo wins), but keep parity.
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: str(tmp_path / "no_home.env") if p == "~/.env" else p,
        )
        # Make sure no leak from outer env.
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_HOME_CHAT_ID", raising=False)

        _load_home_env()

        assert os.environ.get("TELEGRAM_BOT_TOKEN") == "7832687030:AA-real-token-from-test"
        assert os.environ.get("TELEGRAM_HOME_CHAT_ID") == "999"

    def test_existing_env_wins_over_file(self, tmp_path, monkeypatch):
        """If launchd-injected env already has the token, .env does NOT overwrite."""
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=from-file\n")
        _redirect_repo_env(monkeypatch, env_file)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-launchd")

        _load_home_env()

        # Launchd value preserved, file value ignored.
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-launchd"

    def test_missing_repo_env_falls_back_to_home_env(self, tmp_path, monkeypatch):
        """No repo .env + has ~/.env -> ~/.env loaded."""
        _no_repo_env(monkeypatch)
        home_env = tmp_path / "home.env"
        home_env.write_text("TELEGRAM_BOT_TOKEN=from-home-fallback\n")
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: str(home_env) if p == "~/.env" else p,
        )
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        _load_home_env()

        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-home-fallback"

    def test_missing_env_files_are_silent(self, monkeypatch):
        """If neither file exists, no error, no env mutation."""
        _no_repo_env(monkeypatch)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        before = dict(os.environ)
        _load_home_env()
        for k in set(before) | set(os.environ):
            assert os.environ.get(k) == before.get(k), f"unexpected change to {k}"

    def test_telegram_token_length_revealed_not_value(self, tmp_path, monkeypatch, caplog):
        """The log lines must NOT contain the token value, only its length."""
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=secret-value-1234567890abcdef\n")
        _redirect_repo_env(monkeypatch, env_file)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        with caplog.at_level(logging.INFO):
            _load_home_env()

        # We emit one "env: loaded ... " per candidate and an "env load complete: ..."
        # summary line — one of them has the token length.
        all_msgs = [r.getMessage() for r in caplog.records]
        assert any("secret-value-1234567890abcdef" not in m for m in all_msgs)
        # At least one log line must include the token length (26 chars).
        assert any("26" in m and "token" in m.lower() for m in all_msgs), (
            f"expected token length 26 in some log line; got: {all_msgs}"
        )
