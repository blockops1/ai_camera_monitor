"""api_server -- Read-only HTTP API server for farm-surveillance.

STATUS: alpha -- V2-072b
THREAD SAFETY: not thread-safe (http.server is single-threaded by default)
               Use ThreadingHTTPServer for concurrent requests.

INPUTS:
    - FARMSURV_API_ENABLED (1=ON, 0=OFF, default 0)
    - FARMSURV_API_BIND (tailnet-only | all | <interface>, default tailnet-only)
    - FARMSURV_API_TOKEN (required when enabled, 32+ chars)
    - FARMSURV_API_PORT (default 8092)
    - FARMSURV_BOX_ID (default '')

OUTPUTS:
    - HTTP server on configured bind+port
    - /healthz endpoint returning server status
    - 17 GET routes mirroring MCP tools 1:1 via farm_api.*

DOES NOT DO:
    - Write tools (read-only in V2-072; write tools ship in V2-073)
    - TLS termination (operator responsibility)
    - Rate limiting or connection pooling

CALLERS:
    - python -m infra.api_server (standalone launcher)
    - HTTP integrations (n8n, Grafana, curl scripts)

USAGE:
    python -m infra.api_server
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse

import infra.net_bind
from infra.env_required import _require_env

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_ENABLED = os.environ.get("FARMSURV_API_ENABLED", "0") == "1"
_API_BIND_MODE = os.environ.get("FARMSURV_API_BIND", "tailnet-only")
_API_PORT = int(os.environ.get("FARMSURV_API_PORT", "8092"))
_BOX_ID = os.environ.get("FARMSURV_BOX_ID", os.environ.get("FARM_BOX_ID", ""))

# Server uptime at boot
_SERVER_START = datetime.datetime.now(datetime.UTC)

# Tailscale CGNAT address for healthz probes (primary target when bound
# to tailnet-only; 127.0.0.1 is used as a fallback below).
_TAILNET_API_URL = f"http://{infra.net_bind.TAILNET_BIND}:{_API_PORT}/healthz"

# ---------------------------------------------------------------------------
# Token validation (mirrors MCP server pattern)
# ---------------------------------------------------------------------------


def _validate_token() -> tuple[bool, str]:
    """Validate the FARMSURV_API_TOKEN env var at server startup.

    Uses _require_env for fail-loud behavior: if the token is missing,
    raise RuntimeError immediately rather than falling back to an empty
    string.

    Returns:
        (True, token) if the token is present and >= 32 chars.
        (False, error_msg) if too short.
    """
    token = _require_env("FARMSURV_API_TOKEN")
    if len(token) < 32:
        return False, (
            f"FARMSURV_API_TOKEN is too short ({len(token)} chars, "
            "need 32+). Generate a new one with: python -c "
            "'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return True, token


def _authenticate(auth_header: str | None) -> bool:
    """Check the Authorization header against the configured token.

    Per RFC 6750 the 'Bearer' scheme prefix is case-insensitive.
    Accepts 'Bearer xxx' and 'bearer xxx'; rejects 'BEARER xxx'
    (scheme must be lowercase or title-case; anything else is rejected
    to avoid ambiguity with non-standard schemes).

    Args:
        auth_header: The raw Authorization header value, or None.

    Returns:
        True if the header is present and matches, False otherwise.
    """
    if auth_header is None:
        return False

    # Case-insensitive scheme check per RFC 6750 Section 2.1.
    auth_lower = auth_header.lower()
    if not auth_lower.startswith("bearer "):
        return False

    provided_token = auth_header[7:]

    # Constant-time comparison to prevent timing attacks
    current_token = _require_env("FARMSURV_API_TOKEN")
    return _constant_time_compare(provided_token, current_token)


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


# ---------------------------------------------------------------------------
# Stable list endpoints (no Cache-Control needed beyond default)
# ---------------------------------------------------------------------------

# Endpoints that serve stable list data (cacheable).
_LIST_ENDPOINTS = frozenset({
    "/api/v1/cameras",
    "/api/v1/gate",
    "/api/v1/gate/suppression-rules",
    "/api/v1/cooldowns",
    "/api/v1/identities/people",
    "/api/v1/identities/vehicles",
    "/api/v1/identities/animals",
    "/api/v1/listener/status",
    "/api/v1/listener/errors",
    "/api/v1/telegram/status",
    "/api/v1/vision/status",
    "/api/v1/config",
})

# ---------------------------------------------------------------------------
# Route handlers (each is a 3-line adapter delegating to farm_api.*)
# ---------------------------------------------------------------------------


def _route_cameras(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.cameras import list_cameras
    cams = list_cameras()
    return 200, {"box_id": _BOX_ID, "data": [c.__dict__ for c in cams]}


def _route_camera_health(camera_id: str, **kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.cameras import get_camera_health
    result = get_camera_health(camera_id)
    if result is None:
        return 404, {"error": "Camera not found", "camera_id": camera_id}
    return 200, {"box_id": _BOX_ID, "data": result.__dict__}


def _route_camera_snapshot(camera_id: str, **kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.cameras import get_camera_snapshot
    path = get_camera_snapshot(camera_id)
    if path is None:
        return 404, {"error": "Snapshot not found", "camera_id": camera_id}
    return 200, {"box_id": _BOX_ID, "snapshot_path": str(path)}


def _route_events(limit: int = 50, since: str | None = None,
                  **kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.events import list_events
    try:
        result = list_events(limit=limit, since=since, **kwargs)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, {
        "box_id": _BOX_ID,
        "data": [r.__dict__ for r in result["data"]],
        "next_page_token": result.get("next_page_token"),
    }


def _route_event(alert_id: str, **kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.events import get_event
    record = get_event(alert_id)
    if record is None:
        return 404, {"error": "Event not found", "alert_id": alert_id}
    return 200, {"box_id": _BOX_ID, "data": record.__dict__}


def _route_gate(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.gate import get_gate_config
    return 200, {"box_id": _BOX_ID, "data": get_gate_config().__dict__}


def _route_suppression_rules(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.gate import list_suppression_rules
    return 200, {"box_id": _BOX_ID, "data": [r.__dict__ for r in list_suppression_rules()]}


def _route_cooldowns(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.cooldowns import list_active_cooldowns
    return 200, {"box_id": _BOX_ID, "data": [r.__dict__ for r in list_active_cooldowns()]}


def _route_identities_people(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.identities import list_known_people
    return 200, {"box_id": _BOX_ID, "data": [r.__dict__ for r in list_known_people()]}


def _route_identities_vehicles(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.identities import list_known_vehicles
    return 200, {"box_id": _BOX_ID, "data": [r.__dict__ for r in list_known_vehicles()]}


def _route_identities_animals(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.identities import list_known_animals
    return 200, {"box_id": _BOX_ID, "data": [r.__dict__ for r in list_known_animals()]}


def _route_listener_status(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.listener import get_listener_status
    return 200, {"box_id": _BOX_ID, "data": get_listener_status().__dict__}


def _route_listener_errors(limit: int = 50, since: str | None = None, **kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.listener import get_recent_errors
    records = get_recent_errors(n=limit)
    return 200, {"box_id": _BOX_ID, "data": [r.__dict__ for r in records]}


def _route_telegram_status(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.telegram import get_telegram_status
    return 200, {"box_id": _BOX_ID, "data": get_telegram_status().__dict__}


def _route_vision_status(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.vision import get_vision_status
    return 200, {"box_id": _BOX_ID, "data": get_vision_status().__dict__}


def _route_config(**kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.config import get_config
    return 200, {"box_id": _BOX_ID, "data": get_config().__dict__}


def _route_openapi(**kwargs: Any) -> tuple[int, Any]:
    """Return the OpenAPI 3.1 schema document."""
    doc = _generate_openapi_doc()
    return 200, doc


# ---------------------------------------------------------------------------
# URL routing table
# ---------------------------------------------------------------------------

# Pattern: (regex, handler)
# Parameters in named groups are passed to the handler as kwargs.
_ROUTE_TABLE: list[tuple[re.Pattern[str], Any]] = [
    (re.compile(r"^/api/v1/cameras$"), _route_cameras),
    (re.compile(r"^/api/v1/cameras/(?P<camera_id>[^/]+)/health$"), _route_camera_health),
    (re.compile(r"^/api/v1/cameras/(?P<camera_id>[^/]+)/snapshot$"), _route_camera_snapshot),
    (re.compile(r"^/api/v1/events$"), _route_events),
    (re.compile(r"^/api/v1/events/(?P<alert_id>[^/]+)$"), _route_event),
    (re.compile(r"^/api/v1/gate$"), _route_gate),
    (re.compile(r"^/api/v1/gate/suppression-rules$"), _route_suppression_rules),
    (re.compile(r"^/api/v1/cooldowns$"), _route_cooldowns),
    (re.compile(r"^/api/v1/identities/people$"), _route_identities_people),
    (re.compile(r"^/api/v1/identities/vehicles$"), _route_identities_vehicles),
    (re.compile(r"^/api/v1/identities/animals$"), _route_identities_animals),
    (re.compile(r"^/api/v1/listener/status$"), _route_listener_status),
    (re.compile(r"^/api/v1/listener/errors$"), _route_listener_errors),
    (re.compile(r"^/api/v1/telegram/status$"), _route_telegram_status),
    (re.compile(r"^/api/v1/vision/status$"), _route_vision_status),
    (re.compile(r"^/api/v1/config$"), _route_config),
    (re.compile(r"^/api/v1/openapi\.json$"), _route_openapi),
]

# ---------------------------------------------------------------------------
# OpenAPI 3.1 schema export
# ---------------------------------------------------------------------------

# Cache: generated once at import time from _ROUTE_TABLE.
_openapi_cache: dict[str, Any] | None = None


def _generate_openapi_doc() -> dict[str, Any]:
    """Build an OpenAPI 3.1.0 document from _ROUTE_TABLE.

    Each route is described as a GET operation. Path parameters are extracted
    from named regex groups; query parameters are inferred from the route
    handler's signature and _parse_qs defaults.
    """
    global _openapi_cache

    if _openapi_cache is not None:
        return _openapi_cache

    paths: dict[str, Any] = {}

    for pattern, handler in _ROUTE_TABLE:
        regex_str = pattern.pattern
        # Derive an OpenAPI path from the regex.
        # Named groups (?P<name>...) -> {name}, everything else stripped.
        # Replace full named group (name + inner pattern + closing paren).
        path_str = re.sub(r"\(\?P<([^>]+)>[^)]*\)", "{\\1}", regex_str)
        # Strip ^ and $ anchors
        path_str = path_str.lstrip("^").rstrip("$")
        # Replace literal regex dots with plain dots (e.g. openapi\.json)
        path_str = path_str.replace("\\.", ".")

        params: list[dict[str, Any]] = []

        # Path parameters from named groups
        for name in re.findall(r"\(\?P<([^>]+)>[^)]*\)", regex_str):
            params.append({
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            })

        # Query parameters from _parse_qs and handler defaults
        qs_params = _infer_query_params(handler, pattern)
        params.extend(qs_params)

        # Build response schema — the handler returns (status, body) where
        # body is a dict. We represent it as an object with generic properties.
        resp_200: dict[str, Any] = {
            "description": "Successful response",
            "content": {
                "application/json": {
                    "schema": {"type": "object", "properties": {
                        "box_id": {"type": "string"},
                        "data": {"type": "object"},
                        "request_id": {"type": "string"},
                        "code": {"type": "string"},
                        "status": {"type": "integer"},
                    }, "required": ["request_id", "code", "status"]}
                }
            }
        }

        # Some handlers return non-standard schemas
        resp_200 = _refine_response_schema(handler, regex_str, resp_200)

        # Generate a human-readable summary from the path
        summary = _path_to_summary(path_str)

        paths[path_str] = {
            "get": {
                "summary": summary,
                "parameters": params,
                "responses": {
                    "200": resp_200,
                    "401": {
                        "description": "Unauthorized",
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {
                                    "error": {"type": "string"},
                                    "request_id": {"type": "string"},
                                    "code": {"type": "string"},
                                    "status": {"type": "integer"},
                                }, "required": ["error", "request_id", "code", "status"]}
                            }
                        }
                    },
                    "404": {
                        "description": "Not found",
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {
                                    "error": {"type": "string"},
                                    "request_id": {"type": "string"},
                                    "code": {"type": "string"},
                                    "status": {"type": "integer"},
                                }, "required": ["error", "request_id", "code", "status"]}
                            }
                        }
                    },
                    "500": {
                        "description": "Internal error",
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {
                                    "error": {"type": "string"},
                                    "request_id": {"type": "string"},
                                }, "required": ["error", "request_id"]}
                            }
                        }
                    },
                },
            }
        }

    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "Farm Surveillance V2 API",
            "version": "0.1.0",
            "description": "Read-only HTTP API server for farm-surveillance-v2. "
                          "17 GET routes mirroring MCP tools 1:1 via farm_api.*",
        },
        "servers": [
            {
                "url": f"http://{infra.net_bind.TAILNET_BIND}:{_API_PORT}",
                "description": "tailnet",
            }
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "Bearer": {
                    "type": "http",
                    "scheme": "bearer",
                }
            }
        },
    }

    _openapi_cache = doc
    return doc


def _infer_query_params(handler, pattern: re.Pattern[str]) -> list[dict[str, Any]]:
    """Infer query parameters for a route handler.

    Only routes that actually accept query params (events, errors) get them
    in the OpenAPI doc. Other routes that go through _parse_qs but don't
    use those params aren't polluted with them.
    """
    params: list[dict[str, Any]] = []
    route_path = pattern.pattern.rstrip("$")

    # The events route: limit, since, page_token, camera_id
    if re.search(r"/events$", route_path):
        params.append({
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer"},
            "description": "Max events to return (default 50, max 200).",
        })
        params.append({
            "name": "since",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "ISO-8601 timestamp filter (inclusive).",
        })
        params.append({
            "name": "page_token",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Opaque cursor (base64 of last alert_id from previous page). "
                           "Absent on first call.",
        })
        params.append({
            "name": "camera_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Filter events by camera ID.",
        })

    # The errors route: limit, since
    elif re.search(r"/listener/errors$", route_path):
        params.append({
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer"},
            "description": "Max errors to return (default 50).",
        })
        params.append({
            "name": "since",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "ISO-8601 timestamp filter (inclusive).",
        })

    return params


def _path_to_summary(path_str: str) -> str:
    """Generate a human-readable summary from an OpenAPI path string."""
    # /api/v1/openapi.json -> GET OpenAPI schema
    if path_str == "/api/v1/openapi.json":
        return "Return OpenAPI 3.1 schema document"
    # /api/v1/events/{alert_id} -> Get event by alert_id
    parts = path_str.strip("/").split("/")
    if len(parts) >= 3:
        resource = parts[2]
        if "{alert_id}" in parts[3] if len(parts) > 3 else False:
            return f"Get {resource} by ID"
        if "{camera_id}" in parts[3] if len(parts) > 3 else False:
            sub = parts[4] if len(parts) > 4 else resource
            return f"Get {resource} {sub} by ID"
    # /api/v1/gate/suppression-rules -> List suppression rules
    return f"List {parts[2] if len(parts) > 2 else path_str}"


def _refine_response_schema(handler, route: str, default_schema) -> dict[str, Any]:
    """Refine the 200 response schema based on route-specific knowledge."""
    # Camera snapshot returns snapshot_path, not data
    if "snapshot" in route:
        default_schema["content"]["application/json"]["schema"]["properties"] = {
            "box_id": {"type": "string"},
            "snapshot_path": {"type": "string"},
            "request_id": {"type": "string"},
            "code": {"type": "string"},
            "status": {"type": "integer"},
        }
    return default_schema


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------


class _APIHandler(BaseHTTPRequestHandler):
    """Handle a single API HTTP request."""

    # US-101y: suppress Python version leak in Server header.
    server_version = "farm-api/0.1"
    sys_version = ""  # empty so BaseHTTPRequestHandler doesn't append Python/3.11.14

    # Suppress default stderr logging (keep it clean for loggers)
    def log_message(self, format, *args):
        log.debug(format, *args)

    def _generate_request_id(self) -> str:
        """Generate a unique request ID for this request."""
        return str(uuid.uuid4())

    def _send_json(self, status_code, body):
        """Send a JSON response with standard headers and error envelope.

        US-101y additions:
        - Adds request_id, code, status fields to every body.
        - Sets Cache-Control on stable list endpoints.
        - Adds X-Content-Type-Options: nosniff.
        """
        # Ensure every body has the standard envelope fields.
        if "request_id" not in body:
            body = dict(body)
            body["request_id"] = getattr(self, "_request_id", "")
            body["code"] = getattr(self, "_error_code", "ok")
            body["status"] = status_code

        # Cache-Control: cache stable list endpoints, no-store for lookups.
        path = getattr(self, "_request_path", "")
        if path in _LIST_ENDPOINTS:
            cache_control = "max-age=5, private"
        elif path.startswith("/api/v1/events/") and path != "/api/v1/events":
            # Single event lookup — no caching.
            cache_control = "no-store"
        else:
            # Default: no caching for API responses.
            cache_control = "no-store"

        body_str = json.dumps(body)
        encoded = body_str.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _log_access(self, method: str, path: str, status: int,
                    elapsed_ms: float) -> None:
        """Log a per-request access line. Skips /healthz."""
        if path == "/healthz":
            return
        request_id = getattr(self, "_request_id", "")
        log.info(
            "api: method=%s path=%s status=%d elapsed_ms=%.1f request_id=%s box_id=%s",
            method, path, status, elapsed_ms, request_id, _BOX_ID,
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        self._request_path = parsed.path
        self._request_id = self._generate_request_id()

        start = time.monotonic()

        # Healthz - no auth required
        if parsed.path == "/healthz":
            self._handle_healthz()
            elapsed = (time.monotonic() - start) * 1000
            self._log_access("GET", parsed.path, 200, elapsed)
            return

        # Auth check for all /api/* routes
        if not parsed.path.startswith("/api/"):
            self._send_json(404, {"error": "Not found"})
            elapsed = (time.monotonic() - start) * 1000
            self._log_access("GET", parsed.path, 404, elapsed)
            return

        auth_header = self.headers.get("Authorization")
        if not _authenticate(auth_header):
            # US-101y: add WWW-Authenticate header on 401.
            body_str = json.dumps({
                "request_id": self._request_id,
                "code": "unauthorized",
                "error": "Unauthorized: missing or invalid bearer token",
                "status": 401,
            })
            encoded = body_str.encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", 'Bearer realm="farm-api"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)
            elapsed = (time.monotonic() - start) * 1000
            self._log_access("GET", parsed.path, 401, elapsed)
            return

        # Query string parsing
        qs = {}
        if parsed.query:
            for param in parsed.query.split("&"):
                if "=" in param:
                    key, val = param.split("=", 1)
                    qs[key] = val

        # Match against route table
        for pattern, handler in _ROUTE_TABLE:
            m = pattern.match(parsed.path)
            if m:
                try:
                    status, body = handler(**m.groupdict(), **_parse_qs(qs))
                except Exception:
                    # US-101y: sanitize 500 — never leak Python internals.
                    log.exception("api: route %s failed", parsed.path)
                    self._request_id = self._generate_request_id()
                    self._send_json(500, {
                        "error": "internal error",
                        "request_id": self._request_id,
                    })
                    elapsed = (time.monotonic() - start) * 1000
                    self._log_access("GET", parsed.path, 500, elapsed)
                    return
                self._send_json(status, body)
                elapsed = (time.monotonic() - start) * 1000
                self._log_access("GET", parsed.path, status, elapsed)
                return

        self._send_json(404, {"error": "Not found"})
        elapsed = (time.monotonic() - start) * 1000
        self._log_access("GET", parsed.path, 404, elapsed)

    def _send_405_json(self):
        """Return a JSON 405 Method Not Allowed response."""
        self._request_id = self._generate_request_id()
        body = {
            "request_id": self._request_id,
            "code": "method_not_allowed",
            "error": "Method not allowed",
            "status": 405,
        }
        self._send_json(405, body)

    def do_POST(self):
        parsed = urlparse(self.path)
        self._request_path = parsed.path
        self._request_id = self._generate_request_id()

        start = time.monotonic()

        # Auth check for all /api/* routes
        if not parsed.path.startswith("/api/"):
            self._send_json(404, {"error": "Not found"})
            elapsed = (time.monotonic() - start) * 1000
            self._log_access("POST", parsed.path, 404, elapsed)
            return

        auth_header = self.headers.get("Authorization")
        if not _authenticate(auth_header):
            body_str = json.dumps({
                "request_id": self._request_id,
                "code": "unauthorized",
                "error": "Unauthorized: missing or invalid bearer token",
                "status": 401,
            })
            encoded = body_str.encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", 'Bearer realm="farm-api"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)
            elapsed = (time.monotonic() - start) * 1000
            self._log_access("POST", parsed.path, 401, elapsed)
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(400, {
                "error": "Parse error: empty body",
                "request_id": self._request_id,
            })
            elapsed = (time.monotonic() - start) * 1000
            self._log_access("POST", parsed.path, 400, elapsed)
            return

        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {
                "error": "Parse error: invalid JSON",
                "request_id": self._request_id,
            })
            elapsed = (time.monotonic() - start) * 1000
            self._log_access("POST", parsed.path, 400, elapsed)
            return

        # Route POST requests
        if parsed.path == "/api/v1/identities/people":
            try:
                result = self._handle_post_enroll_person(data)
                status, resp = result
            except Exception:
                log.exception("api: POST /api/v1/identities/people failed")
                self._request_id = self._generate_request_id()
                self._send_json(500, {
                    "error": "internal error",
                    "request_id": self._request_id,
                })
                elapsed = (time.monotonic() - start) * 1000
                self._log_access("POST", parsed.path, 500, elapsed)
                return
            self._send_json(status, resp)
            elapsed = (time.monotonic() - start) * 1000
            self._log_access("POST", parsed.path, status, elapsed)
            return

        self._send_405_json()
        elapsed = (time.monotonic() - start) * 1000
        self._log_access("POST", parsed.path, 405, elapsed)

    def _handle_post_enroll_person(self, data: dict) -> tuple[int, dict]:
        """Handle POST /api/v1/identities/people — enroll a known person."""
        from infra.farm_api import enroll_known_person

        name = data.get("name")
        person_id = data.get("id")
        identity_markers = data.get("identity_markers", [])
        image_paths = data.get("image_paths", [])

        if not name or not person_id:
            return 400, {
                "error": "Bad request: 'name' and 'id' are required",
                "request_id": self._request_id,
            }
        if not image_paths or not isinstance(image_paths, list):
            return 400, {
                "error": "Bad request: 'image_paths' must be a non-empty list",
                "request_id": self._request_id,
            }

        try:
            entry = enroll_known_person(
                name=name,
                person_id=person_id,
                identity_markers=identity_markers,
                image_paths=image_paths,
            )
            return 200, {
                "box_id": _BOX_ID,
                "request_id": self._request_id,
                "data": entry,
            }
        except ValueError as exc:
            return 409, {
                "error": str(exc),
                "request_id": self._request_id,
            }

    def do_PUT(self):
        self._request_path = urlparse(self.path).path
        self._send_405_json()

    def do_DELETE(self):
        self._request_path = urlparse(self.path).path
        self._send_405_json()

    def do_OPTIONS(self):
        self._request_path = urlparse(self.path).path
        self._send_405_json()

    # ---- Helpers ----

    def _handle_healthz(self):
        """Return health status."""
        uptime = (
            datetime.datetime.now(datetime.UTC) - _SERVER_START
        ).total_seconds()
        response = {
            "ok": _API_ENABLED,
            "box_id": _BOX_ID,
            "uptime_seconds": round(uptime, 1),
            "api_enabled": _API_ENABLED,
        }
        self._send_json(200, response)


def _parse_qs(qs: dict[str, str]) -> dict[str, Any]:
    """Cast query-string values to appropriate types for route handlers."""
    result: dict[str, Any] = {}
    # Defaults
    result.setdefault("limit", 50)
    result.setdefault("since", None)
    result.setdefault("page_token", None)
    result.setdefault("camera_id", None)
    for key, val in qs.items():
        if key == "limit":
            try:
                result["limit"] = int(val)
            except ValueError:
                pass
        elif key == "since":
            result["since"] = val
        elif key in ("page_token", "camera_id"):
            result[key] = val
    return result


# ---------------------------------------------------------------------------
# ThreadingHTTPServer (for concurrent request handling)
# ---------------------------------------------------------------------------


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread."""

    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Startup logic
