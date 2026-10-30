"""
test_night_rescue.py — unit tests for infra.night_rescue (US-111a).

Tests the night_rescue pipeline:
  - env gating (rescue_enabled, conf_floor)
  - night check
  - bbox availability
  - VM1 classify result matching
  - exception handling

AC4: env -u PYTHONPATH .venv/bin/python3.11 -m pytest tests/test_night_rescue.py -q
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def _clear_night_rescue_env(monkeypatch):
    """Ensure night_rescue env vars are unset for each test."""
    monkeypatch.delenv("FARMSURV_NIGHT_RESCUE", raising=False)
    monkeypatch.delenv("FARMSURV_NIGHT_RESCUE_CONF_FLOOR", raising=False)


@pytest.fixture()
def _utc_summer():
    """Return a summer-time UTC datetime (not night)."""
    return datetime(2026, 7, 15, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def _utc_winter():
    """Return a winter-time UTC datetime that falls in the night window.

    We mock is_night_at_edt to return True so we don't need real lat/lon.
    """
    return datetime(2026, 1, 15, 3, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def _mock_night_true(_clear_night_rescue_env):
    """Mock is_night_at_edt to return True."""
    with patch("infra.night_rescue.is_night_at_edt", return_value=True):
        yield


@pytest.fixture()
def _mock_night_false(_clear_night_rescue_env):
    """Mock is_night_at_edt to return False."""
    with patch("infra.night_rescue.is_night_at_edt", return_value=False):
        yield


@pytest.fixture()
def _mock_crop(tmp_path):
    """Mock crop_frame_to_bbox to return a valid path."""
    from PIL import Image

    crop_path = tmp_path / "crop.png"
    img = Image.new("RGB", (2, 2), color=(128, 128, 128))
    img.save(str(crop_path), format="PNG")

    def _crop_fn(frame_path, bbox):
        return str(crop_path)

    with patch(
        "infra.night_rescue._crop_frame_to_bbox", side_effect=_crop_fn
    ):
        yield


@pytest.fixture()
def _mock_vm1(monkeypatch):
    """Mock verify_class to return a vehicle classification."""
    mock_result = {"class": "vehicle", "confidence": 0.7}

    def mock_verify(crop_a, crop_b):
        return mock_result

    monkeypatch.setattr("infra.night_rescue._verify_class", mock_verify)


# ---------------------------------------------------------------------------
# rescue_enabled
# ---------------------------------------------------------------------------


def test_rescue_enabled_default_off(_clear_night_rescue_env):
    """Unset env var → False."""
    from infra.night_rescue import rescue_enabled

    assert rescue_enabled() is False


def test_rescue_enabled_0(_clear_night_rescue_env, monkeypatch):
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "0")
    from infra.night_rescue import rescue_enabled

    assert rescue_enabled() is False


def test_rescue_enabled_1(_clear_night_rescue_env, monkeypatch):
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "1")
    from infra.night_rescue import rescue_enabled

    assert rescue_enabled() is True


def test_rescue_enabled_on(_clear_night_rescue_env, monkeypatch):
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "ON")
    from infra.night_rescue import rescue_enabled

    assert rescue_enabled() is True


# ---------------------------------------------------------------------------
# conf_floor
# ---------------------------------------------------------------------------


def test_conf_floor_default(_clear_night_rescue_env):
    from infra.night_rescue import conf_floor

    assert conf_floor() == 0.50


def test_conf_floor_custom(_clear_night_rescue_env, monkeypatch):
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE_CONF_FLOOR", "0.75")
    from infra.night_rescue import conf_floor

    assert conf_floor() == 0.75


def test_conf_floor_bad_value_fallback(_clear_night_rescue_env, monkeypatch):
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE_CONF_FLOOR", "not_a_float")
    from infra.night_rescue import conf_floor

    assert conf_floor() == 0.50


# ---------------------------------------------------------------------------
# night_rescue — scenario tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def _dummy_frame(tmp_path):
    """Write a tiny 2x2 red JPEG to tmp_path."""
    from PIL import Image

    img = Image.new("RGB", (2, 2), color=(255, 0, 0))
    p = tmp_path / "frame1.jpg"
    img.save(str(p), format="JPEG")
    return str(p)


@pytest.fixture()
def _dummy_frame2(tmp_path):
    from PIL import Image

    img = Image.new("RGB", (2, 2), color=(0, 255, 0))
    p = tmp_path / "frame2.jpg"
    img.save(str(p), format="JPEG")
    return str(p)


def test_flag_off_returns_none(
    _clear_night_rescue_env,
    _utc_winter,
    _mock_crop,
    _mock_vm1,
    _dummy_frame,
    _dummy_frame2,
):
    """AC: flag off → None (byte-identical drop path)."""
    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame, _dummy_frame2],
        bboxes=[(0, 0, 100, 100)],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_winter,
    )
    assert result is None


def test_day_returns_none(
    _clear_night_rescue_env,
    _utc_summer,
    _mock_night_false,
    _mock_crop,
    _mock_vm1,
    _dummy_frame,
    _dummy_frame2,
):
    """AC: day → None (not night)."""
    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame, _dummy_frame2],
        bboxes=[(0, 0, 100, 100)],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_summer,
    )
    assert result is None


def test_no_bbox_returns_none(
    _clear_night_rescue_env,
    _utc_winter,
    _mock_crop,
    _mock_vm1,
    _dummy_frame,
):
    """AC: no bbox → None."""
    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame],
        bboxes=[],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_winter,
    )
    assert result is None


def test_vehicle_07_returns_rescue_dict(
    _clear_night_rescue_env,
    monkeypatch,
    _utc_winter,
    _mock_night_true,
    _mock_crop,
    _mock_vm1,
    _dummy_frame,
    _dummy_frame2,
):
    """AC: vehicle 0.7 → rescue dict (>= 0.50 floor)."""
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "1")
    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame, _dummy_frame2],
        bboxes=[(0, 0, 100, 100)],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_winter,
    )
    assert result is not None
    assert result["status"] == "ok"
    assert result["reason"] == "night_rescue_vm1"
    assert result["classification"] == "vehicle"
    assert result["confidence"] == 0.7


def test_vehicle_03_below_floor_returns_none(
    _clear_night_rescue_env,
    monkeypatch,
    _utc_winter,
    _mock_night_true,
    _mock_crop,
    _dummy_frame,
    _dummy_frame2,
):
    """AC: vehicle 0.3 → None (below 0.50 floor)."""
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "1")

    def _low_conf(_a, _b):
        return {"class": "vehicle", "confidence": 0.3}

    monkeypatch.setattr("infra.night_rescue._verify_class", _low_conf)

    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame, _dummy_frame2],
        bboxes=[(0, 0, 100, 100)],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_winter,
    )
    assert result is None


def test_unsure_class_returns_none(
    _clear_night_rescue_env,
    monkeypatch,
    _utc_winter,
    _mock_night_true,
    _mock_crop,
    _dummy_frame,
    _dummy_frame2,
):
    """AC: unsure → None (not in {vehicle, person, animal})."""
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "1")

    def _unsure(_a, _b):
        return {"class": "unsure", "confidence": 0.9}

    monkeypatch.setattr("infra.night_rescue._verify_class", _unsure)

    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame, _dummy_frame2],
        bboxes=[(0, 0, 100, 100)],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_winter,
    )
    assert result is None


def test_vision_error_returns_none(
    _clear_night_rescue_env,
    monkeypatch,
    _utc_winter,
    _mock_night_true,
    _mock_crop,
    _dummy_frame,
    _dummy_frame2,
):
    """AC: verify_class raises → None + warning logged."""
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "1")

    def _raises(_a, _b):
        raise Exception("llama-server down")

    monkeypatch.setattr("infra.night_rescue._verify_class", _raises)

    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame, _dummy_frame2],
        bboxes=[(0, 0, 100, 100)],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_winter,
    )
    assert result is None


def test_string_confidence_mapping(
    _clear_night_rescue_env,
    monkeypatch,
    _utc_winter,
    _mock_night_true,
    _mock_crop,
    _dummy_frame,
    _dummy_frame2,
):
    """AC: VM1 returns enum-string confidence ('likely' → 0.7) → rescue."""
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "1")

    def _likely(_a, _b):
        return {"class": "vehicle", "confidence": "likely"}

    monkeypatch.setattr("infra.night_rescue._verify_class", _likely)

    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame, _dummy_frame2],
        bboxes=[(0, 0, 100, 100)],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_winter,
    )
    assert result is not None
    assert result["confidence"] == 0.7


def test_string_confidence_below_floor(
    _clear_night_rescue_env,
    monkeypatch,
    _utc_winter,
    _mock_night_true,
    _mock_crop,
    _dummy_frame,
    _dummy_frame2,
):
    """'unlikely' → 0.0 → below floor → None."""
    monkeypatch.setenv("FARMSURV_NIGHT_RESCUE", "1")

    def _unlikely(_a, _b):
        return {"class": "vehicle", "confidence": "unlikely"}

    monkeypatch.setattr("infra.night_rescue._verify_class", _unlikely)

    from infra.night_rescue import night_rescue

    result = night_rescue(
        frames=[_dummy_frame, _dummy_frame2],
        bboxes=[(0, 0, 100, 100)],
        camera_id="TEST_CAM",
        alert_id="evt-001",
        timestamp=_utc_winter,
    )
    assert result is None
