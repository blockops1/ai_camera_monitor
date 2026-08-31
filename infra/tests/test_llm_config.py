"""
Tests for infra/llm_config.py — Vision + text LLM endpoint config.

Covers:
    - DEFAULT URL/token/model values match pre-6B.146 hard-coded values
    - llm-creds.env file loading (all 6 fields)
    - env var overrides (env wins over file)
    - file value overrides default
    - token never logged
    - missing fields → defaults
    - malformed file → fallback to defaults
    - reset_for_tests() clears cache
    - auth_headers() returns Bearer when token set, empty when not
"""

import logging

import pytest

from infra.llm_config import (
    TextLLMConfig,
    VisionLLMConfig,
    _parse_llm_creds_file,
    load_text_config,
    load_vision_config,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_llm_config_cache():
    """Each test starts with a clean config cache."""
    reset_for_tests()
    yield
    reset_for_tests()


# =============================================================================
# Defaults (no env, no file) — must match pre-6B.146 hard-coded values
# =============================================================================


class TestDefaults:
    """When llm-creds.env is absent and no env vars are set, defaults
    must match the previously hard-coded values (zero behavior change)."""

    def test_vision_default_url(self, monkeypatch, tmp_path, caplog):
        # Point the file parser at a non-existent path so the file
        # branch is empty. Clear env to ensure defaults.
        monkeypatch.setattr(
            "infra.llm_config.LLM_CREDS_FILE", str(tmp_path / "no-such-file.env")
        )
        monkeypatch.delenv("VISION_LLM_URL", raising=False)
        monkeypatch.delenv("VISION_LLM_TOKEN", raising=False)
        monkeypatch.delenv("VISION_LLM_MODEL", raising=False)
        with caplog.at_level(logging.INFO, logger="llm_config"):
            cfg = load_vision_config()
        assert cfg.url == "http://127.0.0.1:8093/v1/chat/completions"
        assert cfg.model == "qwen3.6"
        assert cfg.token == ""

    def test_text_default_url(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "infra.llm_config.LLM_CREDS_FILE", str(tmp_path / "no-such-file.env")
        )
        monkeypatch.delenv("TEXT_LLM_URL", raising=False)
        monkeypatch.delenv("TEXT_LLM_TOKEN", raising=False)
        monkeypatch.delenv("TEXT_LLM_MODEL", raising=False)
        cfg = load_text_config()
        assert cfg.url == "http://127.0.0.1:8093/v1/chat/completions"
        assert cfg.model == "qwen3.6"
        assert cfg.token == ""

    def test_token_never_logged(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(
            "infra.llm_config.LLM_CREDS_FILE", str(tmp_path / "no-such-file.env")
        )
        monkeypatch.setenv("VISION_LLM_TOKEN", "sk-supersecrettoken-XYZ-9999")
        with caplog.at_level(logging.INFO, logger="llm_config"):
            load_vision_config()
        # Token must NOT appear in any log record
        for record in caplog.records:
            assert "sk-supersecrettoken" not in record.getMessage()
            assert record.getMessage()  # but the log line does exist


# =============================================================================
# File loading
# =============================================================================


class TestFileLoading:
    """When llm-creds.env is present, file values populate the config."""

    def test_file_value_used_when_no_env(self, monkeypatch, tmp_path):
        creds = tmp_path / "llm-creds.env"
        creds.write_text(
            "VISION_LLM_URL=http://custom.example.com:9999/v1/chat/completions\n"
            "VISION_LLM_TOKEN=file-vision-token\n"
            "VISION_LLM_MODEL=gpt-4o\n"
            "TEXT_LLM_URL=http://custom.example.com:9998/v1/chat/completions\n"
            "TEXT_LLM_TOKEN=file-text-token\n"
            "TEXT_LLM_MODEL=gpt-4o-mini\n"
        )
        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        for var in (
            "VISION_LLM_URL", "VISION_LLM_TOKEN", "VISION_LLM_MODEL",
            "TEXT_LLM_URL", "TEXT_LLM_TOKEN", "TEXT_LLM_MODEL",
        ):
            monkeypatch.delenv(var, raising=False)

        v = load_vision_config()
        assert v.url == "http://custom.example.com:9999/v1/chat/completions"
        assert v.token == "file-vision-token"
        assert v.model == "gpt-4o"

        t = load_text_config()
        assert t.url == "http://custom.example.com:9998/v1/chat/completions"
        assert t.token == "file-text-token"
        assert t.model == "gpt-4o-mini"

    def test_quoted_values_stripped(self, monkeypatch, tmp_path):
        creds = tmp_path / "llm-creds.env"
        creds.write_text(
            'VISION_LLM_TOKEN="quoted-token"\n'
            "VISION_LLM_URL='single-quoted-url'\n"
        )
        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        monkeypatch.delenv("VISION_LLM_URL", raising=False)
        monkeypatch.delenv("VISION_LLM_TOKEN", raising=False)

        v = load_vision_config()
        assert v.token == "quoted-token"
        assert v.url == "single-quoted-url"

    def test_comments_and_blanks_skipped(self, monkeypatch, tmp_path):
        creds = tmp_path / "llm-creds.env"
        creds.write_text(
            "# Top comment\n"
            "\n"
            "VISION_LLM_MODEL=file-model\n"
            "# Inline comment\n"
            "TEXT_LLM_MODEL=text-model\n"
            "no-equals-sign-line\n"
        )
        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        monkeypatch.delenv("VISION_LLM_MODEL", raising=False)
        monkeypatch.delenv("TEXT_LLM_MODEL", raising=False)

        v = load_vision_config()
        assert v.model == "file-model"
        t = load_text_config()
        assert t.model == "text-model"

    def test_partial_file_uses_defaults_for_missing_fields(
        self, monkeypatch, tmp_path
    ):
        # File has only VISION_LLM_URL; the other fields fall back to defaults.
        creds = tmp_path / "llm-creds.env"
        creds.write_text("VISION_LLM_URL=http://only-url-set.example.com\n")
        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        for var in (
            "VISION_LLM_URL", "VISION_LLM_TOKEN", "VISION_LLM_MODEL",
        ):
            monkeypatch.delenv(var, raising=False)

        v = load_vision_config()
        assert v.url == "http://only-url-set.example.com"
        assert v.token == ""  # default
        assert v.model == "qwen3.6"  # default


# =============================================================================
# env-var override (env wins over file)
# =============================================================================


class TestEnvOverrides:
    """Environment variables win over file values."""

    def test_env_url_wins_over_file(self, monkeypatch, tmp_path):
        creds = tmp_path / "llm-creds.env"
        creds.write_text("VISION_LLM_URL=http://file-url.example.com\n")
        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        monkeypatch.setenv("VISION_LLM_URL", "http://env-url.example.com")

        v = load_vision_config()
        assert v.url == "http://env-url.example.com"

    def test_env_token_wins_over_file(self, monkeypatch, tmp_path):
        creds = tmp_path / "llm-creds.env"
        creds.write_text("VISION_LLM_TOKEN=file-token\n")
        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        monkeypatch.setenv("VISION_LLM_TOKEN", "env-token")

        v = load_vision_config()
        assert v.token == "env-token"

    def test_env_model_wins_over_file(self, monkeypatch, tmp_path):
        creds = tmp_path / "llm-creds.env"
        creds.write_text("VISION_LLM_MODEL=file-model\n")
        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        monkeypatch.setenv("VISION_LLM_MODEL", "env-model")

        v = load_vision_config()
        assert v.model == "env-model"


# =============================================================================
# Edge cases
# =============================================================================


class TestEdgeCases:
    """Defensive parsing: malformed input doesn't crash the listener."""

    def test_missing_file_returns_empty_dict(self):
        # _parse_llm_creds_file is the raw parser; must NOT raise on
        # missing file — the listener loads it at import time.
        assert _parse_llm_creds_file("/no/such/file.env") == {}

    def test_unreadable_file_falls_back_to_defaults(
        self, monkeypatch, tmp_path, caplog
    ):
        creds = tmp_path / "bad-perms.env"
        creds.write_text("VISION_LLM_MODEL=should-not-load\n")
        # Make unreadable (skip on platforms where chmod is restricted)
        import os
        try:
            os.chmod(creds, 0o000)
        except (OSError, PermissionError):
            pytest.skip("Cannot chmod on this platform")

        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        monkeypatch.delenv("VISION_LLM_MODEL", raising=False)

        with caplog.at_level(logging.WARNING, logger="llm_config"):
            v = load_vision_config()
        # Defaults preserved, file value ignored.
        assert v.model == "qwen3.6"
        # Cleanup so pytest can remove the file
        os.chmod(creds, 0o644)

    def test_reset_for_tests_clears_cache(
        self, monkeypatch, tmp_path
    ):
        creds = tmp_path / "llm-creds.env"
        creds.write_text("VISION_LLM_MODEL=first-model\n")
        monkeypatch.setattr("infra.llm_config.LLM_CREDS_FILE", str(creds))
        monkeypatch.delenv("VISION_LLM_MODEL", raising=False)
        v1 = load_vision_config()
        assert v1.model == "first-model"

        # Update file, but cache is still warm — should NOT pick up.
        creds.write_text("VISION_LLM_MODEL=second-model\n")
        v2 = load_vision_config()
        assert v2.model == "first-model"  # cached

        # Clear cache; now picks up new value.
        reset_for_tests()
        v3 = load_vision_config()
        assert v3.model == "second-model"

    def test_cached_result_is_same_instance(self):
        # lru_cache(maxsize=1) means same dict returned on repeat calls
        v1 = load_vision_config()
        v2 = load_vision_config()
        assert v1 is v2


# =============================================================================
# auth_headers() helper
# =============================================================================


class TestAuthHeaders:
    """The auth_headers() helper must return Bearer when token set."""

    def test_empty_token_returns_empty_dict(self):
        cfg = VisionLLMConfig(
            url="http://x", token="", model="qwen3.6"
        )
        assert cfg.auth_headers() == {}

    def test_set_token_returns_bearer(self):
        cfg = VisionLLMConfig(
            url="http://x", token="sk-test-abc", model="qwen3.6"
        )
        assert cfg.auth_headers() == {"Authorization": "Bearer sk-test-abc"}

    def test_text_config_same_behavior(self):
        cfg = TextLLMConfig(url="http://x", token="tk-text", model="qwen3.6")
        assert cfg.auth_headers() == {"Authorization": "Bearer tk-text"}
        cfg2 = TextLLMConfig(url="http://x", token="", model="qwen3.6")
        assert cfg2.auth_headers() == {}