# Farm Surveillance v2 — MCP Server API

Streamable-HTTP MCP server for the farm-surveillance alert pipeline.
Built in V2-068c.

## Transport

- Protocol: JSON-RPC 2.0 over HTTP
- Endpoint: `POST /mcp`
- Health check: `GET /healthz`
- Server sends SSE-like `text/event-stream` with `text/event-source` type
  for the initialize handshake (MCP spec 2025-06-18).

### Initialize handshake

```
POST /mcp
Content-Type: application/json

{"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 0}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "result": {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {}, "resources": {}},
    "serverInfo": {"name": "farm-surveillance", "version": "0.6.3"}
  },
  "id": 0
}
```

Client then sends:

```
POST /mcp
Content-Type: application/json

{"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
```

No response body required.

## Authentication

Bearer-token auth on all POST /mcp requests.

- Token source: `FARMSURV_MCP_TOKEN` env var (required, 32+ chars)
- Header: `Authorization: Bearer <token>`
- Missing or wrong token: HTTP 401 with JSON-RPC error
- Comparison is constant-time to prevent timing attacks

Healthz endpoint (`GET /healthz`) does NOT require auth.

### Healthz response

```json
{
  "ok": true,
  "box_id": "jills-mac-mini",
  "uptime_seconds": 1420.5,
  "mcp_enabled": true
}
```

## Tools

Each tool is a thin adapter over a `farm_api` function. The MCP server
appends `box_id` to every response.

### list_cameras

- **Description:** List all known cameras with redacted IPs.
- **Inputs:** none
- **Output:** `list[CameraInfo]` — see docs/API.md#CameraInfo
- **farm_api function:** `infra.farm_api.cameras.list_cameras`

### get_camera_health

- **Description:** Get health status for a specific camera.
- **Inputs:**
  - `camera_id` (string, required): Camera prefix, e.g. "FRONT"
- **Output:** `CameraHealth | null` — see docs/API.md#CameraHealth
- **farm_api function:** `infra.farm_api.cameras.get_camera_health`

### get_camera_snapshot

- **Description:** Get the latest frame path for a camera.
- **Inputs:**
  - `camera_id` (string, required): Camera prefix, e.g. "FRONT"
- **Output:** `string | null` — absolute path to latest frame PNG
- **farm_api function:** `infra.farm_api.cameras.get_camera_snapshot`

### list_events

- **Description:** List events, newest first. Optional camera_id and since filter.
- **Inputs:**
  - `camera_id` (string, optional): Filter by camera prefix
  - `since` (string, optional): ISO-8601 timestamp; only events after this
  - `limit` (integer, optional): Max events to return (default 50)
- **Output:** `list[EventRecord]` — see docs/API.md#EventRecord
- **farm_api function:** `infra.farm_api.events.list_events`

### get_event

- **Description:** Get a single event by alert_id.
- **Inputs:**
  - `event_id` (string, required): The alert_id (UUID string)
- **Output:** `EventRecord | null` — see docs/API.md#EventRecord
- **farm_api function:** `infra.farm_api.events.get_event`

### get_gate_config

- **Description:** Get the motion gate configuration from env vars.
- **Inputs:** none
- **Output:** `GateConfig` — see docs/API.md#GateConfig
- **farm_api function:** `infra.farm_api.gate.get_gate_config`

### list_suppression_rules

- **Description:** List active gate suppression rules.
- **Inputs:** none
- **Output:** `list[SuppressionRule]` — see docs/API.md#SuppressionRule
- **farm_api function:** `infra.farm_api.gate.list_suppression_rules`

### list_active_cooldowns

- **Description:** List currently active cooldowns.
- **Inputs:** none
- **Output:** `list[Cooldown]` — see docs/API.md#Cooldown
- **farm_api function:** `infra.farm_api.cooldowns.list_active_cooldowns`

NOTE: Currently returns `[]` — pipeline cooldown state is not yet
exposed. See docs/API.md#Cooldown for details.

### list_known_people

- **Description:** List known people identities.
- **Inputs:** none
- **Output:** `list[PersonIdentity]` — see docs/API.md#PersonIdentity
- **farm_api function:** `infra.farm_api.identities.list_known_people`

### list_known_vehicles

- **Description:** List known vehicle identities.
- **Inputs:** none
- **Output:** `list[VehicleIdentity]` — see docs/API.md#VehicleIdentity
- **farm_api function:** `infra.farm_api.identities.list_known_vehicles`

