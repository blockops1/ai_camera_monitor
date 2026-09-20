"""
test_pipeline_baseline_override.py -- integration tests for the three-layer
night-time FP suppression pipeline (quiet hours + baseline override).

STATUS: new (US-062f)
THREAD SAFETY: single-threaded, no shared state — safe.
"""

# ── imports -----------------------------------------------------------------

import importlib
import sys
from datetime import UTC, datetime
from unittest.mock import patch

# ── Helpers -----------------------------------------------------------------

def _make_alert(**kwargs):
    """Build a minimal alert dict for pipeline stages."""
    base = {
        "alert_id": "test-001",
        "camera_id": "cam-backyard",
        "threat_level": 1,
        "timestamp": "2026-09-20T02:30:00",
        "location": {"x": 500, "y": 300, "w": 60, "h": 120},
    }
    base.update(kwargs)
    return base


def _make_vision_result(**kwargs):
    """Build a minimal VM1 result dict."""
    base = {
        "top_class": "car",  # must be in infra.quick_classifier.VEHICLE_CLASSES
        "top_conf": 0.78,
        "scene_description": "distant vehicle far away on road",
        "object_count": 1,
    }
    base.update(kwargs)
    return base


def _reload_modules():
    """Reload infra modules so env vars take effect."""
    for mod_name in (
        "infra.quiet_hours",
        "infra.time_of_day",
        "infra.alert_overrides_baseline",
        "listener.pipeline",
    ):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])


# ── Test 1: stage_apply_baseline_override demotes threat_level for distant vehicle

def test_stage_applies_baseline_demotion():
    """AC6(a): stage_apply_baseline_override demotes threat_level 1→0 for distant vehicles."""
    # Set required env vars for the baseline override to activate.
    with patch.dict("os.environ", {
        "FARMSURV_BASELINE_OVERRIDE_ENABLED": "1",
        "FARMSURV_BASELINE_OVERRIDE_CAMERAS": "cam-backyard",
        "FARMSURV_DISTANT_KEYWORDS": "distant, far, away, horizon",
    }):
        _reload_modules()
        from listener.pipeline import stage_apply_baseline_override

        alert = _make_alert(threat_level=1)
        vision = _make_vision_result(
            top_class="car",  # must be in VEHICLE_CLASSES for demotion
            scene_description="distant vehicle far away on road",
        )

        dt = datetime.now(UTC)
        result = stage_apply_baseline_override(alert, vision, "cam-backyard", dt)

        assert result["threat_level"] == 0, (
            f"Expected threat_level 0 for distant vehicle, got {result['threat_level']}"
        )


def test_stage_no_demotion_for_non_distant():
    """AC6(b): Non-distant vehicles are not demoted (threat_level stays 1)."""
    with patch.dict("os.environ", {
        "FARMSURV_BASELINE_OVERRIDE_ENABLED": "1",
        "FARMSURV_BASELINE_OVERRIDE_CAMERAS": "cam-backyard",
        "FARMSURV_DISTANT_KEYWORDS": "distant, far, away, horizon",
    }):
        _reload_modules()
        from listener.pipeline import stage_apply_baseline_override

        alert = _make_alert(threat_level=1)
        vision = _make_vision_result(
            top_class="car",
            scene_description="motorcycle close up",
        )

        dt = datetime.now(UTC)
        result = stage_apply_baseline_override(alert, vision, "cam-backyard", dt)

        assert result["threat_level"] == 1, (
            f"Expected threat_level 1 for non-distant vehicle, got {result['threat_level']}"
        )


# ── Test 2: quiet hours gate + baseline override together

def test_quiet_hours_suppresses_alert_on_scoped_camera():
    """AC6(c): in_quiet_hours gate suppresses alerts during off-hours on scoped cameras."""
    # Set env vars for quiet hours
    with patch.dict("os.environ", {
        "FARMSURV_QUIET_HOURS_ENABLED": "1",
        "FARMSURV_QUIET_HOURS_CAMERAS": "cam-backyard",
        "FARMSURV_FARM_LAT": "34.5782",
        "FARMSURV_FARM_LON": "-84.9438",
        "FARMSURV_NIGHT_TWILIGHT_BUFFER_MIN": "30",
    }):
        _reload_modules()
        from infra.quiet_hours import in_quiet_hours

        # 02:30 AM is definitely during off-hours
        dt = datetime(2026, 9, 20, 2, 30, tzinfo=UTC)
        assert in_quiet_hours(dt, "cam-backyard") is True

        # 14:00 (2 PM) is during on-hours
        dt_day = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
        assert in_quiet_hours(dt_day, "cam-backyard") is False


def test_quiet_hours_allows_alert_on_unscoped_camera():
    """Unscoped cameras are NOT suppressed during off-hours."""
    with patch.dict("os.environ", {
        "FARMSURV_QUIET_HOURS_ENABLED": "1",
        "FARMSURV_QUIET_HOURS_CAMERAS": "cam-front",
        "FARMSURV_FARM_LAT": "34.5782",
        "FARMSURV_FARM_LON": "-84.9438",
        "FARMSURV_NIGHT_TWILIGHT_BUFFER_MIN": "30",
    }):
        _reload_modules()
        from infra.quiet_hours import in_quiet_hours

        dt = datetime(2026, 9, 20, 2, 30, tzinfo=UTC)
        # cam-backyard is NOT in the scoped list → should NOT be suppressed
        assert in_quiet_hours(dt, "cam-backyard") is False


# ── Test 3: baseline override env toggle

def test_baseline_override_disabled_when_env_is_zero():
    """When FARMSURV_BASELINE_OVERRIDE_ENABLED=0, threat_level is NOT demoted."""
    with patch.dict("os.environ", {
        "FARMSURV_BASELINE_OVERRIDE_ENABLED": "0",
    }):
        _reload_modules()
        from listener.pipeline import stage_apply_baseline_override

        alert = _make_alert(threat_level=1)
        vision = _make_vision_result(
            top_class="car",
            scene_description="distant vehicle far away on road",
        )

        dt = datetime.now(UTC)
        result = stage_apply_baseline_override(alert, vision, "cam-backyard", dt)

        # When override is disabled, threat_level stays at original value
        assert result["threat_level"] == 1


# ── Test 4: _apply_distant_vehicle_baseline_override module function

def test_module_applies_demotion():
    """The underlying module function demotes threat_level correctly."""
    with patch.dict("os.environ", {
        "FARMSURV_BASELINE_OVERRIDE_ENABLED": "1",
        "FARMSURV_BASELINE_OVERRIDE_CAMERAS": "cam-backyard",
        "FARMSURV_DISTANT_KEYWORDS": "distant, far, away, horizon",
    }):
        _reload_modules()
        from infra.alert_overrides_baseline import (
            _apply_distant_vehicle_baseline_override,
        )

        alert = _make_alert(threat_level=1)
        vision = _make_vision_result(
            top_class="car",
            scene_description="distant vehicle far away on road",
        )

        dt = datetime.now(UTC)
        result = _apply_distant_vehicle_baseline_override(alert, vision, "cam-backyard", dt)

        assert result["threat_level"] == 0
