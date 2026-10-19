"""
test_night_corroborate.py — unit tests for infra.night_corroborate (US-114a).

Tests the night vehicle corroboration gate:
  - mode parsing (off/night_vehicle/all + unknown)
  - night vehicle likely+0feats → suppressed
  - night vehicle definite+0feats → delivered
  - night vehicle likely+2feats → delivered
  - day vehicle likely+0feats → delivered (day untouched)
  - night person likely+0feats → delivered (person exempt)
  - mode off → delivered (byte-identical default)

AC3: env -u PYTHONPATH -u PYTHONHOME .venv/bin/python3.11 -m pytest tests/test_night_corroborate.py -q
"""
from __future__ import annotations

import warnings
from datetime import UTC, datetime
from unittest.mock import patch

import pytest


@pytest.fixture()
def _clear_night_corr_env(monkeypatch):
    """Ensure night_corroborate env var is unset for each test."""
    monkeypatch.delenv("FARMSURV_NIGHT_CORROBORATE", raising=False)


@pytest.fixture()
def _utc_summer():
    """Return a summer-time UTC datetime (not night)."""
    return datetime(2026, 7, 15, 14, 0, 0, tzinfo=UTC)


@pytest.fixture()
def _utc_winter():
    """Return a winter-time UTC datetime that falls in the night window."""
    return datetime(2026, 1, 15, 3, 0, 0, tzinfo=UTC)


@pytest.fixture()
def _mock_night_true():
    """Mock is_night_at_edt to return True."""
    with patch("infra.night_corroborate.is_night_at_edt", return_value=True):
        yield


@pytest.fixture()
def _mock_night_false():
    """Mock is_night_at_edt to return False."""
    with patch("infra.night_corroborate.is_night_at_edt", return_value=False):
        yield


# ---------------------------------------------------------------------------
# corroborate_enabled — env parsing
# ---------------------------------------------------------------------------


def test_enabled_default_off(_clear_night_corr_env):
    """Unset env var → off (default)."""
    from infra.night_corroborate import corroborate_enabled

    assert corroborate_enabled() == "off"


def test_enabled_0_is_off(_clear_night_corr_env, monkeypatch):
    """FARMSURV_NIGHT_CORROBORATE=off → off."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "off")
    from infra.night_corroborate import corroborate_enabled

    assert corroborate_enabled() == "off"


def test_enabled_night_vehicle(_clear_night_corr_env, monkeypatch):
    """FARMSURV_NIGHT_CORROBORATE=night_vehicle → night_vehicle."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "night_vehicle")
    from infra.night_corroborate import corroborate_enabled

    assert corroborate_enabled() == "night_vehicle"


def test_enabled_all(_clear_night_corr_env, monkeypatch):
    """FARMSURV_NIGHT_CORROBORATE=all → all."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "all")
    from infra.night_corroborate import corroborate_enabled

    assert corroborate_enabled() == "all"


def test_enabled_unknown_warns_and_defaults_off(
    _clear_night_corr_env, monkeypatch
):
    """Unknown value → off + warning."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "bogus_value")
    from infra.night_corroborate import corroborate_enabled

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        result = corroborate_enabled()

    assert result == "off"
    assert len(w) == 1
    assert "bogus_value" in str(w[0].message)


# ---------------------------------------------------------------------------
# _count_vm2_features
# ---------------------------------------------------------------------------


def test_count_features_empty():
    """Empty dict → 0."""
    from infra.night_corroborate import _count_vm2_features

    assert _count_vm2_features({}) == 0


def test_count_features_null_values():
    """All-null values → 0."""
    from infra.night_corroborate import _count_vm2_features

    vm2 = {
        "color": None,
        "vehicle_features": {
            "wheel_style": None,
            "body_style_hint": "null",
        },
    }
    assert _count_vm2_features(vm2) == 0