### list_known_animals

- **Description:** List known animal identities.
- **Inputs:** none
- **Output:** `list[AnimalIdentity]` — see docs/API.md#AnimalIdentity
- **farm_api function:** `infra.farm_api.identities.list_known_animals`

### get_listener_status

- **Description:** Get the listener daemon status (PID, uptime, mode).
- **Inputs:** none
- **Output:** `ListenerStatus` — see docs/API.md#ListenerStatus
- **farm_api function:** `infra.farm_api.listener.get_listener_status`

### get_recent_errors

- **Description:** Get recent ERROR lines from the daemon log.
- **Inputs:**
  - `n` (integer, optional): Number of errors to return (default 20)
- **Output:** `list[ErrorRecord]` — see docs/API.md#ErrorRecord
- **farm_api function:** `infra.farm_api.listener.get_recent_errors`

### get_vision_status

- **Description:** Get the vision analyzer subsystem status.
- **Inputs:** none
- **Output:** `VisionStatus` — see docs/API.md#VisionStatus
- **farm_api function:** `infra.farm_api.vision.get_vision_status`

NOTE: Runtime fields (queue_depth, latency, calls_last_24h) are
currently `null` because the vision analyzer does not expose counters.

### get_config

- **Description:** Get operational config: env vars, documented/missing knobs.
- **Inputs:** none
- **Output:** `ConfigView` — see docs/API.md#ConfigView
- **farm_api function:** `infra.farm_api.config.get_config`

## Multi-box discovery

Every tool response includes a `box_id` field (defaults to
`$(hostname)`). This allows a multi-box outer agent to namespace
responses from several farm-surveillance boxes without ambiguity.

The outer agent is responsible for discovering boxes and maintaining
a mapping of `box_id` to network address. This is configured in the
outer-loop repo, not in this server.

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `FARMSURV_MCP_ENABLED` | `0` | `1`=ON, `0`=OFF (default). Server does not start when 0. |
| `FARMSURV_MCP_BIND` | `tailnet-only` | `tailnet-only` (100.64.0.1), `all` (0.0.0.0), or specific IP |
| `FARMSURV_MCP_TOKEN` | (required) | Bearer token, 32+ chars. Server aborts if missing or weak. |
| `FARMSURV_MCP_PORT` | `8091` | TCP port to listen on |
| `FARMSURV_BOX_ID` | `$(hostname)` | Box identifier in MCP responses |

All knobs declared in `.env.example`.

## Binding security

`tailnet-only` (default) binds to `100.64.0.1` (Tailscale CGNAT range).
The server is unreachable from the local LAN unless the operator
explicitly sets `FARMSURV_MCP_BIND=all`, which logs a loud warning at
startup.

TLS termination is the operator's responsibility if exposing beyond
Tailscale.

## Design references

- **Original design rationale:** `gate-alert/docs/original-design-discussion.md`
  (outer-loop agent + inner-loop harness split, MCP as portable tool
  surface, Hermes as workshop not product)
- **PRD:** `docs/PHASE-V2-068-PRD-mcp-server-and-event-records.json`
- **API contract:** `docs/API.md`

## Do not call from outer agent

The MCP server exposes read-only system state. The following actions
are out of scope for V2-068 and MUST NOT be done:

- **Call the live pipeline.** The MCP server reads from completed
  events on disk. It does NOT invoke `pipeline.run()` or trigger
  motion detection. Doing so would re-alert and is not supported.
- **Mutate files.** No tool writes to the filesystem (frames, events,
  suppression rules, identities). Write tools ship in V2-069.
- **Restart the listener.** The server reports listener status via
  `get_listener_status()` but does not control the process.
  Restarting is done via launchctl (operator tool).
- **Bypass the write-tool guardrail.** The read-only contract is a
  safety boundary until V2-069 ships write tools. Do not extend the
  MCP server to add mutations without operator approval.
- **Inject secrets into tool responses.** All responses are serialized
  through `asdict()` + `box_id` append. The MCP layer must not
  add, remove, or alter redacted fields.

## Write tools (future: V2-069)

The next PRD (V2-069) adds write-capable tools: mute camera, enroll
identity, set cooldown, restart listener. These will be gated behind
an explicit write-tool permit and will not be part of V2-068.
