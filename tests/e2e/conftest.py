"""E2E conftest — fixture overrides for tests needing real HTTP.

The root conftest.py has an autouse fixture ``mock_llama_server`` that
patches ``httpx.Client`` with a MagicMock.  This conftest overrides it:
for test_api_server_e2e and test_mcp_server_e2e we restore the real
httpx.Client so real HTTP calls reach spawned servers.  For other tests
in this directory (e.g. test_telegram_html_rendering.py) the root fixture
remains active because we only override for those two module names.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def mock_llama_server(monkeypatch, request):
    """Override the root conftest autouse fixture for server E2E tests.

    For test_api_server_e2e / test_mcp_server_e2e: restore real httpx.Client.
    For all other E2E tests: install the same httpx.Client mock from the
    root conftest so tests receive a proper MagicMock instead of None.
    """
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
        # Build the mock response that the root conftest creates.
        _mock_resp = MagicMock()
        _mock_resp.status_code = 200  # real int, not MagicMock
        _mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"class": "vehicle", "subclass": "sedan", "confidence": "likely", "count": 1, "face_visible": false, "notes": null}'
                }
            }]
        }

        # Build the mock client — httpx.Client(timeout=...) returns this.
        _mock_client = MagicMock()
        _mock_client.post.return_value = _mock_resp
        _mock_client.__enter__ = MagicMock(return_value=_mock_client)
        _mock_client.__exit__ = MagicMock(return_value=False)

        # Patch httpx.Client to return _mock_client (not to BE _mock_client).
        # Use patch.context so it auto-undoes on fixture teardown.
        with patch("httpx.Client", return_value=_mock_client) as _patch:
            yield _mock_client