def test_count_features_some_non_null():
    """2 non-null vehicle_features + color → 3."""
    from infra.night_corroborate import _count_vm2_features

    vm2 = {
        "color": "white",
        "vehicle_features": {
            "wheel_style": "stock",
            "front_grille_style": "closed",
        },
    }
    assert _count_vm2_features(vm2) == 3


# ---------------------------------------------------------------------------
# night_corroborate — scenario tests
# ---------------------------------------------------------------------------


def test_mode_off_delivers_vehicle(
    _clear_night_corr_env, _utc_winter, _mock_night_true
):
    """mode off → vehicle likely+0feats DELIVERED (byte-identical default)."""
    vm1_result = {"class": "vehicle", "confidence": "likely"}
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, None, _utc_winter, "evt-001", "CAM1") is True


def test_night_vehicle_likely_0feats_suppressed(
    _clear_night_corr_env, monkeypatch, _utc_winter, _mock_night_true
):
    """mode night_vehicle + night + vehicle likely + 0 feats → suppressed."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "night_vehicle")
    vm1_result = {"class": "vehicle", "confidence": "likely"}
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, None, _utc_winter, "evt-001", "CAM1") is False


def test_night_vehicle_likely_2feats_delivered(
    _clear_night_corr_env, monkeypatch, _utc_winter, _mock_night_true
):
    """mode night_vehicle + night + vehicle likely + 2 feats → delivered."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "night_vehicle")
    vm1_result = {"class": "vehicle", "confidence": "likely"}
    vm2_result = {
        "color": "white",
        "vehicle_features": {
            "wheel_style": "stock",
            "front_grille_style": "closed",
        },
    }
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, vm2_result, _utc_winter, "evt-001", "CAM1") is True


def test_night_vehicle_definite_0feats_delivered(
    _clear_night_corr_env, monkeypatch, _utc_winter, _mock_night_true
):
    """mode night_vehicle + night + vehicle definite + 0 feats → delivered."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "night_vehicle")
    vm1_result = {"class": "vehicle", "confidence": "definite"}
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, None, _utc_winter, "evt-001", "CAM1") is True


def test_night_vehicle_daytime_delivered(
    _clear_night_corr_env, monkeypatch, _utc_summer, _mock_night_false
):
    """mode night_vehicle + DAYTIME + vehicle likely + 0 feats → delivered (day untouched)."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "night_vehicle")
    vm1_result = {"class": "vehicle", "confidence": "likely"}
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, None, _utc_summer, "evt-001", "CAM1") is True


def test_night_vehicle_person_delivered(
    _clear_night_corr_env, monkeypatch, _utc_winter, _mock_night_true
):
    """mode night_vehicle + night + person likely + 0 feats → delivered (person exempt)."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "night_vehicle")
    vm1_result = {"class": "person", "confidence": "likely"}
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, None, _utc_winter, "evt-001", "CAM1") is True


def test_night_vehicle_animal_delivered(
    _clear_night_corr_env, monkeypatch, _utc_winter, _mock_night_true
):
    """mode night_vehicle + night + animal likely + 0 feats → delivered (animal exempt)."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "night_vehicle")
    vm1_result = {"class": "animal", "confidence": "likely"}
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, None, _utc_winter, "evt-001", "CAM1") is True


def test_mode_all_person_at_night_delivered(
    _clear_night_corr_env, monkeypatch, _utc_winter, _mock_night_true
):
    """mode all + night + person → delivered (reserved, but currently just passes through)."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "all")
    vm1_result = {"class": "person", "confidence": "likely"}
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, None, _utc_winter, "evt-001", "CAM1") is True


def test_mode_all_vehicle_likely_suppressed(
    _clear_night_corr_env, monkeypatch, _utc_winter, _mock_night_true
):
    """mode all + night + vehicle likely + 0 feats → suppressed."""
    monkeypatch.setenv("FARMSURV_NIGHT_CORROBORATE", "all")
    vm1_result = {"class": "vehicle", "confidence": "likely"}
    from infra.night_corroborate import night_corroborate

    assert night_corroborate(vm1_result, None, _utc_winter, "evt-001", "CAM1") is False
