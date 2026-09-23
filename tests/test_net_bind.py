"""test_net_bind -- Tests for the network binding address resolver.

STATUS: new (US-072a)
RUNS: pytest tests/test_net_bind.py

COVERAGE:
    - test_tailnet_only_resolves_100: 'tailnet-only' -> '100.x'
    - test_all_resolves_0_0_0_0: 'all' -> '0.0.0.0'
    - test_custom_interface_resolves: custom interface name returned as-is
    - test_is_tailnet_ip_recognizes_cgnat: 100.64.0.0/10 detection
"""

from __future__ import annotations

from infra.net_bind import is_tailnet_ip, resolve_bind_address

# ---------------------------------------------------------------------------
# resolve_bind_address tests
# ---------------------------------------------------------------------------


def test_tailnet_only_resolves_100():
    """Passes 'tailnet-only', expects a string starting with '100.'"""
    result = resolve_bind_address("tailnet-only")
    assert result.startswith("100.")


def test_all_resolves_0_0_0_0():
    """Passes 'all', expects '0.0.0.0'"""
    result = resolve_bind_address("all")
    assert result == "0.0.0.0"


def test_custom_interface_resolves():
    """Passes a literal interface name, returns it unchanged"""
    result = resolve_bind_address("lo0")
    assert result == "lo0"


def test_custom_ip_resolves():
    """Passes a literal IP, returns it unchanged"""
    result = resolve_bind_address("10.0.0.1")
    assert result == "10.0.0.1"


# ---------------------------------------------------------------------------
# is_tailnet_ip tests
# ---------------------------------------------------------------------------


def test_is_tailnet_ip_recognizes_cgnat():
    """'100.64.0.5' True; '10.0.0.1' False; '127.0.0.1' False"""
    assert is_tailnet_ip("100.64.0.5") is True
    assert is_tailnet_ip("100.127.255.255") is True
    assert is_tailnet_ip("10.0.0.1") is False
    assert is_tailnet_ip("127.0.0.1") is False
    assert is_tailnet_ip("192.168.1.1") is False
    assert is_tailnet_ip("99.255.255.255") is False  # just below the range
    assert is_tailnet_ip("101.0.0.0") is False  # just above the range
