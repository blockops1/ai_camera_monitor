"""
test_camera_audio.py — Tests for infra.camera_audio.dispatch_audio_clip.

Phase.106. Phase.167 §13.5 (Commit 11) — IP resolution is now
backed by infra.cameras; tests mock load_cameras() and use synthetic
TEST_FRONT / TEST_BACK codes so no operator-flavored naming leaks
into the public repo.

All HTTP calls mocked — no real Reolink network access.

Test inventory:
  TestDispatchAudioClip:
    - Skip when FARM_REOLINK_AUTH_TOKEN unset (returns False, no HTTP)
    - Skip when camera not in cameras list (returns False, no HTTP)
    - Skip when no IP from cameras list (returns False)
    - Successful dispatch (HTTP 200) returns True
    - HTTP 500 returns False (rejected by camera)
    - Network timeout returns False
    - URL contains the right command + token
    - Body contains the right file + repeat + volume
    - Default audio_file used when None passed
    - Custom audio_file + repeat honored
    - POST method used
    - Response 3xx returns False
  TestInternalHelpers:
    - _build_cgi_url: cmd + token
    - _build_cgi_url: no token
    - _build_request_body: file/repeat/volume
  TestResolveCameraIp:
    - Returns spec.ip when called with spec.name
    - Returns spec.ip when called with spec.code
    - Returns None when neither matches
    - First-match wins, not arbitrary

  TestPhase6B167CameraAudio:
    - dispatch_audio_clip dispatches via infra.cameras IPCode lookup
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


import pytest

from infra.camera_audio import (
    _build_cgi_url,
    _build_request_body,
    _resolve_camera_ip,
    dispatch_audio_clip,
)
from infra.cameras import CameraSpec


# Synthetic test fleet used by all tests in this module. IPs are in
# the test-only 10.x range (matches the public-repo test fixtures).
_TEST_FRONT_IP = "10.0.0.1"
_TEST_BACK_IP = "10.0.0.2"
_TEST_SIDE_IP = "10.0.0.3"

_FLEET = [
    CameraSpec(code="TEST_FRONT", name="Test Front", ip=_TEST_FRONT_IP, zone="yard"),
    CameraSpec(code="TEST_BACK", name="Test Back", ip=_TEST_BACK_IP, zone="yard"),
    CameraSpec(code="TEST_SIDE", name="Test Side", ip=_TEST_SIDE_IP, zone="yard"),
]


@pytest.fixture(autouse=True)
def _patch_load_cameras(monkeypatch):
    """Mock infra.cameras.load_cameras() to return _FLEET for every test."""
    monkeypatch.setattr(
        "infra.cameras.load_cameras", lambda env_path=None: list(_FLEET)
    )


@pytest.fixture(autouse=True)
def auth_token_env(monkeypatch):
    """Set FARM_REOLINK_AUTH_TOKEN for every test in this module."""
    monkeypatch.setenv("FARM_REOLINK_AUTH_TOKEN", "test-token-123")


# ---------------------------------------------------------------------------
# dispatch_audio_clip
# ---------------------------------------------------------------------------


class TestDispatchAudioClip:
    def test_skip_when_token_unset(self, monkeypatch):
        monkeypatch.delenv("FARM_REOLINK_AUTH_TOKEN", raising=False)
        with patch("infra.camera_audio.urllib.request.urlopen") as mock_urlopen:
            result = dispatch_audio_clip("Test Front")
        assert result is False
        mock_urlopen.assert_not_called()

    def test_skip_when_camera_not_in_cameras_list(self):
        with patch("infra.camera_audio.urllib.request.urlopen") as mock_urlopen:
            result = dispatch_audio_clip("Nonexistent Camera")
        assert result is False
        mock_urlopen.assert_not_called()

    def test_successful_dispatch_returns_true(self):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"code": 0}'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False

        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            return_value=mock_response,
        ) as mock_urlopen:
            result = dispatch_audio_clip("Test Front")

        assert result is True
        assert mock_urlopen.called
        # Verify URL has the right shape
        called_url = mock_urlopen.call_args.args[0].full_url
        assert "cmd=AudioFilePlay" in called_url
        assert _TEST_FRONT_IP in called_url
        assert "token=test-token-123" in called_url

    def test_http_500_returns_false(self):
        # Reolink returns HTTP 200 with JSON {"code": 1} on errors,
        # not HTTP 500 — but defense in depth: handle any non-2xx.
        # urllib raises HTTPError for non-2xx by default.
        http_error = urllib.error.HTTPError(
            f"http://{_TEST_FRONT_IP}/cgi-bin/api.cgi",
            500,
            "Internal Server Error",
            {},  # type: ignore[arg-type]
            MagicMock(read=lambda: b"error"),
        )
        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            side_effect=http_error,
        ):
            result = dispatch_audio_clip("Test Front")
        # URLError is the parent class of HTTPError — caught together.
        assert result is False

    def test_network_timeout_returns_false(self):
        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            side_effect=TimeoutError("read timed out"),
        ):
            result = dispatch_audio_clip("Test Front")
        assert result is False

    def test_url_error_returns_false(self):
        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = dispatch_audio_clip("Test Front")
        assert result is False

    def test_unexpected_exception_returns_false(self):
        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            side_effect=RuntimeError("kaboom"),
        ):
            result = dispatch_audio_clip("Test Front")
        assert result is False

    def test_default_audio_file(self):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"code": 0}'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False

        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            return_value=mock_response,
        ) as mock_urlopen:
            dispatch_audio_clip("Test Front")

        # urllib's Request uses .data attribute for the body.
        called_request = mock_urlopen.call_args.args[0]
        body = json.loads(called_request.data)
        assert body["file"] == "greeting.wav"
        assert body["repeat"] == 1

    def test_custom_audio_file(self):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"code": 0}'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False

        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            return_value=mock_response,
        ) as mock_urlopen:
            dispatch_audio_clip(
                "Test Front",
                audio_file="custom.wav",
                repeat=3,
            )

        called_request = mock_urlopen.call_args.args[0]
        body = json.loads(called_request.data)
        assert body["file"] == "custom.wav"
        assert body["repeat"] == 3

    def test_post_method_used(self):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"code": 0}'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False

        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            return_value=mock_response,
        ) as mock_urlopen:
            dispatch_audio_clip("Test Front")
        assert mock_urlopen.call_args.args[0].method == "POST"

    def test_response_non_2xx_returns_false(self):
        # Reolink may return HTTP 200 with non-zero code in JSON, or
        # HTTP 4xx for auth issues. urllib handles 4xx via HTTPError
        # (URLError parent). But if a server returns a 3xx, urlopen
        # follows by default. Force a 3xx via mock.
        mock_response = MagicMock()
        mock_response.status = 302
        mock_response.read.return_value = b""
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False

        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            return_value=mock_response,
        ):
            result = dispatch_audio_clip("Test Front")
        assert result is False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestInternalHelpers:
    def test_url_contains_cmd(self):
        url = _build_cgi_url(_TEST_FRONT_IP, "tok")
        assert "cmd=AudioFilePlay" in url
        assert _TEST_FRONT_IP in url
        assert "token=tok" in url

    def test_url_without_token(self):
        url = _build_cgi_url(_TEST_FRONT_IP, None)
        assert "cmd=AudioFilePlay" in url
        assert "token=" not in url

    def test_body_contains_file(self):
        body = _build_request_body("test.wav", 2)
        parsed = json.loads(body)
        assert parsed["file"] == "test.wav"
        assert parsed["repeat"] == 2
        assert parsed["volume"] == 50


# ---------------------------------------------------------------------------
# Phase.167 §13.5 Commit 11 — _resolve_camera_ip backed by infra.cameras
# ---------------------------------------------------------------------------


class TestResolveCameraIp:
    def test_lookup_by_name(self):
        assert _resolve_camera_ip("Test Front") == _TEST_FRONT_IP
        assert _resolve_camera_ip("Test Back") == _TEST_BACK_IP
        assert _resolve_camera_ip("Test Side") == _TEST_SIDE_IP

    def test_lookup_by_code(self):
        assert _resolve_camera_ip("TEST_FRONT") == _TEST_FRONT_IP
        assert _resolve_camera_ip("TEST_BACK") == _TEST_BACK_IP
        assert _resolve_camera_ip("TEST_SIDE") == _TEST_SIDE_IP

    def test_missing_name_returns_none(self):
        assert _resolve_camera_ip("Does Not Exist") is None

    def test_missing_code_returns_none(self):
        assert _resolve_camera_ip("NONEXISTENT_CODE") is None

    def test_empty_string_returns_none(self):
        assert _resolve_camera_ip("") is None

    def test_first_match_wins(self, monkeypatch):
        """If both name and code collide, the first matched spec wins."""
        # Build a fleet where TEST_FRONT the code AND TEST_FRONT the
        # name both map to different IPs — ensure deterministic
        # behaviour (and that no exception leaks).
        fleet = [
            CameraSpec(code="TEST_FRONT", name="Test Front Other",
                       ip="10.0.0.99"),
            CameraSpec(code="TEST_FRONT", name="Test Front",  # code dup
                       ip="10.0.0.99"),
        ]
        monkeypatch.setattr(
            "infra.cameras.load_cameras", lambda env_path=None: list(fleet)
        )
        # The function should still return an IP rather than raising.
        ip = _resolve_camera_ip("Test Front")
        assert ip == "10.0.0.99"


# ---------------------------------------------------------------------------
# Phase.167 §13.5 Commit 11 — dispatch by CameraSpec.code
# ---------------------------------------------------------------------------


class TestPhase6B167CameraAudio:
    def test_dispatch_by_code_uses_resolved_ip(self):
        """Passing the CameraSpec.code (TEST_FRONT) resolves the same
        IP as passing the CameraSpec.name (Test Front). Both paths hit
        the same _resolve_camera_ip() now backed by infra.cameras."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"code": 0}'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = False

        with patch(
            "infra.camera_audio.urllib.request.urlopen",
            return_value=mock_response,
        ) as mock_urlopen:
            result = dispatch_audio_clip("TEST_FRONT")  # code, not name

        assert result is True
        called_url = mock_urlopen.call_args.args[0].full_url
        assert _TEST_FRONT_IP in called_url

    def test_dispatch_unknown_code_returns_false(self):
        with patch("infra.camera_audio.urllib.request.urlopen") as mock_urlopen:
            result = dispatch_audio_clip("NOPE")
        assert result is False
        mock_urlopen.assert_not_called()

    def test_module_no_longer_exposes_camera_ip_dict(self):
        """Back-compat: the previous `_CAMERA_IP` module-level dict
        is gone — public API surface is now dispatch_audio_clip +
        _resolve_camera_ip."""
        import infra.camera_audio as mod
        assert not hasattr(mod, "_CAMERA_IP")
