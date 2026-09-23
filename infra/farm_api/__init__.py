"""farm_api — Read-only system API for the farm-surveillance pipeline.

STATUS: alpha — V2-068, one resource group per module.
THREAD SAFETY: read-only; safe for concurrent access.

RESOURCE GROUPS:
    cameras      — CameraInfo, CameraHealth, list_cameras, get_camera_health, get_camera_snapshot
    events       — EventRecord re-export, list_events, get_event
    gate         — GateConfig, SuppressionRule, get_gate_config, list_suppression_rules
    cooldowns    — Cooldown, list_active_cooldowns
    identities   — PersonIdentity, VehicleIdentity, AnimalIdentity, list_*
    listener     — ListenerStatus, ErrorRecord, get_listener_status, get_recent_errors
    telegram     — TelegramStatus, get_telegram_status
    vision       — VisionStatus, get_vision_status
    config       — ConfigView, get_config

USAGE:
    from infra.farm_api.cameras import list_cameras, get_camera_health

All functions are pure or read from well-known filesystem paths.
No transport awareness — the MCP server is the only consumer today.
"""

from infra.farm_api import (
    cameras,  # noqa: F401 — exposes camera resources
    gate,  # noqa: F401 — exposes gate resources
    listener,  # noqa: F401 — exposes listener status
    vision,  # noqa: F401 — exposes vision status
)
