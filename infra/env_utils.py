"""
env_utils.py -- shared env-var parsing helpers for farm-surveillance-v2.

STATUS: stable (US-069a)
THREAD SAFETY: single-threaded, no shared mutable state
IMPORT ORDER: stdlib -> third-party -> local (infra.)

INPUTS:
    - os.environ entries (comma-separated strings)

OUTPUTS:
    - parsed Python lists/frozensets

DOES NOT DO:
    - IO, network, file reads beyond os.environ
    - Hardcoded values (all defaults from os.environ.get)

RULE (US-069-fail-loud-env):
    Operator-significant env vars (gates, suppress, auth tokens) MUST use
    _require_env / _require_env_lower_bool / _require_csv_env from
    infra.env_required — these raise RuntimeError when the var is unset,
    forcing the operator to configure them explicitly. Only connection
    knobs (LISTEN_HOST, LISTEN_PORT) and tuning thresholds
    (NIGHT_CONF_FLOOR, NIGHT_BRIGHTNESS_RATIO) use os.environ.get with
    defaults.

CALLED BY:
    - infra.quick_classifier (_parse_csv_env for night suppress lists)
    - infra.quiet_hours (_parse_cameras, similar pattern)
"""

from __future__ import annotations

import os


def parse_csv_env(name: str) -> list[str]:
    """Read an env var as a comma-separated list.

    Reads ``os.environ.get(name, '')``, strips whitespace from each token,
    splits on comma, and drops empty strings.  Returns ``[]`` when the
    variable is unset or contains only whitespace.

    Args:
        name: environment variable name (e.g. ``"FARMSURV_NIGHT_SUPPRESS_CAMERAS"``).

    Returns:
        List of non-empty, stripped tokens.  ``[]`` if the var is unset or blank.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]
