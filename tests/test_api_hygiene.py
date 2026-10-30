"""test_api_hygiene — E2E tests for API security / hygiene headers.

E2E strategy:
    - Start the real infra.api_server on a random port in a background thread.
    - Hit it with urllib.request (real HTTP, no httpx — avoids conftest
      autouse mock of httpx.Client).
    - Each test logs the response body and status code.

Dependencies:
    - US-101y.api merged to main (5a9a5888).
    - US-101y.openapi merged to main (72f0206e) — openapi.json route.
"""

from __future__ import annotations

import json
import logging
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


def _http_req(base_url, method, path="/healthz", headers=None, data=None):
    """Send a raw HTTP request and return (status, hdr_dict, body_str)."""
    hdrs = dict(headers or {})
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method=method, headers=hdrs,
                                 data=data)
    try:
        resp = urllib.request.urlopen(req)
        body_bytes = resp.read()
        return resp.status, dict(resp.headers), body_bytes.decode("utf-8")
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read()
        return exc.code, dict(exc.headers), body_bytes.decode("utf-8")


def _get(base_url, path="/healthz", headers=None):
    """Convenience: GET request."""
    return _http_req(base_url, "GET", path, headers)


def _post(base_url, path="/api/v1/cameras", headers=None):
    """Convenience: POST request."""
    return _http_req(base_url, "POST", path, headers)


def _put(base_url, path="/api/v1/cameras", headers=None):
    """Convenience: PUT request."""
    return _http_req(base_url, "PUT", path, headers)


def _delete(base_url, path="/api/v1/cameras", headers=None):
    """Convenience: DELETE request."""
    return _http_req(base_url, "DELETE", path, headers)


def _options(base_url, path="/api/v1/cameras", headers=None):
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
# Fixture: start the real API server
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def api_server():
    """Start the real API server on a random port.

    Yields (base_url, token) where base_url is http:// host and token is a
    40-char bearer token.  The server runs in a background thread and is
    shut down when the module-scoped fixture tears down.
    """
    import logging

    from infra.api_server import _APIHandler, _ThreadingHTTPServer

    port = _free_port()
    token = "a" * 40

    os.environ["FARMSURV_API_ENABLED"] = "1"
    os.environ["FARMSURV_API_TOKEN"] = token
    os.environ["FARMSURV_API_BIND"] = "all"
    os.environ["FARMSURV_API_PORT"] = str(port)
    os.environ["FARMSURV_BOX_ID"] = "test-box"

    # Clear cached modules so they re-read env vars
    for mod_name in list(sys.modules.keys()):
        if mod_name == "infra.api_server":
            del sys.modules[mod_name]

    # Configure a file-based handler on the api_server logger so
    # _log_access output lands in a known file we can read back.
    # We use a file instead of stderr because pytest's stderr capture
    # does not propagate daemon-thread writes into capsys.readouterr().
    _api_logger = logging.getLogger("infra.api_server")
    _api_logger.setLevel(logging.INFO)
    _log_file = f"/tmp/api_hygiene_{port}.log"
    _handler = logging.FileHandler(_log_file)
    _handler.setLevel(logging.INFO)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _api_logger.addHandler(_handler)
    _api_logger.propagate = False

    server = _ThreadingHTTPServer(("0.0.0.0", port), _APIHandler)
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
        raise RuntimeError(f"API server did not start on port {port} within 1s")

    base_url = f"http://127.0.0.1:{port}"
    yield base_url, token, _log_file

    server.shutdown()
    _handler.close()
    try:
        os.remove(_log_file)
    except FileNotFoundError:
        pass

    os.environ.pop("FARMSURV_API_ENABLED", None)
    os.environ.pop("FARMSURV_API_TOKEN", None)
    os.environ.pop("FARMSURV_API_BIND", None)
    os.environ.pop("FARMSURV_API_PORT", None)
    os.environ.pop("FARMSURV_BOX_ID", None)
    for mod_name in list(sys.modules.keys()):
        if mod_name == "infra.api_server":
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# Helper fixture: (base_url, token)
# ---------------------------------------------------------------------------

@pytest.fixture()
def server(api_server):
    """Alias fixture returning (base_url, token) tuple."""
    return api_server[:2]


@pytest.fixture()
def server_log(api_server):
    """Returns (base_url, token, log_file_path) for tests needing log access."""
    return api_server


# ---------------------------------------------------------------------------
# AC1: 401 responses include WWW-Authenticate
# ---------------------------------------------------------------------------

