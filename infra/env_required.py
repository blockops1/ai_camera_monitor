"""env_required.py -- fail-loud env-var loader for operator-significant knobs.

STATUS: new (US-069-fail-loud-env)
THREAD SAFETY: single-threaded, no shared mutable state
IMPORT ORDER: stdlib -> third-party -> local (infra.)

INPUTS:
    - os.environ keys (operator-significant env vars)

OUTPUTS:
    - parsed values (str, bool, list[str])
    - RuntimeError if required var is missing

DOES NOT DO:
    - Silent fallback to defaults
    - Hardcoded values
    - Reading from config files or databases

CALLED BY:
    - infra.quick_classifier (module-level _require_env calls)
    - infra.quiet_hours (module-level _require_env calls)
    - infra.alert_overrides_baseline (module-level _require_env calls)
    - infra.api_server (module-level _require_env calls)
    - infra.mcp_server (module-level _require_env calls)
    - listener.daemon (startup _require_env calls)

RATIONALE:
    2026-09-25 OUTSIDE_BACK_SOLAR vehicle alert slipped through V2-069 night
    suppress. Root cause: module-level os.environ.get(...) calls captured
    values BEFORE _load_home_env() ran. The fix is fail-loud: if an
    operator-significant knob is missing, the process refuses to start
    rather than silently falling back to a default.
"""

from __future__ import annotations

import os


def _require_env(name: str) -> str:
    """Read an env var or raise RuntimeError if unset.

    Args:
        name: environment variable name (e.g. ``"FARMSURV_API_TOKEN"``).

    Returns:
        The value as a string.

    Raises:
        RuntimeError: if the variable is unset in os.environ.
    """
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(
            f"{name} is not set in os.environ. "
            f"Set it in your .env file (see .env.example for the minimal set). "
            f"Example: {name}=<value>"
        )
    return value


def _require_env_lower_bool(name: str) -> bool:
    """Read an env var as a lowercase truthy boolean.

    Reads ``os.environ[name]`` (via _require_env, so it fails if unset),
    lowercases the value, and checks against '1', 'true', 'yes', 'on'.

    Args:
        name: environment variable name (e.g. ``"FARMSURV_QUIET_HOURS_ENABLED"``).

    Returns:
        True if the value is truthy, False otherwise.

    Raises:
        RuntimeError: if the variable is unset.
    """
    value = _require_env(name).lower().strip()
    return value in ("1", "true", "yes", "on")


def _require_csv_env(name: str) -> list[str]:
    """Read a comma-separated list from an env var.

    Parses the value, strips whitespace from each token, and drops empty
    strings. Returns ``[]`` when the variable is set but empty or blank
    (operator chose to disable). This is the only _require_env variant
    that allows an empty result -- callers must explicitly check for
    empty lists if a non-empty result is required.

    Args:
        name: environment variable name (e.g. ``"FARMSURV_NIGHT_SUPPRESS_CAMERAS"``).

    Returns:
        List of non-empty, stripped tokens. ``[]`` if the var is empty.

    Raises:
        RuntimeError: if the variable is unset.
    """
    raw = _require_env(name)
    if not raw.strip():
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]
