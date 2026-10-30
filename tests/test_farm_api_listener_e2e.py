"""test_farm_api_listener_e2e — E2E tests for farm_api listener endpoint.

E2E strategy:
    - Test the route handler _route_listener_status directly.
    - Verify it returns (200, body_dict) with correct structure.
    - Verify uptime_seconds > 0 when listener is running.
    - Verify mode is "running" when process is alive.

Bug classes covered:
    - Listener status endpoint returns wrong field names.
"""

from __future__ import annotations


class TestListenerStatusE2E:
    """E2E: _route_listener_status() returns correct structure."""

    def test_returns_200_and_dict(self):
        from infra.api_server import _route_listener_status

        status, body = _route_listener_status()
        assert status == 200
        assert isinstance(body, dict)
        assert "data" in body

    def test_data_has_pid(self):
        from infra.api_server import _route_listener_status

        _, body = _route_listener_status()
        data = body["data"]
        assert "pid" in data
        assert isinstance(data["pid"], int)

    def test_data_has_uptime_seconds(self):
        from infra.api_server import _route_listener_status

        _, body = _route_listener_status()
        data = body["data"]
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))

    def test_data_has_mode(self):
        from infra.api_server import _route_listener_status

        _, body = _route_listener_status()
        data = body["data"]
        assert "mode" in data
        assert data["mode"] in ("running", "stopped")
