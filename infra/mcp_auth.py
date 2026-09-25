"""mcp_auth -- Bearer-token authentication for the MCP server.

STATUS: alpha -- V2-068c
THREAD SAFETY: thread-safe (stateless token comparison)

INPUTS:
    - FARMSURV_MCP_TOKEN env var (required, 32+ chars)

OUTPUTS:
    - bearer_token_required() -> True
    - authenticate(auth_header) -> bool
    - validate_token(env_value) -> (True, token_str) | (False, error_msg)

DOES NOT DO:
    - Read credentials from files (token from env var only)
    - Implement session management (stateless token check)
    - Handle OAuth, JWT, or multi-token rotation

USAGE:
    from infra.mcp_auth import authenticate, validate_token
"""

from __future__ import annotations

import logging
from functools import lru_cache

from infra.env_required import _require_env

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token validation at startup
# ---------------------------------------------------------------------------


def validate_token() -> tuple[bool, str]:
    """Validate the FARMSURV_MCP_TOKEN env var at server startup.

    Uses _require_env for fail-loud behavior: if the token is missing,
    raises RuntimeError immediately rather than falling back to empty.

    Returns:
        (True, token) if the token is present and >= 32 chars.
        (False, error_msg) if too short.
    """
    token = _require_env("FARMSURV_MCP_TOKEN")
    if len(token) < 32:
        return False, (
            f"FARMSURV_MCP_TOKEN is too short ({len(token)} chars, "
            "need 32+). Generate a new one with: python -c "
            "'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return True, token


# ---------------------------------------------------------------------------
# Per-request authentication
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _cached_token() -> str:
    """Return the configured token, cached after first read via _require_env."""
    return _require_env("FARMSURV_MCP_TOKEN")


def authenticate(auth_header: str | None) -> bool:
    """Check the Authorization header against the configured token.

    Args:
        auth_header: The raw Authorization header value, or None.

    Returns:
        True if the header is present and matches, False otherwise.
    """
    if auth_header is None:
        return False

    # Expect "Bearer <token>"
    if not auth_header.startswith("Bearer "):
        return False

    provided_token = auth_header[7:]  # Strip "Bearer " prefix
    expected_token = _cached_token()

    # Constant-time comparison to prevent timing attacks
    return _constant_time_compare(provided_token, expected_token)


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks.

    Args:
        a: First string.
        b: Second string.

    Returns:
        True if the strings are equal, False otherwise.
    """
    if len(a) != len(b):
        return False
    result = 0
    for ca, cb in zip(a, b):
        result |= ord(ca) ^ ord(cb)
    return result == 0


def bearer_token_required() -> bool:
    """Return True if bearer token auth is enabled (token is set).

    This is always True when the MCP server is running -- the server
    aborts at startup if no valid token exists.
    """
    return True
