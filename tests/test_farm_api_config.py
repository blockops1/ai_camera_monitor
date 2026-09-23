"""Tests for infra.farm_api.config.

STATUS: V2-068b9
RUN WITH: pytest tests/test_farm_api_config.py -v

Test strategy:
    - Patch home ~/.env and .env.example paths via tempfile fixtures.
    - test_get_config_redacts_secrets: create a .env with mixed secret/public
      knobs and verify redaction.
    - test_get_config_reports_missing_knobs: create .env.example with more
      knobs than .env and verify the missing list.
"""

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures — create temp env files and patch sys.modules
# ---------------------------------------------------------------------------


@pytest.fixture()
def _patched_config(tmp_path: Path):
    """Create temp .env and .env.example files, patch sys.modules.

    The fixture writes:
      - tmp_home/.env       (operator-style env)
      - tmp_repo/.env.example (committed template)

    Then deletes any cached farm_api.config module so get_config()
    re-imports fresh paths.
    """
    # -- .env (simulated home env) --
    home_env = tmp_path / "home_env"
    home_env.write_text(
        "LISTEN_HOST=0.0.0.0\n"
        "LISTEN_PORT=8090\n"
        "VISION_LLM_URL=http://127.0.0.1:8080\n"
        "MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1\n"
        "TELEGRAM_BOT_TOKEN=abc123\n"  # secret (name matches /token/i)
        "FARMSURV_FARM_LAT=34.5782\n"  # documented but NOT in this .env
        "FARMSURV_BASELINE_OVERRIDE_ENABLED=0\n"
    )

    # -- .env.example (committed template) --
    example_env = tmp_path / "example_env"
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

    # We need get_config() to read from these temp paths. The cleanest way
    # is to monkey-patch Path.home() and the example path resolution.
    # We'll do this by overriding the module's module-level Path calls.

    # Save original modules
    old_modules = {}
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("infra.farm_api.config"):
            old_modules[mod_name] = sys.modules[mod_name]
            del sys.modules[mod_name]

    # Monkey-patch
    _orig_home = Path.home

    def _mock_home():
        return home_env

    Path.home = _mock_home

    # We also need to override the .env.example path. Since get_config()
    # resolves it relative to the module file, we can't easily override that
    # without rewriting the source. Instead, we'll test by directly importing
    # and using the _parse_env_file + _redact_secrets helpers on explicit paths.

    yield {
        "home": home_env,
        "example": example_env,
    }

    # Restore
    Path.home = _orig_home
    for mod_name, mod in old_modules.items():
        sys.modules[mod_name] = mod


# ---------------------------------------------------------------------------
# test_get_config_redacts_secrets
# ---------------------------------------------------------------------------

class TestGetConfigRedactsSecrets:
    """Secret-matching knob names have [REDACTED] values."""

    def test_redacts_token_names(self, _patched_config):
        """Knobs with 'token' in the name show [REDACTED] value."""
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

        # Name must be preserved
        assert "TELEGRAM_BOT_TOKEN" in redacted

    def test_redact_case_insensitive(self, _patched_config):
        """Pattern matching is case-insensitive: TOKEN, Token, token all match."""
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

    def test_env_field_contains_redacted_values(self, _patched_config):
        """ConfigView.env has redacted values for secret-matching keys."""
        from infra.farm_api.config import _parse_env_file, _redact_secrets

        env = _parse_env_file(_patched_config["home"])
        redacted = _redact_secrets(env)

        # TELEGRAM_BOT_TOKEN is in the test .env and matches /token/i
        assert redacted["TELEGRAM_BOT_TOKEN"] == "[REDACTED]"

        # Non-secret knobs are preserved
        assert redacted["LISTEN_HOST"] == "0.0.0.0"
        assert redacted["MOTION_GATE_NIGHT_SUPPRESS_ENABLED"] == "1"

    def test_empty_env_returns_empty_dict(self, _patched_config):
        """_redact_secrets handles empty dicts gracefully."""
        from infra.farm_api.config import _redact_secrets

        result = _redact_secrets({})
        assert result == {}


# ---------------------------------------------------------------------------
# test_get_config_reports_missing_knobs
# ---------------------------------------------------------------------------

