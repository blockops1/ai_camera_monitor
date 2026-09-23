"""net_bind -- network binding address resolver for MCP and API servers.

STATUS: new (US-072a)
THREAD SAFETY: single-threaded, no shared mutable state
IMPORT ORDER: stdlib -> third-party -> local (infra.)

CONTRACT:
    resolve_bind_address(spec) accepts three canonical values:
      - 'tailnet-only'   -> resolves to a Tailscale CGNAT 100.x IP
      - 'all'            -> resolves to '0.0.0.0' (all interfaces)
      - <interface name> -> resolves to that interface's IP address
    Raises ValueError on any other spec.

    is_tailnet_ip(addr) returns True when addr falls within
    100.64.0.0/10 (Tailscale CGNAT range).

INPUTS:
    - spec: str ('tailnet-only', 'all', or interface name)
    - addr: str (IPv4 address to classify)

OUTPUTS:
    - resolve_bind_address: str (bind address)
    - is_tailnet_ip: bool

DOES NOT DO:
    - Network I/O beyond socket.getaddrinfo (no DNS, no API calls)
    - Environment variable reading (caller passes spec explicitly)
    - Warning logs (caller handles logging; this module is pure)

CALLERS:
    - infra.mcp_server (MCP server startup)
    - infra.api_server (API server startup, future)
"""

from __future__ import annotations

import ipaddress
import logging

log = logging.getLogger(__name__)

# Tailscale CGNAT range: 100.64.0.0/10
_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")
# Default Tailscale bind address when spec is 'tailnet-only'
TAILNET_BIND = "100.64.0.1"


def is_tailnet_ip(addr: str) -> bool:
    """Check if an IP address falls within the Tailscale CGNAT range.

    Args:
        addr: IPv4 address string (e.g. '100.64.0.5').

    Returns:
        True if the address is in 100.64.0.0/10, False otherwise.
    """
    try:
        return ipaddress.ip_address(addr) in _TAILNET_NET
    except ValueError:
        return False


def resolve_bind_address(spec: str) -> str:
    """Resolve a bind specification to a concrete IP address string.

    Accepts three canonical values:
      - 'tailnet-only'   -> TAILNET_BIND (a 100.x Tailscale IP)
      - 'all'            -> '0.0.0.0' (all interfaces)
      - <interface name> -> that interface's first resolved IP

    Args:
        spec: Bind mode specification. One of 'tailnet-only', 'all',
              or a literal interface name/IP.

    Returns:
        The IP address string to bind to.

    Raises:
        ValueError: If spec is unknown. The message lists all valid values.
    """
    if spec == "all":
        log.warning(
            "Binding to 0.0.0.0 (all interfaces). "
            "Ensure the service token is set to a strong value."
        )
        return "0.0.0.0"

    if spec == "tailnet-only":
        return TAILNET_BIND

    # Treat as a specific interface name or literal IP
    log.warning(
        "Binding to custom address %s. Verify this interface exists.",
        spec,
    )
    return spec
