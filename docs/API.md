# Farm Surveillance v2 -- System API

## HTTP API (cross-process)

V2-072 adds a parallel HTTP API surface to the existing MCP server. Operators
integrate via curl, n8n, scripts, or Grafana HTTP sensor. The HTTP API mirrors
the MCP tools 1:1 -- when MCP gets a new tool, the API gets a new route. Both
back to the same `infra.farm_api.*` typed Python contract.

### Routes

All API routes are GET-only. Every response wraps data in a JSON object with a
`box_id` field (the local hostname). Auth is required for `/api/*` routes
(see Auth model below); `/healthz` requires no auth.

| Path | Query params | Returns | Example curl |
|---|---|---|---|
| `GET /healthz` | -- | `ok`, `box_id`, `uptime_seconds`, `api_enabled` | `curl http://100.x.x.x:8092/healthz` |
| `GET /api/v1/cameras` | -- | list of CameraInfo objects | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/cameras` |
| `GET /api/v1/cameras/{id}/health` | -- | CameraHealth object or 404 | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/cameras/FRONT/health` |
| `GET /api/v1/cameras/{id}/snapshot` | -- | snapshot file path or 404 | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/cameras/FRONT/snapshot` |
| `GET /api/v1/events` | `limit=50`, `since=ISO8601` | list of EventRecord objects | `curl -H 'Authorization: Bearer $TOKEN' 'http://100.x.x.x:8092/api/v1/events?limit=10'` |
| `GET /api/v1/events/{id}` | -- | EventRecord or 404 | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/events/abc123` |
| `GET /api/v1/gate` | -- | GateConfig object | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/gate` |
| `GET /api/v1/gate/suppression-rules` | -- | list of SuppressionRule | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/gate/suppression-rules` |
| `GET /api/v1/cooldowns` | -- | list of Cooldown (currently []) | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/cooldowns` |
| `GET /api/v1/identities/people` | -- | list of PersonIdentity | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/identities/people` |
| `GET /api/v1/identities/vehicles` | -- | list of VehicleIdentity | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/identities/vehicles` |
| `GET /api/v1/identities/animals` | -- | list of AnimalIdentity | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/identities/animals` |
| `GET /api/v1/listener/status` | -- | ListenerStatus object | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/listener/status` |
| `GET /api/v1/listener/errors` | `limit=50`, `since=ISO8601` | list of ErrorRecord | `curl -H 'Authorization: Bearer $TOKEN' 'http://100.x.x.x:8092/api/v1/listener/errors?limit=20'` |
| `GET /api/v1/telegram/status` | -- | TelegramStatus object | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/telegram/status` |
| `GET /api/v1/vision/status` | -- | VisionStatus object | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/vision/status` |
| `GET /api/v1/config` | -- | ConfigView object | `curl -H 'Authorization: Bearer $TOKEN' http://100.x.x.x:8092/api/v1/config` |

### Auth model

- **Token env:** `FARMSURV_API_TOKEN` (32+ chars). Generate with:
  `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`
- **Startup abort:** server refuses to start if the token is missing or too
  short. Error is printed to stderr and the process exits with code 1.
- **Per-request auth:** every `/api/*` route requires
  `Authorization: Bearer <token>` header. Missing or wrong token returns
  HTTP 401 with `{"error": "Unauthorized: missing or invalid bearer token"}`.
- **Constant-time comparison:** the token check uses a constant-time string
  comparison to prevent timing attacks.
- **/healthz bypass:** the health endpoint requires no auth (it is useful
  before the operator has the token configured).

### Bind model

Default: Tailscale-only. The server binds to the Tailscale CGNAT interface
(100.64.0.1 by default). Configured via:

