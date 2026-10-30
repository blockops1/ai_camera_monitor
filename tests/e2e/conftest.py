"""E2E conftest — fixture overrides for tests needing real HTTP.

The root conftest.py has an autouse fixture ``mock_llama_server`` that
patches ``httpx.Client`` with a MagicMock.  This conftest overrides it:
for test_api_server_e2e and test_mcp_server_e2e we restore the real
httpx.Client so real HTTP calls reach spawned servers.  For other tests
in this directory (e.g. test_telegram_html_rendering.py) the root fixture
remains active because we only override for those two module names.
"""

from __future__ import annotations

import httpx as _httpx
from unittest.mock import MagicMock, patch

import pytest

# Capture the REAL httpx.Client at conftest import time — before any
# fixture (root mock_llama_server, per-test monkeypatch) can patch it.
# Do NOT re-read httpx.Client inside the fixture: sibling tests
# (test_heartbeat_tg_on_fail) stack monkeypatch on top of the autouse
# mock, and monkeypatch.undo() then leaks a stale MagicMock back onto
# httpx.Client after the patch context has restored the real one.
_TRUE_HTTPX_CLIENT = _httpx.Client


@pytest.fixture(autouse=True)
def mock_llama_server(monkeypatch, request):
    """Override the root conftest autouse fixture for server E2E tests.

    For test_api_server_e2e / test_mcp_server_e2e: restore real httpx.Client.
    For all other E2E tests: install the same httpx.Client mock from the
    root conftest so tests receive a proper MagicMock instead of None.
    """
    module_file = request.path.name
    if module_file in (
        "test_api_server_e2e.py",
        "test_mcp_server_e2e.py",
    ):
        # Restore the real httpx.Client.  Use the import-time captured
        # reference, NOT a look-up of httpx.Client here: at this point a
        # stale MagicMock may be sitting on the attribute (leaked by a
        # sibling test's monkeypatch teardown ordering — see above).
        monkeypatch.setattr("httpx.Client", _TRUE_HTTPX_CLIENT)
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
