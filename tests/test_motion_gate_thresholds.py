"""Regression test: per-camera subject allowlist includes vehicle + bicycle.

US-073b: every camera with a restrictive subjects allowlist must include
"vehicle" and "bicycle" so YOLO detections (truck, car, motorcycle, etc.)
are not silently dropped before the alert is constructed.

When a camera has no subjects key, it falls through to the global
KEEP_CLASSES_DEFAULT — which already includes vehicle classes. Those
cameras are OK as-is and are tested by the "fallthrough OK" branch.
"""

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "motion_gate_thresholds.json"

# Camera profiles for threshold assignment.
_SOLAR = {"OUTSIDE_FRONT_SOLAR", "OUTSIDE_BACK_SOLAR", "OUTSIDE_FRONT_POWER"}
_FRONT_BACK = {"OUTSIDE_FRONT_GARAGE", "FRONT", "BACK"}


def _load_config() -> dict:
    with CONFIG_PATH.open() as f:
        raw = json.load(f)
    # Strip metadata keys (prefixed with "_").
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _camera_profile(name: str) -> str:
    """Return 'solar' or 'front_back' for a given camera name."""
    if name in _SOLAR:
        return "solar"
    if name in _FRONT_BACK:
        return "front_back"
    return "unknown"


class TestMotionGateThresholds:
    """Validate every camera in motion_gate_thresholds.json."""

    def test_all_cameras_have_vehicle_accessible(self):
        """Every camera either has 'vehicle' in subjects or no subjects key."""
        cfg = _load_config()
        for camera, cam_cfg in cfg.items():
            subjects = cam_cfg.get("subjects")
            if subjects is None:
                # No subjects key — falls through to global default (OK).
                continue
            assert (
                "vehicle" in subjects
            ), f"{camera}: subjects {subjects} must include 'vehicle'"

    def test_all_cameras_have_bicycle_accessible(self):
        """Every camera either has 'bicycle' in subjects or no subjects key."""
        cfg = _load_config()
        for camera, cam_cfg in cfg.items():
            subjects = cam_cfg.get("subjects")
            if subjects is None:
                continue
            assert (
                "bicycle" in subjects
            ), f"{camera}: subjects {subjects} must include 'bicycle'"

    def test_vehicle_threshold_in_range_when_populated(self):
        """When a camera has an explicit vehicle threshold, it must be in
        [0.30, 0.80] — the full valid range per the config validator."""
        cfg = _load_config()
        for camera, cam_cfg in cfg.items():
            if "vehicle" not in cam_cfg:
                # Falls back to THRESHOLDS_BY_CLASS default (0.50) — OK.
                continue
            threshold = cam_cfg["vehicle"]
            assert isinstance(threshold, (int, float)), (
                f"{camera}: vehicle must be a number, got {type(threshold)}"
            )
            assert 0.30 <= threshold <= 0.80, (
                f"{camera}: vehicle threshold {threshold} not in [0.30, 0.80]"
            )

    def test_solar_vehicles_at_055(self):
        """SOLAR cameras should have vehicle at 0.55."""
        cfg = _load_config()
        for camera in _SOLAR:
            if camera in cfg:
                assert cfg[camera]["vehicle"] == 0.55, (
                    f"{camera}: solar vehicle should be 0.55, got {cfg[camera]['vehicle']}"
                )

    def test_front_back_vehicles_at_045(self):
        """Front/back cameras should have vehicle at 0.45."""
        cfg = _load_config()
        for camera in _FRONT_BACK:
            if camera in cfg:
                assert cfg[camera]["vehicle"] == 0.45, (
                    f"{camera}: front/back vehicle should be 0.45, got {cfg[camera]['vehicle']}"
                )

    def test_config_is_valid_json(self):
        """Basic sanity: the file parses as valid JSON."""
        with CONFIG_PATH.open() as f:
            data = json.load(f)
        assert isinstance(data, dict), "Config must be a top-level object"
        assert len(data) > 0, "Config must contain at least one camera"