class Test401WWWAuthenticate:
    """AC1: 401 responses include a WWW-Authenticate header."""

    def test_401_without_auth_header(self, server):
        base_url, _ = server
        status, hdrs, body = _get(base_url, "/api/v1/cameras")
        print(f"RESP: status={status} headers={hdrs}")
        print(f"RESP body: {body}")
        assert status == 401
        www_auth = _header_get(hdrs, "www-authenticate")
        assert www_auth is not None
        assert "Bearer" in www_auth

    def test_401_with_bad_token(self, server):
        base_url, _ = server
        status, hdrs, body = _get(
            base_url, "/api/v1/cameras",
            headers={"Authorization": "Bearer wrongtoken"},
        )
        print(f"RESP: status={status} headers={hdrs}")
        print(f"RESP body: {body}")
        assert status == 401
        assert _header_get(hdrs, "www-authenticate") is not None


# ---------------------------------------------------------------------------
# AC2: Cache-Control on stable vs dynamic endpoints
# ---------------------------------------------------------------------------

class TestCacheControl:
    """AC2: Cache-Control header present and correct per endpoint type."""

    def test_list_endpoint_has_max_age(self, server):
        base_url, token = server
        status, hdrs, body = _get(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status} Cache-Control={_header_get(hdrs, 'cache-control')}")
        print(f"RESP body: {body}")
        assert status == 200
        assert _header_get(hdrs, "cache-control") == "max-age=5, private"

    def test_single_event_no_cache(self, server):
        base_url, token = server
        status, hdrs, body = _get(
            base_url, "/api/v1/events/nonexistent-id-here",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status} Cache-Control={_header_get(hdrs, 'cache-control')}")
        print(f"RESP body: {body}")
        assert status == 404
        assert _header_get(hdrs, "cache-control") == "no-store"

    def test_healthz_no_store(self, server):
        base_url, _ = server
        status, hdrs, body = _get(base_url, "/healthz")
        print(f"RESP: status={status} Cache-Control={_header_get(hdrs, 'cache-control')}")
        print(f"RESP body: {body}")
        assert status == 200
        assert _header_get(hdrs, "cache-control") == "no-store"


# ---------------------------------------------------------------------------
# AC3: POST/PUT/DELETE/OPTIONS return JSON 405
# ---------------------------------------------------------------------------

class TestMethodNotAllowed:
    """AC3: POST/PUT/DELETE/OPTIONS return 405 with JSON body."""

    def test_post_returns_json_405(self, server):
        base_url, token = server
        status, hdrs, body = _post(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status} content-type={_header_get(hdrs, 'content-type')}")
        print(f"RESP body: {body}")
        assert status == 405
        assert _header_get(hdrs, "content-type") == "application/json"
        data = _json_body(body)
        assert data["code"] == "method_not_allowed"
        assert data["status"] == 405

    def test_put_returns_json_405(self, server):
        base_url, token = server
        status, hdrs, body = _put(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status} content-type={_header_get(hdrs, 'content-type')}")
        print(f"RESP body: {body}")
        assert status == 405
        assert _header_get(hdrs, "content-type") == "application/json"
        data = _json_body(body)
        assert data["code"] == "method_not_allowed"

    def test_delete_returns_json_405(self, server):
        base_url, token = server
        status, hdrs, body = _delete(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status} content-type={_header_get(hdrs, 'content-type')}")
        print(f"RESP body: {body}")
        assert status == 405
        assert _header_get(hdrs, "content-type") == "application/json"
        data = _json_body(body)
        assert data["code"] == "method_not_allowed"

    def test_options_returns_json_405(self, server):
        base_url, token = server
        status, hdrs, body = _options(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status} content-type={_header_get(hdrs, 'content-type')}")
        print(f"RESP body: {body}")
        assert status == 405
        assert _header_get(hdrs, "content-type") == "application/json"
        data = _json_body(body)
        assert data["code"] == "method_not_allowed"


# ---------------------------------------------------------------------------
# AC4: X-Content-Type-Options: nosniff on ALL responses
# ---------------------------------------------------------------------------

class TestNosniff:
    """AC4: All responses carry X-Content-Type-Options: nosniff."""

    def test_nosniff_on_200(self, server):
        base_url, _ = server
        status, hdrs, body = _get(base_url, "/healthz")
        print(f"RESP: status={status} nosniff={_header_get(hdrs, 'x-content-type-options')}")
        print(f"RESP body: {body}")
        assert _header_get(hdrs, "x-content-type-options") == "nosniff"

    def test_nosniff_on_401(self, server):
        base_url, _ = server
        status, hdrs, body = _get(base_url, "/api/v1/cameras")
        print(f"RESP: status={status} nosniff={_header_get(hdrs, 'x-content-type-options')}")
        print(f"RESP body: {body}")
        assert _header_get(hdrs, "x-content-type-options") == "nosniff"

    def test_nosniff_on_405(self, server):
        base_url, token = server
        status, hdrs, body = _post(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status} nosniff={_header_get(hdrs, 'x-content-type-options')}")
        print(f"RESP body: {body}")
        assert _header_get(hdrs, "x-content-type-options") == "nosniff"

    def test_nosniff_on_404(self, server):
        base_url, token = server
        status, hdrs, body = _get(
            base_url, "/nonexistent",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status} nosniff={_header_get(hdrs, 'x-content-type-options')}")
        print(f"RESP body: {body}")
        assert _header_get(hdrs, "x-content-type-options") == "nosniff"


