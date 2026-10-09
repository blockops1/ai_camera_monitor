"""
test_load_home_env.py — US-103x-2: import-order fix for _load_home_env.

Verifies that _load_home_env loads .env BEFORE importing infra, so that
infra modules that call _require_env_lower_bool() at import time don't
hit a RuntimeError due to empty os.environ.

All tests use tmp_path and never touch real files.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redirect_repo_env(monkeypatch, target: Path) -> None:
    """Patch the module-level _REPO_ENV_PATH so it points at target."""
    import listener.daemon
    monkeypatch.setattr(
        listener.daemon,
        "_REPO_ENV_PATH",
        str(target),
    )


def _no_repo_env(monkeypatch) -> None:
    """Make the repo .env appear absent for this test."""
    import listener.daemon
    monkeypatch.setattr(
        listener.daemon,
        "_REPO_ENV_PATH",
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
    from infra import paths as infra_paths  # noqa: F401 - trigger infra import
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


# ---------------------------------------------------------------------------
# US-103x-2: import-order fix tests
# ---------------------------------------------------------------------------

class TestImportOrderFix:
    """Tests for the import-order fix (US-103x-2)."""

    def test_load_home_env_before_infra_import(
        self, tmp_path, monkeypatch, caplog
    ):
        """AC3: No RuntimeError when loading .env before infra import.

        The bug was that infra.__init__ -> alert_overrides_baseline ->
        quick_classifier called _require_env_lower_bool("MOTION_GATE_NIGHT_
        SUPPRESS_ENABLED") at import time, which raised RuntimeError because
        os.environ was empty.

        This test creates a tmp .env with that key, calls _load_home_env,
        and asserts no RuntimeError is raised.
        """
        import listener.daemon

        monkeypatch.delenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", raising=False)
        _no_repo_env(monkeypatch)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        _redirect_creds_files(monkeypatch)

        repo_env = tmp_path / ".env"
        repo_env.write_text(
            "MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1\n"
            "TEST_LOAD_VAR=hello\n"
        )
        _redirect_repo_env(monkeypatch, repo_env)

        with caplog.at_level(logging.INFO):
            loaded = listener.daemon._load_home_env()

        assert os.environ.get("MOTION_GATE_NIGHT_SUPPRESS_ENABLED") == "1"
        assert os.environ.get("TEST_LOAD_VAR") == "hello"
        assert isinstance(loaded, int)
        assert loaded >= 2

    def test_existing_env_wins_over_file_with_fix(self, tmp_path, monkeypatch):
        """Existing env var (e.g. from launchd) is NOT overwritten."""
        import listener.daemon

        monkeypatch.setenv("TEST_KEY", "from-launchd")
        _no_repo_env(monkeypatch)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        _redirect_creds_files(monkeypatch)

        repo_env = tmp_path / ".env"
        repo_env.write_text("TEST_KEY=from-file\n")
        _redirect_repo_env(monkeypatch, repo_env)

        loaded = listener.daemon._load_home_env()

        assert os.environ["TEST_KEY"] == "from-launchd"
        assert loaded == 0

    def test_env_total_accurate(self, tmp_path, monkeypatch):
        """total_loaded counts only keys we actually set, not pre-existing ones."""
        import listener.daemon

        monkeypatch.setenv("PRE_EXISTING_KEY", "launchd-value")

        _no_repo_env(monkeypatch)
        monkeypatch.setattr(
            os.path, "expanduser",
            lambda p: "/nonexistent/.env" if p == "~/.env" else p,
        )
        _redirect_creds_files(monkeypatch)

        repo_env = tmp_path / ".env"
        repo_env.write_text(
            "PRE_EXISTING_KEY=from-env\n"
            "NEW_FROM_ENV=from-env\n"
        )
        _redirect_repo_env(monkeypatch, repo_env)

        loaded = listener.daemon._load_home_env()

        assert loaded == 1
