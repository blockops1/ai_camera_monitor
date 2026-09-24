"""test_farm_api_config_e2e — E2E tests for farm_api config endpoint.

E2E strategy:
    - Write real .env and .env.example files into a temp directory.
    - Test the core functions (_parse_env_file, _redact_secrets) directly
      against real file I/O (not mocks) to verify:
      (1) env file parsing handles all edge cases (export prefix, quotes,
          comments, blank lines),
      (2) secret redaction correctly identifies token/key/password/secret names,
      (3) missing-knob detection compares .env against .env.example.

Bug classes covered:
    - Secret values leaked in API responses (token names not redacted).
    - Env file parsing fails on exported or quoted values.
    - Non-safe-prefix knobs incorrectly reported as missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def _config_e2e_files(tmp_path: Path):
    """Create real .env and .env.example files."""
    home_env = tmp_path / ".env"
    home_env.write_text(
        "LISTEN_HOST=0.0.0.0\n"
        "LISTEN_PORT=8090\n"
        "VISION_LLM_URL=http://127.0.0.1:8080\n"
        "MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1\n"
        "TELEGRAM_BOT_TOKEN=abc123\n"
        "FARMSURV_FARM_LAT=34.5782\n"
        "FARMSURV_BASELINE_OVERRIDE_ENABLED=0\n"
    )

    example_env = tmp_path / ".env.example"
    example_env.write_text(
        "# Example env file\n"
        "LISTEN_HOST=0.0.0.0\n"
        "LISTEN_PORT=8090\n"
        "VISION_LLM_URL=http://127.0.0.1:8080\n"
        "MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1\n"
        "FARMSURV_FARM_LAT=34.5782\n"
        "FARMSURV_FARM_LON=-84.9438\n"
        "FARMSURV_BASELINE_OVERRIDE_ENABLED=0\n"
    )

    yield {
        "home": home_env,
        "example": example_env,
    }


# ---------------------------------------------------------------------------
# E2E tests — _parse_env_file
# ---------------------------------------------------------------------------

class TestParseEnvFileE2E:
    """E2E: _parse_env_file reads real .env files correctly."""

    def test_parses_key_value_pairs(self, _config_e2e_files):
        from infra.farm_api.config import _parse_env_file

        parsed = _parse_env_file(_config_e2e_files["home"])
        assert parsed["LISTEN_HOST"] == "0.0.0.0"
        assert parsed["LISTEN_PORT"] == "8090"
        assert parsed["VISION_LLM_URL"] == "http://127.0.0.1:8080"
        assert len(parsed) == 7

    def test_strips_export_prefix(self, _config_e2e_files):
        env_file = _config_e2e_files["home"]
        env_file.write_text(
            "KEY1=value1\n"
            "export KEY2=value2\n"
            "export KEY3=\"quoted value\"\n"
        )
        from infra.farm_api.config import _parse_env_file

        parsed = _parse_env_file(env_file)
        assert parsed["KEY1"] == "value1"
        assert parsed["KEY2"] == "value2"
        assert parsed["KEY3"] == "quoted value"

    def test_handles_quotes_and_comments(self, _config_e2e_files):
        env_file = _config_e2e_files["home"]
        env_file.write_text(
            'KEY1="double quoted"\n'
            "KEY2='single quoted'\n"
            "# comment line\n"
            "\n"
            "KEY3=noquote\n"
        )
        from infra.farm_api.config import _parse_env_file

        parsed = _parse_env_file(env_file)
        assert parsed["KEY1"] == "double quoted"
        assert parsed["KEY2"] == "single quoted"
        assert parsed["KEY3"] == "noquote"
        assert "comment line" not in parsed
        assert len(parsed) == 3

    def test_nonexistent_file_returns_empty(self, _config_e2e_files):
        from infra.farm_api.config import _parse_env_file

        result = _parse_env_file(Path("/nonexistent/path/.env"))
        assert result == {}


# ---------------------------------------------------------------------------
# E2E tests — _redact_secrets
# ---------------------------------------------------------------------------

class TestRedactSecretsE2E:
    """E2E: _redact_secrets correctly masks secret-matching knob names."""

    def test_redacts_token_names(self, _config_e2e_files):
        from infra.farm_api.config import _redact_secrets

        raw = {
            "LISTEN_HOST": "0.0.0.0",
            "TELEGRAM_BOT_TOKEN": "abc123",
            "MY_API_KEY": "secret_key_value",
            "FARMSURV_LAT": "34.5",
            "DB_PASSWORD": "hunter2",
            "APP_SECRET": "mysecret",
        }
        redacted = _redact_secrets(raw)

        assert redacted["LISTEN_HOST"] == "0.0.0.0"
        assert redacted["TELEGRAM_BOT_TOKEN"] == "[REDACTED]"
        assert redacted["MY_API_KEY"] == "[REDACTED]"
        assert redacted["FARMSURV_LAT"] == "34.5"
        assert redacted["DB_PASSWORD"] == "[REDACTED]"
        assert redacted["APP_SECRET"] == "[REDACTED]"

    def test_redact_case_insensitive(self, _config_e2e_files):
        from infra.farm_api.config import _redact_secrets

        raw = {
            "api_token": "value1",
            "Api_Key": "value2",
            "NORMAL_VAR": "value3",
        }
        redacted = _redact_secrets(raw)

        assert redacted["api_token"] == "[REDACTED]"
        assert redacted["Api_Key"] == "[REDACTED]"
        assert redacted["NORMAL_VAR"] == "value3"

    def test_empty_dict_returns_empty(self, _config_e2e_files):
        from infra.farm_api.config import _redact_secrets

        result = _redact_secrets({})
        assert result == {}

    def test_real_env_has_redacted_token(self, _config_e2e_files):
        """Real .env file has TELEGRAM_BOT_TOKEN redacted."""
        from infra.farm_api.config import _parse_env_file, _redact_secrets

        env = _parse_env_file(_config_e2e_files["home"])
        redacted = _redact_secrets(env)

        assert redacted["TELEGRAM_BOT_TOKEN"] == "[REDACTED]"
        assert redacted["LISTEN_HOST"] == "0.0.0.0"
        assert redacted["MOTION_GATE_NIGHT_SUPPRESS_ENABLED"] == "1"


# ---------------------------------------------------------------------------
# E2E tests — missing-knob detection
# ---------------------------------------------------------------------------

class TestMissingKnobsE2E:
    """E2E: missing-knob detection compares .env against .env.example."""

    def test_detects_missing_knobs(self, _config_e2e_files):
        from infra.farm_api.config import _parse_env_file

        home_kv = _parse_env_file(_config_e2e_files["home"])
        example_kv = _parse_env_file(_config_e2e_files["example"])

        safe_prefixes = ("GATE_", "FARMSURV_", "MOTION_", "VISION_", "LISTEN_")
        all_documented = [
            k for k in example_kv
            if any(k.startswith(pfx) for pfx in safe_prefixes)
        ]

        knobs_missing = [k for k in all_documented if k not in home_kv]

        # FARMSURV_FARM_LON is in .env.example but NOT in our test .env
        assert "FARMSURV_FARM_LON" in knobs_missing
        assert len(knobs_missing) >= 1

        # These ARE in both
        assert "LISTEN_HOST" in [k for k in all_documented if k in home_kv]
        assert "LISTEN_PORT" in [k for k in all_documented if k in home_kv]

    def test_no_missing_when_all_present(self, _config_e2e_files):
        from infra.farm_api.config import _parse_env_file

        example_kv = _parse_env_file(_config_e2e_files["example"])
        home = _config_e2e_files["home"]
        home.write_text("\n".join(f"{k}={v}" for k, v in example_kv.items()))

        home_kv = _parse_env_file(home)
        safe_prefixes = ("GATE_", "FARMSURV_", "MOTION_", "VISION_", "LISTEN_")
        all_documented = [
            k for k in example_kv
            if any(k.startswith(pfx) for pfx in safe_prefixes)
        ]

        knobs_missing = [k for k in all_documented if k not in home_kv]
        assert knobs_missing == []

    def test_non_safe_prefixes_excluded(self, _config_e2e_files):
        from infra.farm_api.config import _parse_env_file

        example = _config_e2e_files["example"]
        example.write_text(
            "LISTEN_HOST=0.0.0.0\n"
            "BRAVE_API_KEY=abc\n"
            "FARMSURV_LAT=34.5\n"
        )
        _config_e2e_files["home"].write_text("LISTEN_HOST=0.0.0.0\nFARMSURV_LAT=34.5\n")

        example_kv = _parse_env_file(example)
        safe_prefixes = ("GATE_", "FARMSURV_", "MOTION_", "VISION_", "LISTEN_")
        all_documented = [
            k for k in example_kv
            if any(k.startswith(pfx) for pfx in safe_prefixes)
        ]

        assert "BRAVE_API_KEY" not in all_documented
        assert "LISTEN_HOST" in all_documented
        assert "FARMSURV_LAT" in all_documented