# ---------------------------------------------------------------------------


def _start_server() -> _ThreadingHTTPServer | None:
    """Start the API HTTP server and return the server instance.

    This function blocks until the server is shut down.
    """
    if not _API_ENABLED:
        log.info("API server disabled (FARMSURV_API_ENABLED=0)")
        return None

    # Validate token
    valid, message = _validate_token()
    if not valid:
        log.error("API startup failed: %s", message)
        print(f"ERROR: API server cannot start: {message}", file=sys.stderr)
        sys.exit(1)

    bind_addr = infra.net_bind.resolve_bind_address(_API_BIND_MODE)

    # US-101y: bind mode validation. When FARMSURV_API_BIND=tailnet-only,
    # assert the bound address is in the Tailscale CGNAT range. This catches
    # typos like FARMSURV_API_BIND=lol that resolve to a non-Tailscale IP.
    if _API_BIND_MODE == "tailnet-only" and not infra.net_bind.is_tailnet_ip(bind_addr):
        log.error(
            "Bind mode validation failed: resolved %s is not in "
            "Tailscale CGNAT (100.64.0.0/10). Refusing to start. "
            "Check FARMSURV_API_BIND setting.",
            bind_addr,
        )
        print(
            f"ERROR: bind mode validation failed — "
            f"resolved {bind_addr} is not in Tailscale CGNAT (100.64.0.0/10). "
            f"Check FARMSURV_API_BIND setting.",
            file=sys.stderr,
        )
        sys.exit(1)

    server = _ThreadingHTTPServer((bind_addr, _API_PORT), _APIHandler)

    log.info(
        "API server starting on %s:%d (bind=%s, box_id=%s)",
        bind_addr,
        _API_PORT,
        _API_BIND_MODE,
        _BOX_ID,
    )
    return server


def run():
    """Entry point: start the API server.

    Called via `python -m infra.api_server`.
    """
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    server = _start_server()
    if server is None:
        return

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("API server shutting down")
        server.shutdown()


if __name__ == "__main__":
    run()
