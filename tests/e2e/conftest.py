"""E2E conftest — fixture overrides for tests needing real HTTP.

The root conftest.py has an autouse fixture ``mock_llama_server`` that
patches ``httpx.Client`` with a MagicMock.  This conftest overrides it:
for test_api_server_e2e and test_mcp_server_e2e we restore the real
httpx.Client so real HTTP calls reach spawned servers.  For other tests
in this directory (e.g. test_telegram_html_rendering.py) the root fixture
remains active because we only override for those two module names.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def mock_llama_server(monkeypatch, request):
    """Override the root conftest autouse fixture for server E2E tests."""
    if request.module.__name__ in (
        "tests.e2e.test_api_server_e2e",
        "tests.e2e.test_mcp_server_e2e",
    ):
        # Restore the real httpx.Client — the root conftest already
        # patched it with a MagicMock before this fixture ran.
        import httpx as _original_httpx

        monkeypatch.setattr("httpx.Client", _original_httpx.Client)
        yield None
    else:
        # For other tests, yield nothing — the root conftest's patch
        # remains in place.
        yield None
