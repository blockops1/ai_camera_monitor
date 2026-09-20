"""
test_alert_overrides_baseline.py -- unit tests for infra.alert_overrides_baseline.

STATUS: new (US-062e)
THREAD SAFETY: single-threaded (pytest fixtures are single-threaded)

MOCKS:
    - None needed -- the module reads only env vars and dicts, no network.

DOES NOT DO:
    - Make live network calls
    - Depend on llama-server or any external service
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_override(enabled=None, cameras=None, keywords=None):
    """Build a dict of env-var overrides for baseline-override tests."""
    env = {}
    if enabled is not None:
        env["FARMSURV_BASELINE_OVERRIDE_ENABLED"] = str(enabled)
    if cameras is not None:
        env["FARMSURV_BASELINE_OVERRIDE_CAMERAS"] = cameras
    if keywords is not None:
        env["FARMSURV_DISTANT_KEYWORDS"] = keywords
    return env


def _load_module():
    """Import (or reload) the module and return it."""
    import infra.alert_overrides_baseline as mod
    importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# AC1 / AC2: Module exists and exports
# ---------------------------------------------------------------------------


def test_module_exists():
    """AC1: module file exists."""
    import infra.alert_overrides_baseline as mod
    assert mod.__file__ is not None
    assert mod.__file__.endswith("alert_overrides_baseline.py")


def test_module_exports():
    """AC2: module exports _apply_distant_vehicle_baseline_override callable."""
    mod = _load_module()
    assert callable(mod._apply_distant_vehicle_baseline_override)


# ---------------------------------------------------------------------------
# AC3: Vehicle L1 with distant keyword + camera in list -> demoted
# ---------------------------------------------------------------------------


def test_demote_vehicle_l1_with_keyword():
    """AC3: L1 car with distant/headlight keyword -> demoted to 0."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "car", "scene_description": "faint car with distant headlight"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 0, f"should demote: {result}"


# ---------------------------------------------------------------------------
# AC4: Person with distant keyword -> NOT demoted (person safety wins)
# ---------------------------------------------------------------------------


def test_person_not_demoted():
    """AC4: top_class=person with distant keyword -> NOT demoted."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "person", "scene_description": "faint person with distant headlight"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 1, f"person must NOT be demoted: {result}"


# ---------------------------------------------------------------------------
# AC5: L2 alert preserved regardless of distance keyword
# ---------------------------------------------------------------------------


def test_l2_preserved():
    """AC5: L2 alert preserved regardless of distance keyword."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 2}
        vr = {"top_class": "car", "scene_description": "faint car"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 2, f"L2 must NOT be demoted: {result}"


# ---------------------------------------------------------------------------
# AC6: No distance keyword -> NOT demoted (clear close car)
# ---------------------------------------------------------------------------


def test_no_keyword_not_demoted():
    """AC6: a clear close car is NOT demoted (no distance keyword)."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "car", "scene_description": "clear bright car right next to camera"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 1, f"no distance keyword must NOT demote: {result}"


# ---------------------------------------------------------------------------
# AC7: Kill switch disables override
# ---------------------------------------------------------------------------


def test_kill_switch_disabled():
    """AC7: FARMSURV_BASELINE_OVERRIDE_ENABLED=0 disables the override."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(enabled="0", cameras="OUTSIDE_FRONT_SOLAR"),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "car", "scene_description": "faint car"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 1, f"kill switch must disable: {result}"


# ---------------------------------------------------------------------------
# AC8: 3+ os.environ.get calls
# ---------------------------------------------------------------------------


def test_reads_env_vars():
    """AC8: module reads 3+ os.environ.get calls."""
    mod = _load_module()
    with open(mod.__file__) as fh:
        source = fh.read()
    count = source.count("os.environ.get")
    assert count >= 3, f"Expected 3+ os.environ.get calls, found {count}"


# ---------------------------------------------------------------------------
# AC9: No hardcoded keyword list or hour constant
# ---------------------------------------------------------------------------


def test_no_hardcoded_keywords_or_hours():
    """AC9: no DISTANT_KEYWORDS_DEFAULT or OFFHOURS_START in source as assignments."""
    mod = _load_module()
    with open(mod.__file__) as fh:
        source = fh.read()
    import re

    pattern = re.compile(
        r"^\s*(DISTANT_KEYWORDS_DEFAULT|OFFHOURS_START|OFFHOURS_END)\s*="
    )
    assert not pattern.search(source), "Found hardcoded keyword list or hour constant in source"


# ---------------------------------------------------------------------------
# AC10: Animal NOT demoted
# ---------------------------------------------------------------------------


def test_animal_not_demoted():
    """AC10: top_class=animal with distant keyword -> NOT demoted."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "dog", "scene_description": "faint dog with distant headlight"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 1, f"animal must NOT be demoted: {result}"


# ---------------------------------------------------------------------------
# Camera NOT in env list -> NOT demoted
# ---------------------------------------------------------------------------


def test_camera_not_in_list_not_demoted():
    """Camera not in env list -> NOT demoted even with keyword."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "car", "scene_description": "faint car"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "INSIDE_GARAGE", None
        )
        assert result["threat_level"] == 1, f"out-of-scope camera must NOT demote: {result}"


