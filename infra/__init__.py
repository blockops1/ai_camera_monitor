"""
infra -- farm-surveillance-v2 shared infrastructure modules.
"""

from __future__ import annotations

# alert_overrides_baseline exports
from infra.alert_overrides_baseline import _apply_distant_vehicle_baseline_override

# time_of_day exports
from infra.quiet_hours import in_quiet_hours
from infra.reolink import (
    AudioConfig,
    CapabilitiesResult,
    DeviceInfo,
    DeviceListEntry,
    EncodeConfig,
    EventEntry,
    ImageConfig,
    LightConfig,
    OsdConfig,
    PingResult,
    PrivacyMask,
    PTZAutotrack,
    PTZGuard,
    PTZPatrol,
    PTZPreset,
    RecipeResult,
    RecordSchedule,
    ReolinkAuthError,
    ReolinkClient,
    ReolinkCommandError,
    ReolinkTimeoutError,
    UserEntry,
    VodEntry,
    apply_recipe,
    verify_recipe,
)
from infra.time_of_day import get_night_window, is_night_at_edt

__all__ = [
    "AudioConfig",
    "CapabilitiesResult",
    "DeviceInfo",
    "DeviceListEntry",
    "EncodeConfig",
    "EventEntry",
    "ImageConfig",
    "LightConfig",
    "OsdConfig",
    "PTZAutotrack",
    "PTZGuard",
    "PTZPatrol",
    "PTZPreset",
    "PingResult",
    "PrivacyMask",
    "RecipeResult",
    "RecordSchedule",
    "ReolinkAuthError",
    "ReolinkClient",
    "ReolinkCommandError",
    "ReolinkTimeoutError",
    "UserEntry",
    "VodEntry",
    "_apply_distant_vehicle_baseline_override",
    "apply_recipe",
    "get_night_window",
    "in_quiet_hours",
    "is_night_at_edt",
    "verify_recipe",
]
