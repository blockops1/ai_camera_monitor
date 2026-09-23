"""api_server -- Read-only HTTP API server for farm-surveillance.

STATUS: alpha -- V2-072b
THREAD SAFETY: not thread-safe (http.server is single-threaded by default)
               Use ThreadingHTTPServer for concurrent requests.

INPUTS:
    - FARMSURV_API_ENABLED (1=ON, 0=OFF, default 0)
    - FARMSURV_API_BIND (tailnet-only | all | <interface>, default tailnet-only)
    - FARMSURV_API_TOKEN (required when enabled, 32+ chars)
    - FARMSURV_API_PORT (default 8092)

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

import json
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse

import infra.net_bind

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_ENABLED = os.environ.get("FARMSURV_API_ENABLED", "0") == "1"
_API_BIND_MODE = os.environ.get("FARMSURV_API_BIND", "tailnet-only")
_API_PORT = int(os.environ.get("FARMSURV_API_PORT", "8092"))
_API_TOKEN = os.environ.get("FARMSURV_API_TOKEN", "")
_BOX_ID = os.environ.get("FARMSURV_BOX_ID", os.environ.get("FARM_BOX_ID", ""))

# ---------------------------------------------------------------------------
# Token validation (mirrors MCP server pattern)
# ---------------------------------------------------------------------------


def _validate_token() -> tuple[bool, str]:
    """Validate the FARMSURV_API_TOKEN env var at server startup.

    Returns:
        (True, token) if the token is present and >= 32 chars.
        (False, error_msg) if missing or too short.
    """
    token = os.environ.get("FARMSURV_API_TOKEN", "")
    if not token:
        return False, (
            "FARMSURV_API_TOKEN is required but not set. "
            "Generate one with: python -c "
            "'import secrets; print(secrets.token_urlsafe(32))'"
        )
    if len(token) < 32:
        return False, (
            f"FARMSURV_API_TOKEN is too short ({len(token)} chars, "
            "need 32+). Generate a new one with: python -c "
            "'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return True, token


def _authenticate(auth_header: str | None) -> bool:
    """Check the Authorization header against the configured token.

    Args:
        auth_header: The raw Authorization header value, or None.

    Returns:
        True if the header is present and matches, False otherwise.
    """
    if auth_header is None:
        return False

    if not auth_header.startswith("Bearer "):
        return False

    provided_token = auth_header[7:]

    # Constant-time comparison to prevent timing attacks
    current_token = os.environ.get("FARMSURV_API_TOKEN", "")
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
    from pathlib import Path
    path = get_camera_snapshot(camera_id)
    if path is None:
        return 404, {"error": "Snapshot not found", "camera_id": camera_id}
    return 200, {"box_id": _BOX_ID, "snapshot_path": str(path)}


def _route_events(limit: int = 50, since: str | None = None, **kwargs: Any) -> tuple[int, Any]:
    from infra.farm_api.events import list_events
    records = list_events(limit=limit, since=since)
    return 200, {"box_id": _BOX_ID, "data": [r.__dict__ for r in records]}


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
]

# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------


class _APIHandler(BaseHTTPRequestHandler):
    """Handle a single API HTTP request."""

    # Suppress default stderr logging (keep it clean for loggers)
    def log_message(self, format, *args):
        log.debug(format, *args)

    def do_GET(self):
        parsed = urlparse(self.path)

        # Healthz — no auth required
        if parsed.path == "/healthz":
            self._handle_healthz()
            return

        # Auth check for all /api/* routes
        if not parsed.path.startswith("/api/"):
            self._send_json(404, {"error": "Not found"})
            return

        auth_header = self.headers.get("Authorization")
        if not _authenticate(auth_header):
            self._send_json(401, {"error": "Unauthorized: missing or invalid bearer token"})
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
                except Exception as exc:
                    log.exception("api: route %s failed", parsed.path)
                    self._send_json(500, {"error": str(exc)})
                    return
                self._send_json(status, body)
                return

        self._send_json(404, {"error": "Not found"})

    # ---- Helpers ----

    def _handle_healthz(self):
        """Return health status."""
        response = {
            "ok": _API_ENABLED,
            "box_id": _BOX_ID,
            "api_enabled": _API_ENABLED,
        }
        self._send_json(200, response)

    def _send_json(self, status_code, body):
        """Send a JSON response."""
        body_str = json.dumps(body)
        encoded = body_str.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _parse_qs(qs: dict[str, str]) -> dict[str, Any]:
    """Cast query-string values to appropriate types for route handlers."""
    result: dict[str, Any] = {}
    # Defaults
    result.setdefault("limit", 50)
    result.setdefault("since", None)
    for key, val in qs.items():
        if key == "limit":
            try:
                result["limit"] = int(val)
            except ValueError:
                pass
        elif key == "since":
            result["since"] = val
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
