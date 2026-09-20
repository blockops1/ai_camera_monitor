"""
infra -- farm-surveillance-v2 shared infrastructure modules.
"""

from __future__ import annotations

# alert_overrides_baseline exports
from infra.alert_overrides_baseline import _apply_distant_vehicle_baseline_override

# time_of_day exports
from infra.quiet_hours import in_quiet_hours
from infra.time_of_day import get_night_window, is_night_at_edt

__all__ = [
    "_apply_distant_vehicle_baseline_override",
    "get_night_window",
    "in_quiet_hours",
    "is_night_at_edt",
]
