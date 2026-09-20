"""Tests for quiet-hours gate wiring in listener/daemon.py.

Verifies that _send_tg1/TG#2/TG#3 suppress dispatch when in_quiet_hours()
returns True, and proceed normally when it returns False.
"""
from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

import httpx
import pytest

from listener.daemon import _send_tg1, _send_tg2, _send_tg3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NIGHT_UTC = None  # computed lazily in tests


@pytest.fixture(autouse=True)
def _quiet_env():
    """Ensure quiet-hours env knobs are set for these tests."""
    with patch.dict("os.environ", {
        "FARMSURV_QUIET_HOURS_ENABLED": "1",
        "FARMSURV_QUIET_HOURS_CAMERAS": "OUTSIDE_FRONT_SOLAR",
    }, clear=False):
        yield


# ---------------------------------------------------------------------------
# Suppression tests -- in quiet hours, all three helpers short-circuit
# ---------------------------------------------------------------------------

def test_send_tg1_suppressed_in_quiet_hours():
    """TG#1 is suppressed when in_quiet_hours returns True."""
    with patch("listener.daemon.in_quiet_hours", return_value=True):
        with patch("listener.daemon.datetime") as mock_dt:
            mock_dt.now.return_value = _build_night_dt()
            with patch("listener.daemon.dispatcher.dispatch") as mock_dispatch:
                _send_tg1({"caption": "x"}, "alert-1", "chat", "token", "OUTSIDE_FRONT_SOLAR")
    mock_dispatch.assert_not_called()


def test_send_tg2_suppressed_in_quiet_hours():
    """TG#2 is suppressed when in_quiet_hours returns True."""
    with patch("listener.daemon.in_quiet_hours", return_value=True):
        with patch("listener.daemon.datetime") as mock_dt:
            mock_dt.now.return_value = _build_night_dt()
            with patch("listener.daemon.dispatcher.dispatch") as mock_dispatch:
                _send_tg2({"caption": "x"}, "alert-2", "chat", "token", "OUTSIDE_FRONT_SOLAR")
    mock_dispatch.assert_not_called()


def test_send_tg3_suppressed_in_quiet_hours():
    """TG#3 is suppressed when in_quiet_hours returns True."""
    with patch("listener.daemon.in_quiet_hours", return_value=True):
        with patch("listener.daemon.datetime") as mock_dt:
            mock_dt.now.return_value = _build_night_dt()
            with patch("listener.daemon.dispatcher.dispatch") as mock_dispatch:
                _send_tg3({"text": "y"}, "alert-3", "chat", "token", "OUTSIDE_FRONT_SOLAR")
    mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Non-suppression tests -- NOT in quiet hours, dispatch proceeds normally
# ---------------------------------------------------------------------------

def test_send_tg1_not_suppressed_when_not_night():
    """TG#1 dispatches normally when in_quiet_hours returns False."""
    with patch("listener.daemon.in_quiet_hours", return_value=False):
        with patch("listener.daemon.dispatcher.dispatch") as mock_dispatch:
            mock_dispatch.return_value = [MagicMock(spec=httpx.Response, status_code=200)]
            _send_tg1({"caption": "x", "photos": []}, "alert-1", "chat", "token", "OUTSIDE_FRONT_SOLAR")
    mock_dispatch.assert_called_once()


def test_send_tg2_not_suppressed_when_not_night():
    """TG#2 dispatches normally when in_quiet_hours returns False."""
    with patch("listener.daemon.in_quiet_hours", return_value=False):
        with patch("listener.daemon.dispatcher.dispatch") as mock_dispatch:
            mock_dispatch.return_value = [MagicMock(spec=httpx.Response, status_code=200)]
            _send_tg2({"caption": "x", "photos": []}, "alert-2", "chat", "token", "OUTSIDE_FRONT_SOLAR")
    mock_dispatch.assert_called_once()


def test_send_tg3_not_suppressed_when_not_night():
    """TG#3 dispatches normally when in_quiet_hours returns False."""
    with patch("listener.daemon.in_quiet_hours", return_value=False):
        with patch("listener.daemon.dispatcher.dispatch") as mock_dispatch:
            mock_dispatch.return_value = [MagicMock(spec=httpx.Response, status_code=200)]
            _send_tg3({"text": "y"}, "alert-3", "chat", "token", "OUTSIDE_FRONT_SOLAR")
    mock_dispatch.assert_called_once()


def test_send_tg1_camera_not_in_env_list():
    """TG#1 dispatches normally for a camera not in FARMSURV_QUIET_HOURS_CAMERAS."""
    with patch("listener.daemon.in_quiet_hours", return_value=False):
        with patch("listener.daemon.dispatcher.dispatch") as mock_dispatch:
            mock_dispatch.return_value = [MagicMock(spec=httpx.Response, status_code=200)]
            _send_tg1({"caption": "x", "photos": []}, "alert-1", "chat", "token", "OTHER_CAMERA")
    mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_night_dt():
    """Return a UTC datetime that represents night (1:20 AM UTC)."""
    from datetime import datetime, timezone
    return datetime(2026, 9, 19, 1, 20, tzinfo=timezone.utc)
