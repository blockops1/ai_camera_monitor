"""farm_api.config -- Config view resource.

STATUS: alpha - V2-068b9
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - ~/.env (operator's environment file)
    - .env.example (committed, public template)

OUTPUTS:
    - ConfigView dataclass
    - get_config() -> ConfigView

DOES NOT DO:
    - Write or modify any env file
    - Read per-app credential files (camera-creds.env, etc.)

CALLERS:
    - infra.mcp_server (MCP tool adapter: get_config)
    - Any code that needs a read-only view of operational config knobs

USAGE:
    from infra.farm_api.config import get_config
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_SECRETS_RE = re.compile(r"(token|password|key|secret)", re.IGNORECASE)
REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# Known-safe knob prefixes (included in knobs_documented)
# ---------------------------------------------------------------------------

_SAFE_PREFIXES = ("GATE_", "FARMSURV_", "MOTION_", "VISION_", "LISTEN_")

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigView:
    """Read-only snapshot of operational config knobs.

    Attributes:
        env: Mapping of knob name -> redacted value for all knobs present
            in ~/.env (not just the known-safe set).
        knobs_documented: Knob names declared in .env.example.
        knobs_set: Names from knobs_documented that are also in ~/.env.
        knobs_missing: Names from knobs_documented that are NOT in ~/.env.
    """

    env: dict[str, str]
    knobs_documented: list[str]
    knobs_set: list[str]
    knobs_missing: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file into a name->value dict.

    Handles:
        KEY=value
        export KEY=value
        export KEY="value"
        KEY="value"

    Skips blank lines and comment lines.

    Args:
        path: Path to the env file.

    Returns:
        Dict of knob name -> raw value (no redaction).
    """
    result: dict[str, str] = {}
    if not path.is_file():
        return result

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("config: failed to read %s: %s", path, exc)
        return result

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Strip 'export ' prefix if present
        stripped = stripped.removeprefix("export ")

        # Split on first '='
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        # Strip surrounding quotes from value
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value

    return result


def _redact_secrets(d: dict[str, str]) -> dict[str, str]:
    """Return a copy where secret-matching values are replaced with [REDACTED].

    A knob is considered secret if its NAME matches /token|password|key|secret/i.
    The name is preserved; only the value is redacted.

    Args:
        d: Raw key-value mapping.

    Returns:
        New dict with redacted values.
    """
    result: dict[str, str] = {}
    for key, value in d.items():
        if _SECRETS_RE.search(key):
            result[key] = REDACTED
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_config() -> ConfigView:
    """Return a ConfigView snapshot of operational config.

    Reads ~/.env, parses all KEY=value pairs, and diffs against
    .env.example (project root). The known-safe prefix filter
    (GATE_*, FARMSURV_*, MOTION_*, VISION_*, LISTEN_*) determines
    which knobs are surfaced in knobs_documented / knobs_set /
    knobs_missing. Secret-matching knob names get their values
    redacted in the env mapping.

    If ~/.env does not exist, env is empty and all documented knobs
    appear as missing.

    Returns:
        ConfigView with all four fields populated.
    """
    home_env_path = Path.home() / ".env"
    example_path = Path(__file__).resolve().parent.parent / ".env.example"

    # Parse both files
    home_env = _parse_env_file(home_env_path)
    example_env = _parse_env_file(example_path)

    # Filter example knobs to known-safe prefixes
    all_documented: list[str] = sorted(
        k for k in example_env
        if any(k.startswith(pfx) for pfx in _SAFE_PREFIXES)
    )

    # Determine which documented knobs are set vs missing
    knobs_set: list[str] = sorted(
        k for k in all_documented if k in home_env
    )
    knobs_missing: list[str] = sorted(
        k for k in all_documented if k not in home_env
    )

    # Redact secrets in the home-env mapping (all keys, not just safe ones)
    redacted_env = _redact_secrets(home_env)

    return ConfigView(
        env=redacted_env,
        knobs_documented=all_documented,
        knobs_set=knobs_set,
        knobs_missing=knobs_missing,
    )
