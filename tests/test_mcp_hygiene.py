"""test_mcp_hygiene — E2E tests for MCP server security / hygiene headers.

E2E strategy:
    - Start the real infra.mcp_server on a random port in a background thread.
    - Hit it with urllib.request (real HTTP, no httpx — avoids conftest
      autouse mock of httpx.Client).
    - Each test logs the JSON-RPC response body and status code.

Dependencies:
    - US-101y.mcp merged to main (case-insensitive Bearer, suppressed Server
      header, X-Content-Type-Options: nosniff, do_PUT/do_DELETE/do_OPTIONS
      returning JSON 405, per-request request_id injected into responses).

Acceptance criteria:
  AC1: .venv/bin/python -m pytest tests/test_mcp_hygiene.py -v passes
  AC2: ruff check tests/test_mcp_hygiene.py = 0 errors
  AC3: No skips, no warnings.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return an OS-assigned free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_req(base_url, method="/mcp", path="/mcp", headers=None, data=None):
    """Send a raw HTTP request and return (status, hdr_dict, body_str)."""
    _hdrs = dict(headers or {})
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method=method, headers=_hdrs, data=data)
    try:
        resp = urllib.request.urlopen(req)
        body_bytes = resp.read()
        return resp.status, dict(resp.headers), body_bytes.decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        return exc.code, dict(exc.headers), body_bytes.decode("utf-8")


def _post(base_url, path="/mcp", headers=None, data=None):
    """Convenience: POST request."""
    return _http_req(base_url, "POST", path, headers, data)


def _put(base_url, path="/mcp", headers=None):
    """Convenience: PUT request."""
    return _http_req(base_url, "PUT", path, headers)


def _options(base_url, path="/mcp", headers=None):
    """Convenience: OPTIONS request."""
    return _http_req(base_url, "OPTIONS", path, headers)


def _json_body(text):
    """Parse JSON from a response body string."""
    return json.loads(text)


def _header_get(headers, key):
    """Case-insensitive header lookup.

    urllib returns headers as a case-sensitive dict (keys preserved from wire),
    so we normalise to lowercase for comparison.
    """
    for k, v in headers.items():
        if k.lower() == key.lower():
            return v
    return None


# ---------------------------------------------------------------------------
# Fixture: start the real MCP server
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mcp_server():
    """Start the real MCP server on a random port.

    Yields (base_url, token) where base_url is http:// host and token is a
    40-char bearer token.  The server runs in a background thread and is
    shut down when the module-scoped fixture tears down.
    """
    import logging

    port = _free_port()
    token = "y" * 40

    os.environ["FARMSURV_MCP_ENABLED"] = "1"
    os.environ["FARMSURV_MCP_TOKEN"] = token
    os.environ["FARMSURV_MCP_BIND"] = "all"
    os.environ["FARMSURV_MCP_PORT"] = str(port)
    os.environ["FARMSURV_BOX_ID"] = "test-box"
    # Infra-level vars that quick_classifier / quiet_hours require at import time
    os.environ["MOTION_GATE_NIGHT_SUPPRESS_ENABLED"] = "1"
    os.environ["FARMSURV_QUIET_HOURS_ENABLED"] = "0"
    os.environ["FARMSURV_BASELINE_OVERRIDE_ENABLED"] = "0"
    os.environ["FARMSURV_FARM_LAT"] = "34.5782"
    os.environ["FARMSURV_FARM_LON"] = "-84.9438"
    os.environ["FARMSURV_GLOBAL_CHANGE_MAX_PCT"] = "60"
    os.environ["FARMSURV_DISTANT_KEYWORDS"] = "***REMOVED***"
    os.environ["BBOX_INTERSECTION_DILATE_PCT"] = "0.01"

    # Clear cached modules so they re-read env vars.
    # Import the server classes AFTER clearing so they get the fresh
    # module with a fresh _GLOBAL_REGISTRY list (not a stale reference
    # to a list that we set on a different module instance).
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("infra.mcp_server", "infra.mcp_auth"):
            del sys.modules[mod_name]

    from infra.mcp_server import _MCPHandler, _ThreadingHTTPServer

    # Configure a file-based handler on the mcp_server logger so
    # log output lands in a known file we can read back.
    _mcp_logger = logging.getLogger("infra.mcp_server")
    _mcp_logger.setLevel(logging.INFO)
    _log_file = f"/tmp/mcp_hygiene_{port}.log"
    _handler = logging.FileHandler(_log_file)
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _mcp_logger.addHandler(_handler)
    _mcp_logger.propagate = False

    # Build the registry directly (mimics _start_server without blocking).
    # This is needed because _MCPHandler._handle_request checks
    # _GLOBAL_REGISTRY[0] before dispatching.
    from infra.mcp_server import _GLOBAL_REGISTRY, _build_registry

    _GLOBAL_REGISTRY[0] = _build_registry()

    server = _ThreadingHTTPServer(("0.0.0.0", port), _MCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Wait until the port is accepting connections
    for _ in range(20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", port))
                break
        except ConnectionRefusedError:
            time.sleep(0.05)
    else:
        server.shutdown()
        raise RuntimeError(f"MCP server did not start on port {port} within 1s")

    base_url = f"http://127.0.0.1:{port}"
    yield base_url, token, _log_file

    server.shutdown()
    _handler.close()
    try:
        os.remove(_log_file)
    except FileNotFoundError:
        pass

    # Reset the global registry to None so subsequent fixture
    # runs on different ports don't reuse stale state.
    _GLOBAL_REGISTRY[0] = None

    os.environ.pop("FARMSURV_MCP_ENABLED", None)
    os.environ.pop("FARMSURV_MCP_TOKEN", None)
    os.environ.pop("FARMSURV_MCP_BIND", None)
    os.environ.pop("FARMSURV_MCP_PORT", None)
    os.environ.pop("FARMSURV_BOX_ID", None)
    for mod_name in list(sys.modules.keys()):
        if mod_name in ("infra.mcp_server", "infra.mcp_auth"):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# Helper fixture: (base_url, token)
# ---------------------------------------------------------------------------


@pytest.fixture()
def server(mcp_server):
    """Alias fixture returning (base_url, token) tuple."""
    return mcp_server[:2]


@pytest.fixture()
def server_log(mcp_server):
    """Returns (base_url, token, log_file_path) for tests needing log access."""
    return mcp_server


def _mcp_json(method, params=None, id_val=1):
    """Build a JSON-RPC 2.0 POST body for /mcp."""
    payload = {"jsonrpc": "2.0", "method": method, "id": id_val}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# AC1: tools/list returns a request_id field echoing the request
# ---------------------------------------------------------------------------


class TestRequestId:
    """AC1: tools/list response includes a request_id field."""

    def test_tools_list_has_request_id(self, server):
        base_url, token = server
        body_bytes = _mcp_json("tools/list", id_val=42)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 200
        data = _json_body(body)
        assert "request_id" in data
        assert data["request_id"] != ""
        # request_id is a UUID (36 chars)
        assert len(data["request_id"]) == 36


# ---------------------------------------------------------------------------
# AC2: PUT and OPTIONS return 405 with JSON body, not HTML
# ---------------------------------------------------------------------------


class TestMethodNotAllowed:
    """AC2: PUT and OPTIONS to /mcp return 405 with JSON body."""

    def test_put_returns_json_405(self, server):
        base_url, token = server
        status, _hdrs, body = _put(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        print(f"RESP: status={status} content-type={_header_get(_hdrs, 'content-type')}")
        print(f"RESP body: {body}")
        assert status == 405
        assert _header_get(_hdrs, "content-type") == "application/json"
        data = _json_body(body)
        assert "error" in data
        assert "code" in data["error"]

    def test_options_returns_json_405(self, server):
        base_url, token = server
        status, _hdrs, body = _options(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        print(f"RESP: status={status} content-type={_header_get(_hdrs, 'content-type')}")
        print(f"RESP body: {body}")
        assert status == 405
        assert _header_get(_hdrs, "content-type") == "application/json"
        data = _json_body(body)
        assert "error" in data
        assert "code" in data["error"]


# ---------------------------------------------------------------------------
# AC3: Response envelope — list-style tools/results wrapped in data field
# ---------------------------------------------------------------------------


class TestResponseEnvelope:
    """AC3: List-style MCP results wrapped in a data field."""

    def test_tools_list_wrapped_in_result(self, server):
        base_url, token = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 200
        data = _json_body(body)
        # JSON-RPC 2.0 spec: success has {"result": ...}
        assert "result" in data
        # tools/list result has {"tools": [...]}
        assert "tools" in data["result"]
        assert isinstance(data["result"]["tools"], list)
        assert len(data["result"]["tools"]) > 0


# ---------------------------------------------------------------------------
# AC4: X-Content-Type-Options: nosniff on ALL responses
# ---------------------------------------------------------------------------


class TestNosniff:
    """AC4: All responses carry X-Content-Type-Options: nosniff."""

    def test_nosniff_on_200(self, server):
        base_url, token = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        print(
            f"RESP: status={status} nosniff={_header_get(_hdrs, 'x-content-type-options')}"
        )
        print(f"RESP body: {body}")
        assert _header_get(_hdrs, "x-content-type-options") == "nosniff"

    def test_nosniff_on_405(self, server):
        base_url, token = server
        status, _hdrs, body = _put(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        print(
            f"RESP: status={status} nosniff={_header_get(_hdrs, 'x-content-type-options')}"
        )
        print(f"RESP body: {body}")
        assert _header_get(_hdrs, "x-content-type-options") == "nosniff"


# ---------------------------------------------------------------------------
# AC5: Server header suppressed / generic
# ---------------------------------------------------------------------------


class TestServerHeader:
    """AC5: Server header is suppressed (generic, no Python leak)."""

    def test_server_header_generic(self, server):
        base_url, token = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        server_hdr = _header_get(_hdrs, "server") or ""
        print(f"RESP: status={status} server={server_hdr}")
        print(f"RESP body: {body}")
        assert "Python" not in server_hdr, (
            f"Server header leaks Python version: '{server_hdr}'"
        )
        assert server_hdr.strip() == "farm-mcp/0.1", (
            f"Unexpected server header: '{server_hdr}'"
        )


# ---------------------------------------------------------------------------
# AC6: Case-insensitive Bearer auth
# ---------------------------------------------------------------------------


class TestBearerCaseInsensitive:
    """AC6: Bearer scheme accepted case-insensitively (per RFC 6750)."""

    def test_lowercase_bearer(self, server):
        base_url, token = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 200
        data = _json_body(body)
        assert "result" in data

    def test_titlecase_bearer(self, server):
        base_url, token = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 200
        data = _json_body(body)
        assert "result" in data

    def test_mixed_case_bearer(self, server):
        base_url, token = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": f"beARer {token}",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 200
        data = _json_body(body)
        assert "result" in data


# ---------------------------------------------------------------------------
# AC7: request_id echoed in error responses
# ---------------------------------------------------------------------------


class TestRequestIdErrorResponses:
    """AC7: request_id present in error responses and 401 bodies."""

    def test_401_body_has_request_id(self, server):
        """No auth header -> 401, body includes request_id."""
        base_url, _ = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={"Content-Type": "application/json"},
            data=body_bytes,
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 401
        data = _json_body(body)
        assert "request_id" in data
        assert data["request_id"] != ""
        assert len(data["request_id"]) == 36

    def test_unknown_method_has_request_id(self, server):
        """Unknown JSON-RPC method -> 400, body includes request_id."""
        base_url, token = server
        body_bytes = _mcp_json("nonexistent_method", id_val=99)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 400
        data = _json_body(body)
        assert "request_id" in data
        assert data["request_id"] != ""


# ---------------------------------------------------------------------------
# AC8: WWW-Authenticate header on 401 responses
# ---------------------------------------------------------------------------


class TestWWWAuthenticate:
    """AC8: 401 responses include WWW-Authenticate header."""

    def test_401_has_www_authenticate(self, server):
        base_url, _ = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={"Content-Type": "application/json"},
            data=body_bytes,
        )
        print(f"RESP: status={status} www-auth={_header_get(_hdrs, 'www-authenticate')}")
        print(f"RESP body: {body}")
        assert status == 401
        www_auth = _header_get(_hdrs, "www-authenticate")
        assert www_auth is not None
        assert "Bearer" in www_auth

    def test_401_wrong_token_has_www_authenticate(self, server):
        base_url, _ = server
        body_bytes = _mcp_json("tools/list", id_val=1)
        status, _hdrs, body = _post(
            base_url,
            headers={
                "Authorization": "Bearer wrongtoken",
                "Content-Type": "application/json",
            },
            data=body_bytes,
        )
        print(f"RESP: status={status} www-auth={_header_get(_hdrs, 'www-authenticate')}")
        print(f"RESP body: {body}")
        assert status == 401
        assert _header_get(_hdrs, "www-authenticate") is not None