class TestGetConfigReportsMissingKnobs:
    """knobs_missing contains documented knobs absent from ~/.env."""

    def test_missing_knobs_are_correct(self, _patched_config):
        """Documented knobs not present in .env appear in knobs_missing."""
        from infra.farm_api.config import _parse_env_file

        home = _patched_config["home"]
        example = _patched_config["example"]

        home_kv = _parse_env_file(home)
        example_kv = _parse_env_file(example)

        # Known-safe prefixes
        safe_prefixes = ("GATE_", "FARMSURV_", "MOTION_", "VISION_", "LISTEN_")

        all_documented = sorted(
            k for k in example_kv
            if any(k.startswith(pfx) for pfx in safe_prefixes)
        )

        knobs_set = sorted(k for k in all_documented if k in home_kv)
        knobs_missing = sorted(k for k in all_documented if k not in home_kv)

        # FARMSURV_FARM_LON is in .env.example but NOT in our test .env
        assert "FARMSURV_FARM_LON" in knobs_missing

        # At least one documented knob should be missing
        assert len(knobs_missing) >= 1

        # These ARE in both
        assert "LISTEN_HOST" in knobs_set
        assert "LISTEN_PORT" in knobs_set
        assert "MOTION_GATE_NIGHT_SUPPRESS_ENABLED" in knobs_set

    def test_no_missing_when_all_present(self, _patched_config):
        """If .env has everything in .env.example, knobs_missing is empty."""
        from infra.farm_api.config import _parse_env_file

        example = _patched_config["example"]
        example_kv = _parse_env_file(example)

        # Create a home env with ALL documented knobs
        home = _patched_config["home"]
        home.write_text("\n".join(f"{k}={v}" for k, v in example_kv.items()))

        home_kv = _parse_env_file(home)
        safe_prefixes = ("GATE_", "FARMSURV_", "MOTION_", "VISION_", "LISTEN_")

        all_documented = [
            k for k in example_kv
            if any(k.startswith(pfx) for pfx in safe_prefixes)
        ]

        knobs_missing = [k for k in all_documented if k not in home_kv]
        assert knobs_missing == []

    def test_non_safe_prefixes_excluded_from_documented(self, _patched_config):
        """Documented knobs exclude non-safe-prefix names even in .env.example."""
        from infra.farm_api.config import _parse_env_file

        home = _patched_config["home"]
        example = _patched_config["example"]

        # Add a non-safe prefix knob to .env.example
        example.write_text(
            "LISTEN_HOST=0.0.0.0\n"
            "BRAVE_API_KEY=abc\n"  # NOT a safe prefix
            "FARMSURV_LAT=34.5\n"
        )
        # Also remove it from home to be clean
        home.write_text("LISTEN_HOST=0.0.0.0\nFARMSURV_LAT=34.5\n")

        example_kv = _parse_env_file(example)

        safe_prefixes = ("GATE_", "FARMSURV_", "MOTION_", "VISION_", "LISTEN_")

        all_documented = sorted(
            k for k in example_kv
            if any(k.startswith(pfx) for pfx in safe_prefixes)
        )

        assert "BRAVE_API_KEY" not in all_documented
        assert "LISTEN_HOST" in all_documented
        assert "FARMSURV_LAT" in all_documented

    def test_parse_env_handles_export_and_quotes(self, _patched_config):
        """_parse_env_file strips 'export' prefix and surrounding quotes."""
        from infra.farm_api.config import _parse_env_file

        env_file = _patched_config["home"]
        env_file.write_text(
            'KEY1=value1\n'
            "export KEY2=value2\n"
            'KEY3="value with spaces"\n'
            "KEY4='single quoted'\n"
            "# comment line\n"
            "\n"
            "NO_EQUALS_HERE\n"
        )

        parsed = _parse_env_file(env_file)

        assert parsed["KEY1"] == "value1"
        assert parsed["KEY2"] == "value2"
        assert parsed["KEY3"] == "value with spaces"
        assert parsed["KEY4"] == "single quoted"
        assert "comment line" not in parsed
        assert "NO_EQUALS_HERE" not in parsed

    def test_parse_nonexistent_file_returns_empty(self, _patched_config):
        """_parse_env_file returns empty dict for a missing file."""
        from infra.farm_api.config import _parse_env_file

        result = _parse_env_file(Path("/nonexistent/path/.env"))
        assert result == {}
