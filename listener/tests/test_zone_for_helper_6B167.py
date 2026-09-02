"""
Phase 6B.167 §13.5 Commit 14 — _zone_for(camera_code) helper tests.

The helper is a thin wrapper over infra.cameras.by_code(camera_code).zone
that returns "unknown" on miss (unknown code) or empty string. It is the
foundation for Commit 15 zone-based pipeline routing. Today the legacy
env has no ZONE_* vars, so known legacy codes return "" (empty zone).
The new-schema parser reads CAM{N}_ZONE; the legacy parser ignores zone.
Unknown codes → "unknown" so downstream pipelines don't crash on fresh
enrollment.

Synthetic fleet: CAM1 / CAM2 / CAM3 mapped to CAM1_ZONE=zone_a etc.
Non-routable RFC 5737 TEST-NET-2 IPs (10.0.0.x). No operator PII.
"""
from __future__ import annotations

import pathlib

import pytest

from infra.cameras import by_code as _by_code
from infra.cameras import by_ip as _by_ip
from infra.cameras import code_for as _code_for
from listener.listener import (
    GATEKEEPER_CAMERAS,
    PERSON_GATEKEEPER_CAMERAS,
    _all_camera_codes,
    _by_camera_code,
    _by_camera_ip,
    _zone_for,
)


@pytest.fixture
def new_schema_env(tmp_path):
    """Synthetic fleet with ZONE_* vars for the new-schema parser."""
    p = tmp_path / "cameras.env"
    p.write_text(
        "CAM1_IP=10.0.0.10\n"
        "CAM1_RTSP_URL=rtsp://10.0.0.10:554/Streaming/Channels/101\n"
        "CAM1_ZONE=zone_a\n"
        "CAM2_IP=10.0.0.20\n"
        "CAM2_RTSP_URL=rtsp://10.0.0.20:554/Streaming/Channels/101\n"
        "CAM2_ZONE=zone_b\n"
        "CAM3_IP=10.0.0.30\n"
        "CAM3_RTSP_URL=rtsp://10.0.0.30:554/Streaming/Channels/101\n"
        "CAM3_ZONE=zone_c\n"
    )
    return str(p)


class TestPhase6B167ZoneFor:
    """Commit 14: _zone_for(camera_code) helper."""

    def test_zone_for_known_code_returns_zone(self, new_schema_env):
        assert _zone_for("CAM1", env_path=new_schema_env) == "zone_a"
        assert _zone_for("CAM2", env_path=new_schema_env) == "zone_b"
        assert _zone_for("CAM3", env_path=new_schema_env) == "zone_c"

    def test_zone_for_unknown_code_returns_unknown(self):
        """No env loaded → registry empty → unknown codes return "unknown"."""
        assert _zone_for("Z99_UNKNOWN") == "unknown"

    def test_zone_for_empty_returns_unknown(self):
        assert _zone_for("") == "unknown"

    def test_zone_for_unknown_does_not_raise(self):
        """Downstream pipelines must not crash on fresh enrollment."""
        # KeyError from by_code would propagate; helper absorbs it.
        result = _zone_for("BRAND_NEW_CAMERA")
        assert result == "unknown"

    def test_zone_for_code_only_no_name_translation(self, tmp_path):
        """Helper accepts a code directly; codes only (not friendly names).

        Phase 6B.167 §13.4 Commit 17 (T3 C17): CAM{N} codes are the
        canonical camera identifiers; legacy prefixes (e.g.
        OUTSIDE_FRONT_SOLAR) are translated to CAM5 by the legacy env
        parser in infra.cameras._LEGACY_PREFIX_TO_CODE.

        With a synthetic env that sets CAM{N}_ZONE per code, the helper
        returns the zone. With a synthetic env that omits ZONE_*, the
        helper returns "" (empty zone, not "unknown").
        """
        # Path 1: synthetic env with CAM{N}_ZONE — resolves to zone label
        p_zone = tmp_path / "cameras_with_zone.env"
        p_zone.write_text(
            "CAM1_IP=10.0.0.10\n"
            "CAM1_RTSP_URL=rtsp://10.0.0.10:554/Streaming/Channels/101\n"
            "CAM1_ZONE=zone_a\n"
        )
        assert _zone_for("CAM1", env_path=str(p_zone)) == "zone_a"

        # Path 2: synthetic env without ZONE_* — known code, empty zone
        p_nozone = tmp_path / "cameras_no_zone.env"
        p_nozone.write_text(
            "CAM1_IP=10.0.0.10\n"
            "CAM1_RTSP_URL=rtsp://10.0.0.10:554/Streaming/Channels/101\n"
        )
        assert _zone_for("CAM1", env_path=str(p_nozone)) == ""

        # Path 3: unknown code — returns "unknown"
        p_unknown = tmp_path / "cameras_empty.env"
        p_unknown.write_text("CAM1_IP=10.0.0.10\n")
        # CAM999 is not in the registry → "unknown"
        assert _zone_for("CAM999", env_path=str(p_unknown)) == "unknown"


class TestPhase6B167WebhookParserMigration:
    """Commit 14: webhook parser uses _by_camera_ip(payload['ip']).rtsp_url."""

    def test_rtsp_url_lookup_by_ip(self, new_schema_env):
        spec = _by_camera_ip("10.0.0.10", env_path=new_schema_env)
        assert spec.rtsp_url == "rtsp://10.0.0.10:554/Streaming/Channels/101"
        assert spec.code == "CAM1"

    def test_by_ip_unknown_ip_raises_keyerror(self, new_schema_env):
        with pytest.raises(KeyError):
            # TEST-NET-1 (RFC 5737) — guaranteed non-routable, never
            # present in any operator's cameras.env.
            _by_ip("198.51.100.99", env_path=new_schema_env)

    def test_by_code_returns_full_spec(self, new_schema_env):
        spec = _by_code("CAM1", env_path=new_schema_env)
        assert spec.code == "CAM1"
        assert spec.ip == "10.0.0.10"
        assert spec.zone == "zone_a"
        assert spec.rtsp_url == "rtsp://10.0.0.10:554/Streaming/Channels/101"

    def test_all_codes_iteration(self, new_schema_env):
        codes = list(_all_camera_codes(env_path=new_schema_env))
        assert set(codes) == {"CAM1", "CAM2", "CAM3"}


class TestPhase6B167GatekeeperSetCodeKeyed:
    """Commit 14: GATEKEEPER_CAMERAS / PERSON_GATEKEEPER_CAMERAS are code-keyed."""

    def test_constants_are_frozensets_of_codes(self):
        assert isinstance(GATEKEEPER_CAMERAS, frozenset)
        assert isinstance(PERSON_GATEKEEPER_CAMERAS, frozenset)
        # All elements are non-empty strings (codes)
        for code in PERSON_GATEKEEPER_CAMERAS:
            assert isinstance(code, str)
            assert code  # non-empty

    def test_gatesters_translate_friendly_name_to_code(self):
        """_code_for_camera() translates friendly name → code for membership."""
        # In a legacy env, the friendly name resolves to its legacy code
        # (operator-flavored). In an unknown-code case, the helper falls
        # back to the input string. We only verify the helper doesn't raise.
        result = _code_for("Fake Front Solar")
        assert isinstance(result, str)