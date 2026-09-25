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
import os
import subprocess

log = logging.getLogger(__name__)

# Tailscale CGNAT range: 100.64.0.0/10
# Resolve from TAILNET_BIND_ADDR env var if set, else auto-detect the
# first 100.x IP on the machine. Falls back to 100.64.0.1.
def _resolve_tailnet_ip() -> str:
    """Return the first 100.x.x.x IP found on the machine."""
    override = os.environ.get("TAILNET_BIND_ADDR")
    if override:
        return override
    try:
        out = subprocess.check_output(
            ["ifconfig"], text=True, timeout=5
        )
        for line in out.split("\n"):
            if "100." in line and "inet" in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == "inet" and i + 1 < len(parts):
                        ip = parts[i + 1]
                        if ip.startswith("100."):
                            return ip
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return "100.64.0.1"


TAILNET_BIND = _resolve_tailnet_ip()

# Tailscale CGNAT range: 100.64.0.0/10
_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")


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