- **`FARMSURV_API_BIND`** (default: `tailnet-only`)
  - `tailnet-only` -- binds to the Tailscale CGNAT address (100.64.0.1). This
    is the default and recommended mode. The API is reachable only from peers
    on the same Tailscale network.
  - `all` -- binds to `0.0.0.0` (all interfaces). Only use this on a trusted
    network and with a strong token. A warning is logged at startup.
  - `<interface>` -- binds to a specific interface name or IP. The operator
    must ensure the interface exists. A warning is logged at startup.

The bind resolution is factored into `infra.net_bind.resolve_bind_address()`
(US-072a), shared with the MCP server.

### How to enable

1. Generate a token:
   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```
2. Add to `~/.env`:
   ```
   FARMSURV_API_ENABLED=1
   FARMSURV_API_TOKEN=<the-token>
   ```
3. Restart the listener (the daemon thread picks up the new env on boot):
   ```bash
   launchctl kickstart -k gui/501/com.farm.surveillance.v2
   ```
4. Verify:
   ```bash
   curl http://100.x.x.x:8092/healthz
   curl -H 'Authorization: Bearer <token>' http://100.x.x.x:8092/api/v1/cameras
   ```

**Standalone mode:** the API server can also run as a standalone process:
`python3 -m infra.api_server`. Useful for debugging.

### Deployment modes

```
single-host:     [listener + API + MCP] -- all on one machine, one port each
split-api:       [listener + API] on host A, [MCP] on host B -- API on :8092, MCP on :8091
split-mcp:       [listener + MCP] on host A, [API] on host B -- same idea, reversed
full-split:      [listener] on A, [API] on B, [MCP] on C -- fully decoupled
```

| Mode | Listener | API server | MCP server | Use case |
|---|---|---|---|---|
| single-host | :8090 | :8092 | :8091 | Local dev, single Mac |
| split-api | :8090 (A) | :8092 (B) | :8091 (A) | Remote Grafana on B |
| split-mcp | :8090 (A) | :8092 (A) | :8091 (B) | Remote MCP client on B |
| full-split | :8090 (A) | :8092 (B) | :8091 (C) | Maximum isolation |

### Cross-references

- **Routes mirror MCP tools 1:1.** When MCP gets a new tool, API gets a new
  route. Both back to the same `infra.farm_api.*` typed Python contract.
- See `docs/MCP-API.md` for the MCP server surface (US-068c).
- See `docs/PHASE-V2-068-PRD-mcp-server-and-event-records.json` (V2-068 PRD).
- See `docs/PHASE-V2-072-PRD-http-api-server.json` (V2-072 PRD).
- See `infra/farm_api/` for the underlying typed Python contract.

--

# Farm Surveillance v2 -- System API

Stable read-only API for the farm-surveillance alert pipeline. Built in V2-068.

All types are frozen dataclasses. All functions are read-only. Transport
independent -- today the only consumer is the MCP server, but any caller
may import directly.

## Resource groups

| Module | Functions | Description |
|---|---|---|
| `infra.farm_api.cameras` | `list_cameras`, `get_camera_health`, `get_camera_snapshot` | Camera inventory, health, latest frame |
| `infra.farm_api.events` | `list_events`, `get_event` | Per-alert event records (event.json) |
| `infra.farm_api.gate` | `get_gate_config`, `list_suppression_rules` | Motion gate env config + suppression rules |
| `infra.farm_api.cooldowns` | `list_active_cooldowns` | Active cooldown entries (stub, returns []) |
| `infra.farm_api.identities` | `list_known_people`, `list_known_vehicles`, `list_known_animals` | Known people/vehicles/animals |
| `infra.farm_api.listener` | `get_listener_status`, `get_recent_errors` | Listener PID, uptime, recent log errors |
| `infra.farm_api.vision` | `get_vision_status` | Vision analyzer env config (runtime fields None) |
| `infra.farm_api.config` | `get_config` | Operational env knobs, redacted |

## Data types

### CameraInfo (cameras.py:61)

```
camera_id: str              # Uppercase prefix, e.g. "FRONT", "BACK"
display_name: str           # Human-readable, e.g. "Front Door Outside"
model: str                  # e.g. "Reolink RLC-510A", or ""
rtsp_url_redacted: str      # RTSP URL with 192.168.x.x replaced
ip_redacted: str            # Camera IP, redacted
```

Example:

```json
{
  "camera_id": "FRONT",
  "display_name": "Front Door",
  "model": "Reolink RLC-510A",
  "rtsp_url_redacted": "rtsp://[REDACTED-LAN-IP]:554/h264",
  "ip_redacted": "[REDACTED-LAN-IP]"
}
```

### CameraHealth (cameras.py:78)

```
camera_id: str
online: bool
last_frame_at: str | None   # ISO-8601
rtsp_errors_last_24h: int
```

Freshness: filesystem scan -- reflects last frame written to disk. Not
live RTSP health.

### EventRecord (infra/event_record.py:60)

```
alert_id: str               # UUID
camera_id: str
box_id: str                 # Multi-box namespace (hostname)
started_at: str             # ISO-8601
finished_at: str            # ISO-8601
gate: dict | None
vm1: dict | None
vm2: dict | None
match: dict | None
dropped: str | None         # Drop reason if event was dropped
frame_paths: list[str]
artifact_dir: str
```

Example (dropped alert):

```json
{
  "alert_id": "a1b2c3d4-...",
  "camera_id": "BACK",
  "box_id": "jills-mac-mini",
  "started_at": "2026-09-22T20:51:31+00:00",
  "finished_at": "2026-09-22T20:51:33+00:00",
  "gate": {"class": "dog", "confidence": 0.35},
  "vm1": null,
  "vm2": null,
  "match": null,
  "dropped": "night_suppress",
  "frame_paths": ["data/frames/BACK/a1b2.../frame_001.png"],
  "artifact_dir": "data/frames/BACK/a1b2..."
}
```

Freshness: event.json written atomically per alert after pipeline.run()
completes. Stale-by-design -- the API reads only completed events, never
in-flight pipeline state.

### GateConfig (gate.py:45)

```
motion_threshold: float     # Default 0.50, env: MOTION_GATE_CONFIDENCE_THRESHOLD
night_suppress_enabled: bool # env: MOTION_GATE_NIGHT_SUPPRESS_ENABLED (0|1)
global_change_max_pct: float # env: FARMSURV_GLOBAL_CHANGE_MAX_PCT (default 60)
v2_enabled: bool            # env: MOTION_GATE_V2 (1|0)
```

Freshness: read from os.environ at call time. Changes take effect on
next API call (no caching).

### SuppressionRule (gate.py:66)

```
camera_id: str
class_name: str
reason: str
expires_at: str | None      # ISO-8601, None = no expiry
```

Read from data/gate_suppression/rules.jsonl. Expired rules silently
excluded.

### Cooldown (cooldowns.py:41)

```
camera_id: str
class_name: str
expires_at: str | None      # ISO-8601
set_by: str                 # "pipeline"
reason: str
```

NOTE: `list_active_cooldowns()` currently returns `[]` because pipeline
cooldown state (`infra.pipeline_cooldown._state`) is not yet exposed
via a public accessor. This is a documented TODO -- write tools for
setting cooldowns ship in V2-069.

### PersonIdentity (identities.py:50)

```
name: str
enrolled_at: str            # ISO-8601
last_seen_at: str | None    # ISO-8601
photo_count: int
```

### VehicleIdentity (identities.py:67)

```
label: str
make: str
model: str
color: str
owner: str
enrolled_at: str            # ISO-8601
```

### AnimalIdentity (identities.py:88)

```
species: str
name: str
enrolled_at: str            # ISO-8601
```

### ListenerStatus (listener.py:47)

```
pid: int | None
uptime_seconds: float       # 0 if PID not found
mode: str                   # "running" or "stopped"
started_at: str | None      # ISO-8601
last_event_at: str | None   # ISO-8601
```

Freshness: PID from `launchctl list` (live). Uptime and started_at
from logs/daemon.log (read at call time, not cached).

### ErrorRecord (listener.py:70)

```
timestamp: str              # ISO-8601
severity: str               # Always "ERROR"
message: str
source: str                 # Python module name
```

`get_recent_errors(n=20)` returns the 20 most recent ERROR lines from
daemon.log, newest first.

### VisionStatus (vision.py:56)

```
model_name: str | None      # e.g. from vision_analyzer._VISION_MODEL
base_url_redacted: str | None
queue_depth: int | None     # None -- no exposed queue
last_call_latency_ms: float | None  # None -- no latency tracking
calls_last_24h: int | None  # None -- no call counter
```

RUNTIME GAP (documented 2026-09-22): vision_analyzer.py exposes no
runtime state. Fields that require runtime data return None. If the
analyzer is extended to expose counters (e.g. a stats dict or heartbeat
JSON), `get_vision_status()` can read them without changing the schema.

### ConfigView (config.py:53)

```
env: dict[str, str]         # All ~/.env keys -> redacted values
knobs_documented: list[str] # Knobs in .env.example matching safe prefixes
knobs_set: list[str]        # Subset of documented that are in ~/.env
knobs_missing: list[str]    # Subset of documented that are NOT in ~/.env
```

Known-safe prefixes: GATE_*, FARMSURV_*, MOTION_*, VISION_*, LISTEN_*.
Secret-matching knob names (containing token/password/key/secret) have
their values replaced with `[REDACTED]`.

## Error cases

All functions handle missing files, empty directories, and malformed data
gracefully:

| Failure mode | Behavior |
|---|---|
| Camera not in camera-creds.env | `get_camera_health` returns `None` |
| No frames on disk | `get_camera_snapshot` returns `None` |
| No events written yet | `list_events` returns `[]` |
| Missing event.json | `get_event` returns `None` |
| Malformed JSONL in suppression rules | Log warning, skip line |
| Daemon.log missing | `get_listener_status` returns PID=None, `get_recent_errors` returns `[]` |
| No ~/.env file | `get_config` returns empty env, all knobs missing |
| Malformed event.json | `list_events` skips it with a log warning |

## Redaction rules

| Field | Pattern | Replacement |
|---|---|---|
| Camera IPs | 192.168.x.x | `[REDACTED-LAN-IP]` |
| Vision base_url | any IPv4 | `[REDACTED-IP]` |
| Config secrets | Name matches /token\|password\|key\|secret/i | `[REDACTED]` |

## Usage

```python
from infra.farm_api.cameras import list_cameras
from infra.farm_api.events import list_events, get_event
from infra.farm_api.gate import get_gate_config, list_suppression_rules
from infra.farm_api.cooldowns import list_active_cooldowns
from infra.farm_api.identities import (
    list_known_people,
    list_known_vehicles,
    list_known_animals,
)
from infra.farm_api.listener import get_listener_status, get_recent_errors
from infra.farm_api.vision import get_vision_status
from infra.farm_api.config import get_config

# All functions are pure or read from well-known filesystem paths.
# No transport awareness -- no HTTP, no serialization, no MCP.

cameras = list_cameras()
events = list_events(camera_id="FRONT", limit=10)
status = get_listener_status()
config = get_config()
```

## Dependencies

- `infra/farm_api.py` -- module-level imports for all resource groups
- `infra/camera_creds.py` -- camera inventory (cameras module)
- `infra/event_record.py` -- EventRecord dataclass + write (events module)
- `infra/paths.py` -- DATA_DIR, FRAMES_DIR, LOGS_DIR, known-file paths
- `infra/vision_analyzer.py` -- env constants (vision module)
- `logs/daemon.log` -- listener status, errors (listener module)

## Next extension

Write tools (mute camera, enroll identity, set cooldown, restart
listener) ship in V2-069.