# ---------------------------------------------------------------------------
# AC5: Server header suppressed / generic
# ---------------------------------------------------------------------------

class TestServerHeader:
    """AC5: Server header is suppressed (or generic) on responses."""

    def test_server_header_generic(self, server):
        base_url, _ = server
        status, hdrs, body = _get(base_url, "/healthz")
        server_hdr = _header_get(hdrs, "server") or ""
        print(f"RESP: status={status} server={server_hdr}")
        print(f"RESP body: {body}")
        assert "Python" not in server_hdr, (
            f"Server header leaks Python version: '{server_hdr}'"
        )
        assert server_hdr.strip() == "farm-api/0.1", (
            f"Unexpected server header: '{server_hdr}'"
        )


# ---------------------------------------------------------------------------
# AC6: Bearer scheme case-insensitive
# ---------------------------------------------------------------------------

class TestBearerCaseInsensitive:
    """AC6: Bearer scheme accepted case-insensitively."""

    def test_lowercase_bearer(self, server):
        base_url, token = server
        status, _hdrs, body = _get(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"bearer {token}"},
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 200

    def test_titlecase_bearer(self, server):
        base_url, token = server
        status, _hdrs, body = _get(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 200

    def test_mixed_case_bearer(self, server):
        base_url, token = server
        status, _hdrs, body = _get(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"beARer {token}"},
        )
        print(f"RESP: status={status}")
        print(f"RESP body: {body}")
        assert status == 200


# ---------------------------------------------------------------------------
# AC7: request_id in error bodies and access log
# ---------------------------------------------------------------------------

class TestRequestId:
    """AC7: request_id present in error response bodies and in access log."""

    def test_401_body_has_request_id(self, server):
        base_url, _ = server
        _status, _hdrs, body = _get(base_url, "/api/v1/cameras")
        data = _json_body(body)
        print(f"RESP: status={_status} request_id={data.get('request_id')}")
        print(f"RESP body: {body}")
        assert "request_id" in data
        assert data["request_id"] != ""
        assert len(data["request_id"]) == 36  # UUID format

    def test_404_body_has_request_id(self, server):
        base_url, token = server
        _status, _hdrs, body = _get(
            base_url, "/api/v1/events/nonexistent",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = _json_body(body)
        print(f"RESP: status={_status} request_id={data.get('request_id')}")
        print(f"RESP body: {body}")
        assert "request_id" in data
        assert data["request_id"] != ""

    def test_405_body_has_request_id(self, server):
        base_url, token = server
        _status, _hdrs, body = _post(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = _json_body(body)
        print(f"RESP: status={_status} request_id={data.get('request_id')}")
        print(f"RESP body: {body}")
        assert "request_id" in data
        assert data["request_id"] != ""

    def test_success_body_has_request_id(self, server):
        base_url, _ = server
        _status, _hdrs, body = _get(base_url, "/healthz")
        data = _json_body(body)
        print(f"RESP: status={_status} request_id={data.get('request_id')}")
        print(f"RESP body: {body}")
        assert "request_id" in data
        assert data["request_id"] != ""

    def test_request_id_in_access_log(self, server_log):
        """request_id appears in the access log line for the request.

        The fixture adds a file-based handler to the api_server logger.
        After firing a request, we read the log file and verify the
        request_id from the response body is present.
        """
        base_url, token, log_file = server_log

        api_logger = logging.getLogger("infra.api_server")
        api_logger.setLevel(logging.INFO)

        _status, _hdrs, body = _get(
            base_url, "/api/v1/cameras",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = _json_body(body)
        ok_id = data["request_id"]

        # Small delay to ensure the file is flushed
        time.sleep(0.1)

        with open(log_file) as f:
            log_text = f.read()
        print(f"REQUEST_ID={ok_id}")
        print(f"log_text={log_text}")
        assert ok_id in log_text, (
            f"request_id '{ok_id}' not found in access log. "
            f"Log contents: {log_text}"
        )
