"""farm_api — System API for the farm-surveillance pipeline.

STATUS: alpha — V2-068, one resource group per module; write path US-106a.
THREAD SAFETY: read operations are read-only; write operations serialize via file I/O.

RESOURCE GROUPS:
    cameras      — CameraInfo, CameraHealth, list_cameras, get_camera_health, get_camera_snapshot
    events       — EventRecord re-export, list_events, get_event
    gate         — GateConfig, SuppressionRule, get_gate_config, list_suppression_rules
    cooldowns    — Cooldown, list_active_cooldowns
    identities   — PersonIdentity, VehicleIdentity, AnimalIdentity, list_*, enroll_known_person
    listener     — ListenerStatus, ErrorRecord, get_listener_status, get_recent_errors
    telegram     — TelegramStatus, get_telegram_status
    vision       — VisionStatus, get_vision_status
    config       — ConfigView, get_config

USAGE:
    from infra.farm_api.cameras import list_cameras, get_camera_health
    from infra.farm_api.identities import list_known_people, enroll_known_person

Transport: MCP server (infra.mcp_server) + REST API (infra.api_server).
"""

__all__ = [
    # identities
    "AnimalIdentity",
    # cameras
    "CameraHealth",
    "CameraInfo",
    # config
    "ConfigView",
    # cooldowns
    "Cooldown",
    # listener
    "ErrorRecord",
    # events
    "EventRecord",
    # gate
    "GateConfig",
    # listener
    "ListenerStatus",
    "PersonIdentity",
    "SuppressionRule",
    # telegram
    "TelegramStatus",
    "VehicleIdentity",
    # vision
    "VisionStatus",
    "enroll_known_person",
    # public functions
    "get_camera_health",
    "get_camera_snapshot",
    "get_config",
    "get_event",
    "get_gate_config",
    "get_listener_status",
    "get_recent_errors",
    "get_telegram_status",
    "get_vision_status",
    "list_active_cooldowns",
    "list_cameras",
    "list_events",
    "list_known_animals",
    "list_known_people",
    "list_known_vehicles",
    "list_suppression_rules",
]

from infra.farm_api import (
    cameras,  # noqa: F401
    config,  # noqa: F401
    gate,  # noqa: F401
    listener,  # noqa: F401
    telegram,  # noqa: F401
    vision,  # noqa: F401
)

# Re-export public symbols from submodules
from infra.farm_api.cameras import (
    CameraHealth,
    CameraInfo,
    get_camera_health,
    get_camera_snapshot,
    list_cameras,
)
from infra.farm_api.config import ConfigView, get_config
from infra.farm_api.cooldowns import Cooldown, list_active_cooldowns
from infra.farm_api.events import EventRecord, get_event, list_events
from infra.farm_api.gate import (
    GateConfig,
    SuppressionRule,
    get_gate_config,
    list_suppression_rules,
)
from infra.farm_api.identities import (
    AnimalIdentity,
    PersonIdentity,
    VehicleIdentity,
    enroll_known_person,
    list_known_animals,
    list_known_people,
    list_known_vehicles,
)
from infra.farm_api.listener import (
    ErrorRecord,
    ListenerStatus,
    get_listener_status,
    get_recent_errors,
)
from infra.farm_api.telegram import TelegramStatus, get_telegram_status
from infra.farm_api.vision import VisionStatus, get_vision_status
