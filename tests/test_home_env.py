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

from listener.daemon import _load_home_env


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


def _redirect_home_env(monkeypatch, target: Path) -> None:
    """Make ~/.env point at target (or nonexistent for no-home-env tests)."""
    monkeypatch.setattr(
        os.path, "expanduser",
        lambda p: str(target) if p == "~/.env" else p,
    )


def _redirect_creds_files(monkeypatch, telegram: Path | None = None,
                           camera: Path | None = None, llm: Path | None = None) -> None:
    """Redirect all per-app creds file paths to safe (or nonexistent) targets."""
    monkeypatch.setattr(
        "infra.paths.TELEGRAM_CREDS_FILE",
        str(telegram) if telegram else "/nonexistent/telegram-creds.env",
    )
    monkeypatch.setattr(
        "infra.paths.CAMERA_CREDS_FILE",
        str(camera) if camera else "/nonexistent/camera-creds.env",
    )
    monkeypatch.setattr(
        "infra.paths.LLM_CREDS_FILE",
        str(llm) if llm else "/nonexistent/llm-creds.env",
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
        _redirect_repo_env(monkeypatch, env_file)
        # Redirect ~/.env to nonexistent so it doesn't interfere.
        _redirect_home_env(monkeypatch, tmp_path / "no_home.env")
        # Redirect creds files to nonexistent so they don't override.
        _redirect_creds_files(monkeypatch)
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
        _redirect_home_env(monkeypatch, tmp_path / "no_home.env")
        _redirect_creds_files(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-launchd")

        _load_home_env()

        # Launchd value preserved, file value ignored.
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-launchd"

    def test_missing_repo_env_falls_back_to_home_env(self, tmp_path, monkeypatch):
        """No repo .env + has ~/.env -> ~/.env loaded."""
        _no_repo_env(monkeypatch)
        home_env = tmp_path / "home.env"
        home_env.write_text("TELEGRAM_BOT_TOKEN=from-home-fallback\n")
        _redirect_home_env(monkeypatch, home_env)
        _redirect_creds_files(monkeypatch)
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

    def test_telegram_token_value_not_in_logs(self, tmp_path, monkeypatch, caplog):
        """The log lines must NOT contain the actual token value."""
        fake_token = "secret-value-1234567890abcdef"  # 29 chars
        env_file = tmp_path / ".env"
        env_file.write_text(f"TELEGRAM_BOT_TOKEN={fake_token}\n")
        _redirect_repo_env(monkeypatch, env_file)
        _redirect_home_env(monkeypatch, tmp_path / "no_home.env")
        _redirect_creds_files(monkeypatch)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        with caplog.at_level(logging.INFO):
            _load_home_env()

        # The token value must NOT appear in any log message.
        all_msgs = [r.getMessage() for r in caplog.records]
        for msg in all_msgs:
            assert fake_token not in msg, (
                f"Token value leaked into log: {msg!r}"
            )