# ---------------------------------------------------------------------------
# Multiple cameras in list
# ---------------------------------------------------------------------------


def test_multiple_cameras_in_list():
    """Multiple cameras in env list: any match gates."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR,OUTSIDE_BACK_SOLAR,INSIDE_GARAGE",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "car", "scene_description": "faint car"}

        for cam in ["OUTSIDE_FRONT_SOLAR", "OUTSIDE_BACK_SOLAR", "INSIDE_GARAGE"]:
            result = mod._apply_distant_vehicle_baseline_override(
                alert.copy(), vr, cam, None
            )
            assert result["threat_level"] == 0, f"{cam} should be demoted"

        result = mod._apply_distant_vehicle_baseline_override(
            alert.copy(), vr, "GARAGE_SIDE", None
        )
        assert result["threat_level"] == 1, "GARAGE_SIDE should NOT be demoted"


# ---------------------------------------------------------------------------
# Empty camera list = nothing demoted
# ---------------------------------------------------------------------------


def test_empty_camera_list_demotes_nothing():
    """Empty FARMSURV_BASELINE_OVERRIDE_CAMERAS -> no camera is demoted."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(enabled="1", cameras="", keywords="faint,distant,headlight"),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "car", "scene_description": "faint car"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "ANY_CAMERA", None
        )
        assert result["threat_level"] == 1


# ---------------------------------------------------------------------------
# Empty keywords = nothing demoted
# ---------------------------------------------------------------------------


def test_empty_keywords_demotes_nothing():
    """Empty FARMSURV_DISTANT_KEYWORDS -> no demotions."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1", cameras="OUTSIDE_FRONT_SOLAR", keywords=""
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "car", "scene_description": "faint car"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 1


# ---------------------------------------------------------------------------
# Various vehicle classes eligible for demotion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vehicle", ["car", "truck", "bus", "motorcycle", "bicycle"])
def test_various_vehicle_classes_demoted(vehicle):
    """Various vehicle classes: all eligible for demotion when keyword present."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": vehicle, "scene_description": f"faint {vehicle}"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 0


# ---------------------------------------------------------------------------
# Keyword matching (case-insensitive, substring)
# ---------------------------------------------------------------------------


def test_keyword_case_insensitive():
    """Keywords are matched case-insensitively."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1}
        vr = {"top_class": "car", "scene_description": "FAINT CAR WITH DISTANT HEADLIGHT"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result["threat_level"] == 0


# ---------------------------------------------------------------------------
# _parse_cameras edge cases
# ---------------------------------------------------------------------------


def test_parse_cameras_empty():
    """Empty string -> empty frozenset."""
    from infra.alert_overrides_baseline import _parse_cameras

    assert _parse_cameras("") == frozenset()


def test_parse_cameras_whitespace():
    """Whitespace-only camera names are stripped, empty entries dropped."""
    from infra.alert_overrides_baseline import _parse_cameras

    result = _parse_cameras(" CAM1 , CAM2 , ")
    assert result == frozenset({"CAM1", "CAM2"})


# ---------------------------------------------------------------------------
# _parse_keywords edge cases
# ---------------------------------------------------------------------------


def test_parse_keywords_empty():
    """Empty string -> empty frozenset."""
    from infra.alert_overrides_baseline import _parse_keywords

    assert _parse_keywords("") == frozenset()


def test_parse_keywords_lowercase():
    """Keywords are lowercased for case-insensitive matching."""
    from infra.alert_overrides_baseline import _parse_keywords

    result = _parse_keywords("FAINT,Distant,HEADLIGHT")
    assert result == frozenset({"faint", "distant", "headlight"})


# ---------------------------------------------------------------------------
# Alert dict is modified in-place
# ---------------------------------------------------------------------------


def test_alert_modified_in_place():
    """The alert dict is modified in-place, not a copy."""
    mod = _load_module()
    with patch.dict(
        "os.environ",
        _env_override(
            enabled="1",
            cameras="OUTSIDE_FRONT_SOLAR",
            keywords="faint,distant,headlight",
        ),
        clear=False,
    ):
        mod = _load_module()
        alert = {"threat_level": 1, "extra": "data"}
        vr = {"top_class": "car", "scene_description": "faint car"}
        result = mod._apply_distant_vehicle_baseline_override(
            alert, vr, "OUTSIDE_FRONT_SOLAR", None
        )
        assert result is alert, "should return the same dict object"
        assert result["threat_level"] == 0
        assert result["extra"] == "data"
