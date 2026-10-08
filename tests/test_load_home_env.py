"""
test_load_home_env.py — US-101z: per-app creds file loading in
listener/daemon.py::_load_home_env().

Verifies the three-tier precedence chain:
    <repo>/.env < ~/.env < *-creds.env (last wins).

All tests use tmp_path and never touch real files.
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


class TestLoadHomeEnv:
    """Tests for listener.daemon._load_home_env() and per-app creds loading."""

    # ------------------------------------------------------------------
    # F2: repo env wins over home env (keep existing behavior)
    # ------------------------------------------------------------------

    def test_repo_env_wins_over_home_env(self, tmp_path, monkeypatch):
        """Values from <repo>/.env win over ~/.env for the same key."""
        repo_env = tmp_path / "repo.env"
        repo_env.write_text(
            "SHARED_KEY=from-repo\n"
            "REPO_ONLY=repo-value\n"
        )
        home_env = tmp_path / "home.env"
        home_env.write_text(
            "SHARED_KEY=from-home\n"
            "HOME_ONLY=home-value\n"
        )

        _redirect_repo_env(monkeypatch, repo_env)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: str(home_env) if p == "~/.env" else p,
        )
        monkeypatch.delenv("SHARED_KEY", raising=False)
        monkeypatch.delenv("REPO_ONLY", raising=False)
        monkeypatch.delenv("HOME_ONLY", raising=False)

        _load_home_env()

        assert os.environ["SHARED_KEY"] == "from-repo"
        assert os.environ["REPO_ONLY"] == "repo-value"
        assert os.environ["HOME_ONLY"] == "home-value"

    # ------------------------------------------------------------------
    # F1/F2: creds env wins over repo env (new behavior)
    # ------------------------------------------------------------------

    def test_creds_env_wins_over_repo_env(self, tmp_path, monkeypatch):
        """Values from telegram-creds.env override <repo>/.env."""
        repo_env = tmp_path / "repo.env"
        repo_env.write_text(
            "TELEGRAM_BOT_TOKEN=from-repo-env\n"
            "TELEGRAM_HOME_CHAT_ID=999\n"
        )
        creds_env = tmp_path / "telegram-creds.env"
        creds_env.write_text(
            "TELEGRAM_BOT_TOKEN=from-creds\n"
            "TELEGRAM_HOME_CHAT_ID=888\n"
        )

        _redirect_repo_env(monkeypatch, repo_env)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        monkeypatch.setattr(
            "infra.paths.TELEGRAM_CREDS_FILE",
            str(creds_env),
        )
        monkeypatch.setattr(
            "infra.paths.CAMERA_CREDS_FILE",
            "/nonexistent/camera-creds.env",
        )
        monkeypatch.setattr(
            "infra.paths.LLM_CREDS_FILE",
            "/nonexistent/llm-creds.env",
        )
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_HOME_CHAT_ID", raising=False)

        _load_home_env()

        # Creds env wins over repo env for both keys
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "from-creds"
        assert os.environ["TELEGRAM_HOME_CHAT_ID"] == "888"

    def test_camera_creds_env_loaded(self, tmp_path, monkeypatch):
        """camera-creds.env keys are loaded and win over .env."""
        repo_env = tmp_path / "repo.env"
        repo_env.write_text("FRONT_IP=192.168.1.1\n")
        creds_env = tmp_path / "camera-creds.env"
        creds_env.write_text("FRONT_IP=192.168.1.99\n")

        _redirect_repo_env(monkeypatch, repo_env)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        monkeypatch.setattr(
            "infra.paths.TELEGRAM_CREDS_FILE",
            "/nonexistent/telegram-creds.env",
        )
        monkeypatch.setattr(
            "infra.paths.CAMERA_CREDS_FILE",
            str(creds_env),
        )
        monkeypatch.setattr(
            "infra.paths.LLM_CREDS_FILE",
            "/nonexistent/llm-creds.env",
        )
        monkeypatch.delenv("FRONT_IP", raising=False)

        _load_home_env()

        assert os.environ["FRONT_IP"] == "192.168.1.99"

    def test_llm_creds_env_loaded(self, tmp_path, monkeypatch):
        """llm-creds.env keys are loaded and win over .env."""
        repo_env = tmp_path / "repo.env"
        repo_env.write_text("VISION_LLM_URL=http://wrong:1234\n")
        creds_env = tmp_path / "llm-creds.env"
        creds_env.write_text("VISION_LLM_URL=http://right:5678\n")

        _redirect_repo_env(monkeypatch, repo_env)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        monkeypatch.setattr(
            "infra.paths.TELEGRAM_CREDS_FILE",
            "/nonexistent/telegram-creds.env",
        )
        monkeypatch.setattr(
            "infra.paths.CAMERA_CREDS_FILE",
            "/nonexistent/camera-creds.env",
        )
        monkeypatch.setattr(
            "infra.paths.LLM_CREDS_FILE",
            str(creds_env),
        )
        monkeypatch.delenv("VISION_LLM_URL", raising=False)

        _load_home_env()

        assert os.environ["VISION_LLM_URL"] == "http://right:5678"

    # ------------------------------------------------------------------
    # F3: missing creds files is ok
    # ------------------------------------------------------------------

    def test_missing_creds_files_is_ok(self, tmp_path, monkeypatch):
        """No error if all creds files are absent."""
        _no_repo_env(monkeypatch)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        monkeypatch.setattr(
            "infra.paths.TELEGRAM_CREDS_FILE",
            "/nonexistent/telegram-creds.env",
        )
        monkeypatch.setattr(
            "infra.paths.CAMERA_CREDS_FILE",
            "/nonexistent/camera-creds.env",
        )
        monkeypatch.setattr(
            "infra.paths.LLM_CREDS_FILE",
            "/nonexistent/llm-creds.env",
        )
        before = dict(os.environ)

        _load_home_env()

        # No keys should have been added (all files missing)
        for k in set(before) | set(os.environ):
            assert os.environ.get(k) == before.get(k), f"unexpected change to {k}"

    # ------------------------------------------------------------------
    # F4: mode 600 warning if creds file world-readable
    # ------------------------------------------------------------------

    def test_mode_600_warning_if_creds_file_world_readable(
        self, tmp_path, monkeypatch, caplog
    ):
        """If a creds file has world-readable bits, a WARNING is logged."""
        creds_env = tmp_path / "telegram-creds.env"
        creds_env.write_text("TELEGRAM_BOT_TOKEN=test-token\n")
        # Set mode to 644 (owner rw, group r, other r)
        creds_env.chmod(0o644)

        _no_repo_env(monkeypatch)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        monkeypatch.setattr(
            "infra.paths.TELEGRAM_CREDS_FILE",
            str(creds_env),
        )
        monkeypatch.setattr(
            "infra.paths.CAMERA_CREDS_FILE",
            "/nonexistent/camera-creds.env",
        )
        monkeypatch.setattr(
            "infra.paths.LLM_CREDS_FILE",
            "/nonexistent/llm-creds.env",
        )
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        with caplog.at_level(logging.INFO):
            _load_home_env()

        assert any(
            "world-readable" in r.getMessage()
            for r in caplog.records
        ), f"Expected world-readable warning; got: {[r.getMessage() for r in caplog.records]}"

    def test_no_warning_for_mode_600_creds_file(self, tmp_path, monkeypatch, caplog):
        """If creds file is mode 600, no world-readable warning is logged."""
        creds_env = tmp_path / "telegram-creds.env"
        creds_env.write_text("TELEGRAM_BOT_TOKEN=test-token\n")
        creds_env.chmod(0o600)

        _no_repo_env(monkeypatch)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        monkeypatch.setattr(
            "infra.paths.TELEGRAM_CREDS_FILE",
            str(creds_env),
        )
        monkeypatch.setattr(
            "infra.paths.CAMERA_CREDS_FILE",
            "/nonexistent/camera-creds.env",
        )
        monkeypatch.setattr(
            "infra.paths.LLM_CREDS_FILE",
            "/nonexistent/llm-creds.env",
        )
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

        with caplog.at_level(logging.INFO):
            _load_home_env()

        assert not any(
            "world-readable" in r.getMessage()
            for r in caplog.records
        ), "Should not warn for properly-secured creds file"
