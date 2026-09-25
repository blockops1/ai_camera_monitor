"""
alert_overrides_baseline.py -- per-camera distant-vehicle baseline override.

Third layer of defense: the vision LLM (Qwen3-VL-8B) sometimes writes
'faint', 'distant', 'light source', 'moving lights', 'reflection', 'glare',
'headlight' in its scene_description for what is actually a car driving at
night with its headlights blooming. When this happens off-hours + camera in
baseline set, demote the alert from level 1 (vehicle) to level 0 (baseline
vehicle).

STATUS: new (US-062e)
THREAD SAFETY: single-threaded, no shared mutable state
IMPORT ORDER: stdlib -> third-party -> local (infra.)

INPUTS:
    - Env vars: FARMSURV_BASELINE_OVERRIDE_ENABLED (kill switch),
      FARMSURV_BASELINE_OVERRIDE_CAMERAS (comma-separated camera list),
      FARMSURV_DISTANT_KEYWORDS (comma-separated keyword list)
    - alert dict (with 'threat_level' key)
    - vision_result dict (with 'top_class' and 'scene_description')
    - camera_name (str)

OUTPUTS:
    - alert dict (possibly modified: threat_level demoted 1->0)

DOES NOT DO:
    - Hardcoded keyword list or hour constants
    - Fallback / try-except-pass / compat shims
    - Reading from config files or databases

CALLED BY:
    - listener/pipeline.py (after TG#1/TG#2/TG#3 dispatch decision)
"""

from __future__ import annotations

import os

from infra.env_required import _require_env_lower_bool
from infra.quick_classifier import VEHICLE_CLASSES

# ---------------------------------------------------------------------------
# Env-var defaults
# ---------------------------------------------------------------------------

# Kill switch: no default -- operator must set explicitly.

# Comma-separated list of camera names the override applies to.
# If empty, the override matches no camera (effectively disabled).
_BASELINE_OVERRIDE_CAMERAS_DEFAULT = ""

# Comma-separated list of keywords in scene_description that indicate
# a distant/weak sighting. If empty, no demotions ever occur.
# These are defaults that operators should tune per their site.
_DISTANT_KEYWORDS_DEFAULT = ""

# Vehicle classes eligible for demotion (subset of KEEP_CLASSES).
# Non-vehicle classes (persons, animals) are excluded -- person safety wins
# over baseline override regardless of distance keyword.
_VEHICLE_DEMOTE_CLASSES = VEHICLE_CLASSES


def _parse_cameras(raw: str) -> frozenset[str]:
    """Parse a comma-separated camera list into a frozenset for fast lookup."""
    if not raw:
        return frozenset()
    return frozenset(cam.strip() for cam in raw.split(",") if cam.strip())


def _parse_keywords(raw: str) -> frozenset[str]:
    """Parse a comma-separated keyword list into a frozenset for lookup."""
    if not raw:
        return frozenset()
    return frozenset(kw.strip().lower() for kw in raw.split(",") if kw.strip())


def _apply_distant_vehicle_baseline_override(
    alert: dict,
    vision_result: dict,
    camera_name: str,
    dt,  # datetime -- accepted for signature compatibility but not used here
) -> dict:
    """Apply the distant-vehicle baseline override to an alert.

    Demotes threat_level from 1 to 0 when ALL of the following hold:
    1. FARMSURV_BASELINE_OVERRIDE_ENABLED is truthy (kill switch ON).
    2. *camera_name* is in FARMSURV_BASELINE_OVERRIDE_CAMERAS.
    3. vision_result['top_class'] is a vehicle class (NOT person/animal).
    4. alert['threat_level'] == 1 (L1 only; L2+ preserved).
    5. vision_result['scene_description'] contains a distant keyword.

    Args:
        alert: Mutable dict with 'threat_level' key. May be modified in-place.
        vision_result: Dict with 'top_class' (str) and
            'scene_description' (str).
        camera_name: The camera identifier to check against the env list.
        dt: UTC datetime (unused -- signature compatibility with pipeline).

    Returns:
        The *alert* dict (possibly modified with threat_level demoted).
    """
    # AC4: kill switch -- fail-loud if operator hasn't set it.
    if not _require_env_lower_bool("FARMSURV_BASELINE_OVERRIDE_ENABLED"):
        return alert

    # Camera scoping: only cameras in the env list are gated.
    cameras = _parse_cameras(
        os.environ.get(
            "FARMSURV_BASELINE_OVERRIDE_CAMERAS", _BASELINE_OVERRIDE_CAMERAS_DEFAULT
        )
    )
    if camera_name not in cameras:
        return alert

    # Vehicle check: must be a vehicle, NOT a person or animal.
    top_class = vision_result.get("top_class", "")
    if top_class not in _VEHICLE_DEMOTE_CLASSES:
        return alert

    # Threat level check: only demote L1, preserve L2+.
    if alert.get("threat_level", 0) != 1:
        return alert

    # Scene description keyword check.
    keywords_raw = os.environ.get(
        "FARMSURV_DISTANT_KEYWORDS", _DISTANT_KEYWORDS_DEFAULT
    )
    keywords = _parse_keywords(keywords_raw)
    if not keywords:
        return alert

    scene = vision_result.get("scene_description", "").lower()
    if not any(kw in scene for kw in keywords):
        return alert

    # All conditions met: demote L1 -> L0.
    alert["threat_level"] = 0
    return alert


if __name__ == "__main__":
    # Quick smoke test.
    import importlib
    import sys

    mod = importlib.reload(sys.modules[__name__])

    # Set env for smoke.
    os.environ["FARMSURV_BASELINE_OVERRIDE_ENABLED"] = "1"
    os.environ["FARMSURV_BASELINE_OVERRIDE_CAMERAS"] = "OUTSIDE_FRONT_SOLAR"
    os.environ["FARMSURV_DISTANT_KEYWORDS"] = "faint,distant,headlight"

    alert = {"threat_level": 1}
    vr = {"top_class": "car", "scene_description": "faint car with distant headlight"}
    result = mod._apply_distant_vehicle_baseline_override(alert, vr, "OUTSIDE_FRONT_SOLAR", None)
    print(f"Demote L1 car with distant keyword: {result['threat_level']}")  # expect 0

    # Person should NOT be demoted.
    alert2 = {"threat_level": 1}
    vr2 = {"top_class": "person", "scene_description": "faint person with distant headlight"}
    result2 = mod._apply_distant_vehicle_baseline_override(alert2, vr2, "OUTSIDE_FRONT_SOLAR", None)
    print(f"Person preserved: {result2['threat_level']}")  # expect 1
