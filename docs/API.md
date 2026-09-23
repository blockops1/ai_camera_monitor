# Farm Surveillance v2 — System API

Stable read-only API for the farm-surveillance alert pipeline. Built in V2-068.

All types are frozen dataclasses. All functions are read-only. Transport
independent — today the only consumer is the MCP server, but any caller
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

Freshness: filesystem scan — reflects last frame written to disk. Not
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
completes. Stale-by-design — the API reads only completed events, never
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
via a public accessor. This is a documented TODO — write tools for
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
queue_depth: int | None     # None — no exposed queue
last_call_latency_ms: float | None  # None — no latency tracking
calls_last_24h: int | None  # None — no call counter
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
| Config secrets | Name matches /token|password|key|secret/i | `[REDACTED]` |

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
# No transport awareness — no HTTP, no serialization, no MCP.

cameras = list_cameras()
events = list_events(camera_id="FRONT", limit=10)
status = get_listener_status()
config = get_config()
```

## Dependencies

- `infra/farm_api.py` — module-level imports for all resource groups
- `infra/camera_creds.py` — camera inventory (cameras module)
- `infra/event_record.py` — EventRecord dataclass + write (events module)
- `infra/paths.py` — DATA_DIR, FRAMES_DIR, LOGS_DIR, known-file paths
- `infra/vision_analyzer.py` — env constants (vision module)
- `logs/daemon.log` — listener status, errors (listener module)

## Next extension

Write tools (mute camera, enroll identity, set cooldown, restart
listener) ship in V2-069.
