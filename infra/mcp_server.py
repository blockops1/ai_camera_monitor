"""mcp_server -- Streamable-HTTP MCP server for farm-surveillance system API.

STATUS: alpha -- V2-068c
THREAD SAFETY: not thread-safe (http.server is single-threaded by default)
               Use ThreadingHTTPServer for concurrent requests.

INPUTS:
    - FARMSURV_MCP_ENABLED (1=ON, 0=OFF, default 0)
    - FARMSURV_MCP_BIND (tailnet-only | all | <interface>, default tailnet-only)
    - FARMSURV_MCP_TOKEN (required when enabled, 32+ chars)
    - FARMSURV_MCP_PORT (default 8091)
    - FARMSURV_BOX_ID (default $(hostname))

OUTPUTS:
    - HTTP server on configured bind+port
    - /mcp endpoint accepting JSON-RPC 2.0 (tools/list, tools/call)
    - /healthz endpoint returning server status

DOES NOT DO:
    - Write tools (read-only in V2-068; write tools ship in V2-069)
    - TLS termination (operator responsibility)
    - Rate limiting or connection pooling
    - SSE streaming (basic JSON response only)

CALLERS:
    - python -m infra.mcp_server (standalone launcher)
    - MCP clients (Claude Desktop, VS Code, custom agents)

USAGE:
    python -m infra.mcp_server
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import sys
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse

from infra.mcp_auth import authenticate, validate_token, www_authenticate_header

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MCP_ENABLED = os.environ.get("FARMSURV_MCP_ENABLED", "0") == "1"
_MCP_BIND_MODE = os.environ.get("FARMSURV_MCP_BIND", "tailnet-only")
_MCP_PORT = int(os.environ.get("FARMSURV_MCP_PORT", "8091"))
_BOX_ID = os.environ.get("FARMSURV_BOX_ID", socket.gethostname())

# Server uptime at boot
_SERVER_START = datetime.datetime.now(datetime.UTC)

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class _Tool:
    """A single MCP tool: name, description, schema, handler."""

    __slots__ = ("description", "handler", "input_schema", "name")

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler,
    ):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


# ---------------------------------------------------------------------------
# Tool adapters (3-line: decorator, call farm_api.X(), return asdict + box_id)
# ---------------------------------------------------------------------------


class _ToolRegistry:
    """Registry of MCP tools backed by farm_api functions."""

    def __init__(self, box_id: str):
        self._box_id = box_id
        self._tools: dict[str, _Tool] = {}

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
    ):
        """Decorator: register a farm_api function as an MCP tool."""

        def decorator(func):
            self._tools[name] = _Tool(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=lambda **kw: self._wrap(func, **kw),
            )
            return func

        return decorator

    def _wrap(self, func, **kwargs):
        """Call the farm_api function and append box_id to the result."""
        result = func(**kwargs)
        if result is None:
            return None
        if isinstance(result, dict):
            result = dict(result)
            # Convert EventRecord objects in "data" list to dicts for JSON
            if "data" in result and isinstance(result["data"], list):
                converted = []
                for item in result["data"]:
                    try:
                        converted.append({**asdict(item)})
                    except TypeError:
                        converted.append(item)
                result["data"] = converted
            result["box_id"] = self._box_id
        elif isinstance(result, list):
            # List of dataclasses (e.g. list[CameraInfo]) — map asdict over items
            try:
                result = [{**asdict(item), "box_id": self._box_id} for item in result]
            except TypeError:
                result = [{"box_id": self._box_id}]
        else:
            # Single dataclass or other — convert via asdict or dict
            try:
                result = {**asdict(result), "box_id": self._box_id}
            except TypeError:
                result = {"box_id": self._box_id}
        return result

    def list_tools(self) -> list[dict]:
        """Return the tool list in MCP format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a registered tool by name."""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        return tool.handler(**arguments)


# ---------------------------------------------------------------------------
# Build tool registry from farm_api modules
# ---------------------------------------------------------------------------


def _build_registry() -> _ToolRegistry:
    """Construct the MCP tool registry from farm_api modules.

    Each module in infra.farm_api is introspected for functions, and each
    function becomes an MCP tool. The input schema is derived from the
    function's docstring (simple heuristic) and defaults to an empty object.
    """
    registry = _ToolRegistry(_BOX_ID)

    # Import farm_api modules

    # Manually register each farm_api function as an MCP tool.
    # This is the explicit list — not auto-discovery, because we need
    # to set proper descriptions and input schemas.

    _register_tools(registry)

    return registry


def _register_tools(registry: _ToolRegistry) -> None:
    """Register all farm_api functions as MCP tools.

    Each tool wraps a farm_api function call and appends box_id to the
    response. The MCP tool name is the function name (snake_case ->
    camelCase via the JSON Schema naming convention).
    """
    from infra.farm_api import cameras, config, gate, listener, vision

    # ---- cameras ----

    registry.tool(
        name="list_cameras",
        description="List all known cameras with redacted IPs.",
        input_schema={"type": "object", "properties": {}},
    )(cameras.list_cameras)

    registry.tool(
        name="get_camera_health",
        description="Get health status for a specific camera.",
        input_schema={
            "type": "object",
            "properties": {
                "camera_id": {
                    "type": "string",
                    "description": "Camera prefix, e.g. FRONT, BACK.",
                },
            },
            "required": ["camera_id"],
        },
    )(cameras.get_camera_health)

    registry.tool(
        name="get_camera_snapshot",
        description="Get the latest frame path for a camera.",
        input_schema={
            "type": "object",
            "properties": {
                "camera_id": {
                    "type": "string",
                    "description": "Camera prefix, e.g. FRONT, BACK.",
                },
            },
            "required": ["camera_id"],
        },
    )(cameras.get_camera_snapshot)

    # ---- events ----

    from infra.farm_api import events

    registry.tool(
        name="list_events",
        description="List events, newest first. Optional camera_id and since filter.",
        input_schema={
            "type": "object",
            "properties": {
                "camera_id": {
                    "type": "string",
                    "description": "Filter by camera prefix.",
                },
                "since": {
                    "type": "string",
                    "description": "ISO-8601 timestamp; only events after this.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events to return (default 50).",
                },
                "page_token": {
                    "type": "string",
                    "description": "Opaque cursor (base64 of last alert_id from previous page).",
                },
            },
            "required": [],
        },
    )(events.list_events)

    registry.tool(
        name="get_event",
        description="Get a single event by alert_id.",
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "The alert_id (UUID string).",
                },
            },
            "required": ["event_id"],
        },
    )(events.get_event)

    # ---- gate ----

    registry.tool(
        name="get_gate_config",
        description="Get the motion gate configuration from env vars.",
        input_schema={"type": "object", "properties": {}},
    )(gate.get_gate_config)

    registry.tool(
        name="list_suppression_rules",
        description="List active gate suppression rules.",
        input_schema={"type": "object", "properties": {}},
    )(gate.list_suppression_rules)

    # ---- cooldowns ----

    from infra.farm_api import cooldowns

    registry.tool(
        name="list_active_cooldowns",
        description="List currently active cooldowns.",
        input_schema={"type": "object", "properties": {}},
    )(cooldowns.list_active_cooldowns)

    # ---- identities ----

    from infra.farm_api import identities

    registry.tool(
        name="list_known_people",
        description="List known people identities.",
        input_schema={"type": "object", "properties": {}},
    )(identities.list_known_people)

    registry.tool(
        name="list_known_vehicles",
        description="List known vehicle identities.",
        input_schema={"type": "object", "properties": {}},
    )(identities.list_known_vehicles)

    registry.tool(
        name="list_known_animals",
        description="List known animal identities.",
        input_schema={"type": "object", "properties": {}},
    )(identities.list_known_animals)

    registry.tool(
        name="enroll_person",
        description="Enroll a known person with photos and metadata. Writes to data/known/people/known_people.json and embeddings.npz.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Display name / label for the person.",
                },
                "id": {
                    "type": "string",
                    "description": "Unique person identifier.",
                },
                "identity_markers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Identity marker tokens for matching (e.g. ['red-jacket', 'beard']).",
                },
                "image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Local file paths to sample images for enrollment.",
                },
            },
            "required": ["name", "id", "image_paths"],
        },
    )(identities.enroll_known_person)

    # ---- listener ----

    registry.tool(
        name="get_listener_status",
        description="Get the listener daemon status (PID, uptime, mode).",
        input_schema={"type": "object", "properties": {}},
    )(listener.get_listener_status)

    registry.tool(
        name="get_recent_errors",
        description="Get recent ERROR lines from the daemon log.",
        input_schema={
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "Number of errors to return (default 20).",
                },
            },
            "required": [],
        },
    )(listener.get_recent_errors)

    # ---- vision ----

    registry.tool(
        name="get_vision_status",
        description="Get the vision analyzer subsystem status.",
        input_schema={"type": "object", "properties": {}},
    )(vision.get_vision_status)

    # ---- telegram ----

    from infra.farm_api import telegram

    registry.tool(
        name="get_telegram_status",
        description="Get Telegram dispatch status (chat_id redacted, last-send health).",
        input_schema={"type": "object", "properties": {}},
    )(telegram.get_telegram_status)

    # ---- config ----

    registry.tool(
        name="get_config",
        description="Get operational config: env vars, documented/missing knobs.",
        input_schema={"type": "object", "properties": {}},
    )(config.get_config)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 handler
# ---------------------------------------------------------------------------


def _handle_request(
    method: str,
    params: dict | None,
    id_val: str | int | None,
    request_id: str | None = None,
) -> tuple[int, Any]:
    """Handle a single JSON-RPC 2.0 request.

    Args:
        method: The JSON-RPC method name.
        params: The params dict, or None.
        id_val: The request ID (for responses).
        request_id: Per-request UUID for tracing.

    Returns:
        (status_code, response_body) tuple.
    """
    registry = _GLOBAL_REGISTRY[0]
    if registry is None:
        return 503, _json_error(id_val, -32000, "Server not initialized", request_id)

    if method == "initialize":
        return 200, _json_ok(
            id_val,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "farm-surveillance", "version": "0.6.3"},
            },
            request_id,
        )

    if method == "tools/list":
        return 200, _json_ok(id_val, {"tools": registry.list_tools()}, request_id)

    if method == "tools/call":
        if not params:
            return 400, _json_error(
                id_val, -32602, "tools/call requires 'name' and 'arguments'", request_id
            )
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result = registry.call_tool(tool_name, arguments)
            return 200, _json_ok(
                id_val,
                {"content": [{"type": "text", "text": json.dumps(result)}]},
                request_id,
            )
        except KeyError as exc:
            return 404, _json_error(id_val, -32601, str(exc), request_id)
        except Exception as exc:
            log.exception("mcp: tool call %s failed", tool_name)
            return 500, _json_error(id_val, -32603, f"Internal error: {exc}", request_id)

    if method == "notifications/initialized":
        # Notification, no response needed
        return 200, None

    # Unknown method
    return 400, _json_error(id_val, -32601, f"Method not found: {method}", request_id)


def _json_ok(id_val, result, request_id=None) -> dict:
    resp: dict = {"jsonrpc": "2.0", "result": result, "id": id_val}
    if request_id:
        resp["request_id"] = request_id
    return resp


def _json_error(id_val, code, message, request_id=None) -> dict:
    resp: dict = {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": id_val,
    }
    if request_id:
        resp["request_id"] = request_id
    return resp


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

_GLOBAL_REGISTRY: list[_ToolRegistry | None] = [None]


class _MCPHandler(BaseHTTPRequestHandler):
    """Handle a single MCP HTTP request."""

    # Fix 6: Suppress Python version leak in Server header
    server_version = "farm-mcp/0.1"
    sys_version = ""  # Suppress sys.version appended to Server header

    # Suppress default stderr logging (keep it clean for loggers)
    def log_message(self, format, *args):
        log.debug(format, *args)

    # ---- GET /healthz ----

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._handle_healthz()
        elif parsed.path == "/mcp":
            # Fix 4: JSON 405 for GET on /mcp (not HTML)
            self._method_not_allowed()
        else:
            self._not_found()

    # ---- PUT /mcp ----

    def do_PUT(self):
        self._method_not_allowed()

    # ---- DELETE /mcp ----

    def do_DELETE(self):
        self._method_not_allowed()

    # ---- OPTIONS /mcp ----

    def do_OPTIONS(self):
        self._method_not_allowed()

    # ---- POST /mcp ----

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/mcp":
            self._not_found()
            return

        # Fix 5: Per-request access log (capture start time)
        import time as _time
        _start = _time.time()

        # Generate per-request UUID for tracing (Fix 3)
        _req_id = str(uuid.uuid4())
        self._request_id = _req_id  # Available to _log_access

        # Auth check
        auth_header = self.headers.get("Authorization")
        if not authenticate(auth_header):
            # Fix 1: WWW-Authenticate header on 401
            body = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": "Unauthorized: missing or invalid bearer token",
                },
                "id": None,
                "request_id": _req_id,
            }
            self._send_json_with_headers(401, body, [("WWW-Authenticate", www_authenticate_header())])
            self._log_access("POST /mcp", 401, _start)
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            response_body = {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32700,
                    "message": "Parse error: empty body",
                },
                "id": None,
                "request_id": _req_id,
            }
            self._send_json(400, response_body)
            self._log_access("POST /mcp", 400, _start)
            return

        body = self.rfile.read(content_length)
        try:
            message = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32700,
                        "message": "Parse error: invalid JSON",
                    },
                    "id": None,
                },
            )
            self._log_access("POST /mcp", 400, _start)
            return

        method = message.get("method", "")
        params = message.get("params")
        id_val = message.get("id")

        status, response_body = _handle_request(method, params, id_val, _req_id)
        self._send_json(status, response_body)
        self._log_access(method, status, _start)

    # ---- Helpers ----

    def _send_json_with_headers(self, status_code, body, extra_headers=None):
        """Send a JSON response with optional extra headers."""
        body_str = json.dumps(body) if isinstance(body, dict) else body
        encoded = body_str.encode("utf-8") if isinstance(body_str, str) else body_str
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if extra_headers:
            for name, value in extra_headers:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _log_access(self, method, status, start):
        """Fix 5: Per-request access log. Format: mcp: method=X status=N elapsed_ms=Y request_id=Z box_id=W"""
        elapsed_ms = round((datetime.datetime.now(datetime.UTC).timestamp() - start) * 1000, 1)
        _req_id = getattr(self, '_request_id', '<none>')
        log.info(
            "mcp: method=%s status=%d elapsed_ms=%s request_id=%s box_id=%s",
            method, status, elapsed_ms, _req_id, _BOX_ID,
        )

    def _method_not_allowed(self):
        """Fix 4: JSON 405 for unsupported HTTP methods on /mcp."""
        self._send_json(
            405,
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32601,
                    "message": "Method not allowed",
                },
                "id": None,
            },
        )

    def _handle_healthz(self):
        """Return health status."""
        now = datetime.datetime.now(datetime.UTC)
        uptime = (now - _SERVER_START).total_seconds()

        healthy = _MCP_ENABLED and _GLOBAL_REGISTRY[0] is not None

        response = {
            "ok": healthy,
            "box_id": _BOX_ID,
            "uptime_seconds": round(uptime, 1),
            "mcp_enabled": _MCP_ENABLED,
        }
        self._send_json(200, response)

    def _not_found(self):
        self._send_json(
            404,
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32601,
                    "message": "Not found",
                },
                "id": None,
            },
        )

    # Fix 7: X-Content-Type-Options: nosniff on every response
    # Override to inject the header into every response automatically.
    def send_response(self, code, message=None):
        super().send_response(code, message)
        self.send_header("X-Content-Type-Options", "nosniff")

    def _send_json(self, status_code, body):
        """Send a JSON response."""
        body_str = json.dumps(body) if isinstance(body, dict) else body
        encoded = body_str.encode("utf-8") if isinstance(body_str, str) else body_str
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


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


def _resolve_bind_address() -> str:
    """Resolve the bind address based on FARMSURV_MCP_BIND.

    Delegates to infra.net_bind.resolve_bind_address.

    Returns:
        The IP address string to bind to.
    """
    from infra.net_bind import resolve_bind_address

    return resolve_bind_address(_MCP_BIND_MODE)


def _start_server() -> _ThreadingHTTPServer | None:
    """Start the MCP HTTP server and return the server instance.

    This function blocks until the server is shut down.
    """
    if not _MCP_ENABLED:
        log.info("MCP server disabled (FARMSURV_MCP_ENABLED=0)")
        return None

    # Validate token
    valid, message = validate_token()
    if not valid:
        log.error("MCP startup failed: %s", message)
        print(f"ERROR: MCP server cannot start: {message}", file=sys.stderr)
        sys.exit(1)

    _GLOBAL_REGISTRY[0] = _build_registry()

    bind_addr = _resolve_bind_address()

    server = _ThreadingHTTPServer((bind_addr, _MCP_PORT), _MCPHandler)

    log.info(
        "MCP server starting on %s:%d (bind=%s, box_id=%s)",
        bind_addr,
        _MCP_PORT,
        _MCP_BIND_MODE,
        _BOX_ID,
    )
    return server


def run():
    """Entry point: start the MCP server.

    Called via `python -m infra.mcp_server`.
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
        log.info("MCP server shutting down")
        server.shutdown()


if __name__ == "__main__":
    run()
