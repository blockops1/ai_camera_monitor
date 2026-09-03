"""Tests for infra.cameras — Phase.167 §13.4.

Tests use synthetic env files written to tmp_path; no operator IPs,
no operator camera names. Safe to ride along in public release.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from infra import cameras


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_env(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


@pytest.fixture
def new_schema_env(tmp_path: Path) -> Path:
    """Synthetic CAM{N}_IP-style env file (the §13.2 NEW schema)."""
    return _write_env(tmp_path / "cams.env", """
# Phase.167 §13.4 synthetic test fixture (NEW schema).
CAM1_IP=10.0.0.1
CAM1_NAME=Front Porch
CAM1_ZONE=yard
CAM1_HTTP_USER=admin
CAM1_HTTP_PASS=secret1
CAM1_RTSP_URL=rtsp://admin:secret1@10.0.0.1:554/stream1

CAM2_IP=10.0.0.2
CAM2_NAME=Back Yard
CAM2_ZONE=yard
CAM2_HTTP_USER=admin
CAM2_HTTP_PASS=secret2

CAM3_IP=10.0.0.3
CAM3_NAME=Side Garage
CAM3_ZONE=driveway
CAM3_RTSP_URL=rtsp://admin:secret3@10.0.0.3:554/stream3
""")


@pytest.fixture
def legacy_schema_env(tmp_path: Path) -> Path:
    """Synthetic FRONT_IP / OUTSIDE_*_IP-style env file (LEGACY schema).

    Uses the synthetic TEST_* prefixes (registered in
    infra.cameras._LEGACY_PREFIX_TO_NAME) so the legacy parser path is
    exercised without operator-flavored naming.
    """
    return _write_env(tmp_path / "legacy.env", """
# Legacy schema test fixture (Phase.167 §13.3 fallback path).
TEST_FRONT_IP=10.1.0.1
TEST_FRONT_HTTP_USER=admin
TEST_FRONT_HTTP_PASS=secret-front
TEST_FRONT_RTSP_URL=rtsp://admin:secret-front@10.1.0.1:554/stream

TEST_BACK_IP=10.1.0.2
TEST_BACK_HTTP_USER=admin
TEST_BACK_HTTP_PASS=secret-back
""")


@pytest.fixture
def empty_env(tmp_path: Path) -> Path:
    """An env file with no cameras at all (only comments)."""
    return _write_env(tmp_path / "empty.env", """
# nothing here
# just a comment
""")


@pytest.fixture
def mixed_env(tmp_path: Path) -> Path:
    """Both schemas present — NEW takes precedence per Phase.167 §13.3."""
    return _write_env(tmp_path / "mixed.env", """
# NEW schema (should be picked)
CAM1_IP=10.0.0.1
CAM1_NAME=Front Porch

# Legacy schema (should be ignored when NEW has any cameras)
TEST_FRONT_IP=10.1.0.99
TEST_FRONT_HTTP_PASS=should-not-be-loaded
""")


# ---------------------------------------------------------------------------
# CameraSpec dataclass
# ---------------------------------------------------------------------------


class TestCameraSpec:
    """CameraSpec is a frozen, hashable dataclass."""

    def test_frozen(self, new_schema_env: Path):
        specs = cameras.load_cameras(str(new_schema_env))
        s = specs[0]
        with pytest.raises((AttributeError, Exception)):
            s.code = "MUTATED"  # type: ignore[misc]

    def test_hashable(self, new_schema_env: Path):
        specs = cameras.load_cameras(str(new_schema_env))
        # Should not raise — frozen dataclasses are hashable by default.
        {specs[0], specs[1]}

    def test_defaults(self):
        """Required fields are code/name/ip; rest have Reolink-friendly defaults."""
        from infra.cameras import CameraSpec
        s = CameraSpec(code="CAM1", name="Test", ip="10.0.0.1")
        assert s.zone == ""
        assert s.http_user == "admin"
        assert s.http_pass == ""
        assert s.rtsp_url == ""


# ---------------------------------------------------------------------------
# NEW-schema parsing
# ---------------------------------------------------------------------------


class TestParseNewSchema:
    """Parse CAM{N}_IP-style env files."""

    def test_load_cameras_returns_list(self, new_schema_env: Path):
        specs = cameras.load_cameras(str(new_schema_env))
        assert isinstance(specs, list)
        assert len(specs) == 3

    def test_declaration_order(self, new_schema_env: Path):
        """CAM1 first, CAM2 second, CAM3 third."""
        specs = cameras.load_cameras(str(new_schema_env))
        assert [s.code for s in specs] == ["CAM1", "CAM2", "CAM3"]

    def test_full_block_parsed_correctly(self, new_schema_env: Path):
        specs = cameras.load_cameras(str(new_schema_env))
        s = specs[0]
        assert s.code == "CAM1"
        assert s.name == "Front Porch"
        assert s.ip == "10.0.0.1"
        assert s.zone == "yard"
        assert s.http_user == "admin"
        assert s.http_pass == "secret1"
        assert s.rtsp_url == "rtsp://admin:secret1@10.0.0.1:554/stream1"

    def test_partial_block_uses_defaults(self, new_schema_env: Path):
        """CAM2 has _IP, _NAME, _ZONE, _HTTP_USER, _HTTP_PASS — no _RTSP_URL.
        Should parse with rtsp_url='' default."""
        specs = cameras.load_cameras(str(new_schema_env))
        s = next(c for c in specs if c.code == "CAM2")
        assert s.rtsp_url == ""
        assert s.http_pass == "secret2"

    def test_no_http_pass_defaults_empty(self, new_schema_env: Path):
        """CAM3 has no _HTTP_PASS — should default to empty (not None)."""
        specs = cameras.load_cameras(str(new_schema_env))
        s = next(c for c in specs if c.code == "CAM3")
        assert s.http_pass == ""
        assert s.http_user == "admin"

    def test_out_of_order_codes(self, tmp_path: Path):
        """Even if env file has CAM3 first, return CAM1 → CAM2 → CAM3."""
        env = _write_env(tmp_path / "out_of_order.env", """
CAM3_IP=10.0.0.3
CAM3_NAME=Side Garage

CAM1_IP=10.0.0.1
CAM1_NAME=Front Porch

CAM2_IP=10.0.0.2
CAM2_NAME=Back Yard
""")
        specs = cameras.load_cameras(str(env))
        assert [s.code for s in specs] == ["CAM1", "CAM2", "CAM3"]

    def test_no_ip_skipped(self, tmp_path: Path):
        """CAM5_NAME without CAM5_IP is skipped — can't reach a nameless IP."""
        env = _write_env(tmp_path / "no_ip.env", """
CAM1_IP=10.0.0.1
CAM1_NAME=Front Porch
CAM5_NAME=Ghost
""")
        specs = cameras.load_cameras(str(env))
        assert len(specs) == 1
        assert specs[0].code == "CAM1"

    def test_non_cam_keys_ignored(self, tmp_path: Path):
        """Keys not starting with CAM{N}_ are ignored (e.g. CAMERA_TZ=UTC)."""
        env = _write_env(tmp_path / "noisy.env", """
CAMERA_TZ=UTC
SOME_OTHER=value
CAM1_IP=10.0.0.1
CAM1_NAME=Front Porch
""")
        specs = cameras.load_cameras(str(env))
        assert len(specs) == 1
        assert specs[0].code == "CAM1"


# ---------------------------------------------------------------------------
# Legacy-schema parsing (operator's current format)
# ---------------------------------------------------------------------------


class TestParseLegacyFallback:
    """Parse FRONT_IP / BACK_IP / OUTSIDE_*_IP style blocks."""

    def test_legacy_returns_list(self, legacy_schema_env: Path):
        specs = cameras.load_cameras(str(legacy_schema_env))
        assert len(specs) == 2

    def test_legacy_code_is_cam_n(self, legacy_schema_env: Path):
        """Legacy code is the CAM{N} code (Phase.167 §13.4)."""
        specs = cameras.load_cameras(str(legacy_schema_env))
        codes = sorted(s.code for s in specs)
        assert codes == ["CAM1", "CAM2"]

    def test_legacy_includes_creds(self, legacy_schema_env: Path):
        specs = cameras.load_cameras(str(legacy_schema_env))
        s = next(c for c in specs if c.code == "CAM1")
        assert s.http_user == "admin"
        assert s.http_pass == "secret-front"
        assert s.rtsp_url.startswith("rtsp://")


# ---------------------------------------------------------------------------
# Schema precedence
# ---------------------------------------------------------------------------


class TestSchemaPrecedence:
    """NEW schema wins when present; legacy is only fallback."""

    def test_new_takes_precedence_over_legacy(self, mixed_env: Path):
        specs = cameras.load_cameras(str(mixed_env))
        assert len(specs) == 1
        assert specs[0].code == "CAM1"
        assert specs[0].ip == "10.0.0.1"
        # Legacy TEST_FRONT was NOT loaded (proves precedence)
        assert "TEST_FRONT" not in [s.code for s in specs]

    def test_empty_env_returns_empty_list(self, empty_env: Path):
        """No cameras at all → empty list (not an error)."""
        assert cameras.load_cameras(str(empty_env)) == []

    def test_missing_file_returns_empty_list(self, tmp_path: Path):
        """Non-existent file is OK — returns [], doesn't raise."""
        assert cameras.load_cameras(str(tmp_path / "nope.env")) == []


# ---------------------------------------------------------------------------
# env_path resolution chain
# ---------------------------------------------------------------------------


class TestEnvPathResolution:
    """$FARMSURV_CAMERAS_ENV > infra.paths.CAMERA_CREDS_FILE > operator env."""

    def test_explicit_env_path_overrides_default(
        self, new_schema_env: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Pass env_path directly → uses that, ignores FARMSURV_CAMERAS_ENV."""
        monkeypatch.setenv("FARMSURV_CAMERAS_ENV", "/should/be/ignored")
        specs = cameras.load_cameras(str(new_schema_env))
        assert [s.code for s in specs] == ["CAM1", "CAM2", "CAM3"]

    def test_farmsurv_cameras_env_takes_precedence_over_default(
        self, new_schema_env: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """$FARMSURV_CAMERAS_ENV overrides infra.paths.CAMERA_CREDS_FILE."""
        monkeypatch.setenv("FARMSURV_CAMERAS_ENV", str(new_schema_env))
        specs = cameras.load_cameras()  # no explicit env_path
        assert [s.code for s in specs] == ["CAM1", "CAM2", "CAM3"]


# ---------------------------------------------------------------------------
# by_code() / by_ip() / all_codes()
# ---------------------------------------------------------------------------


class TestLookups:
    """Public lookup helpers."""

    def test_by_code_returns_match(self, new_schema_env: Path):
        spec = cameras.by_code("CAM2", str(new_schema_env))
        assert spec.code == "CAM2"
        assert spec.name == "Back Yard"

    def test_by_code_unknown_raises_with_list(self, new_schema_env: Path):
        """KeyError message includes the list of valid codes."""
        with pytest.raises(KeyError) as exc:
            cameras.by_code("CAM99", str(new_schema_env))
        msg = str(exc.value)
        assert "CAM99" in msg
        assert "CAM1" in msg
        assert "CAM2" in msg
        assert "CAM3" in msg

    def test_by_ip_returns_match(self, new_schema_env: Path):
        spec = cameras.by_ip("10.0.0.3", str(new_schema_env))
        assert spec.code == "CAM3"

    def test_by_ip_unknown_raises_with_list(self, new_schema_env: Path):
        """KeyError message includes the list of valid IPs."""
        with pytest.raises(KeyError) as exc:
            cameras.by_ip("10.99.99.99", str(new_schema_env))
        msg = str(exc.value)
        assert "10.99.99.99" in msg
        assert "10.0.0.1" in msg

    def test_all_codes_in_declaration_order(self, new_schema_env: Path):
        assert cameras.all_codes(str(new_schema_env)) == ["CAM1", "CAM2", "CAM3"]

    def test_all_codes_empty_env(self, empty_env: Path):
        assert cameras.all_codes(str(empty_env)) == []

    def test_index_raises_on_duplicate_codes(self, tmp_path: Path):
        """Two _IP entries under the same code → ValueError."""
        env = _write_env(tmp_path / "dup.env", """
CAM1_IP=10.0.0.1
CAM1_IP=10.0.0.99
""")
        # _parse_new_schema will treat the second as override (last-write-wins)
        # so the dict has only one CAM1 entry. Need to construct duplicate
        # via the legacy path or force duplicates in the kv dict.
        # Easiest: load with a hand-built dict via direct module call.
        from infra.cameras import by_code, _index
        # Force duplicate by mocking _index on the new-schema output.
        # Since the parser dedupes at parse time, we test _index directly:
        from infra.cameras import CameraSpec
        s1 = CameraSpec(code="CAM1", name="A", ip="10.0.0.1")
        s2 = CameraSpec(code="CAM1", name="B", ip="10.0.0.2")
        with pytest.raises(ValueError) as exc:
            _index([s1, s2])
        assert "duplicate" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Module contract: integration with the rest of infra/
# ---------------------------------------------------------------------------


class TestModuleContract:
    """Phase.167 §13.4 — cameras.py is the new way to enumerate cameras.

    `all_codes()` + `by_code()` + `by_ip()` are the public API. The legacy
    `infra.camera_creds.load_camera_creds()` still works but new code
    should prefer this module.
    """

    def test_dataclass_fields_match_plan(self):
        """CameraSpec fields match §13.5 of PLAN.md (verbatim API contract)."""
        from infra.cameras import CameraSpec
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(CameraSpec)}
        assert field_names == {
            "code", "name", "ip", "zone", "http_user", "http_pass", "rtsp_url"
        }

    def test_module_does_not_import_camera_creds(self):
        """cameras.py must stand alone (Commit 5 = additive module).

        Commit 6 will add the reverse dependency (camera_creds → cameras).
        """
        import infra.cameras as m
        # Direct attribute check: cameras does not re-export load_camera_creds
        assert not hasattr(m, "load_camera_creds")


class TestRtspHostExtraction:
    """Phase.167 §13.4 — _rtsp_host helper (Phase.166 §11.87.2 carry-over).

    Moved from infra.camera_creds._extract_ip so the RTSP-URL parsing
    lives with the rest of the camera identity code. Used by IP-rotation
    detection and (in camera_creds delegation) by the legacy dict parser.
    """

    def test_plain_ipv4(self):
        from infra.cameras import _rtsp_host
        assert _rtsp_host("rtsp://admin:secret@10.0.0.1:554/stream") == "10.0.0.1"

    def test_at_literal_in_password(self):
        """Reolink passwords may contain @ — last @ separates auth from host."""
        from infra.cameras import _rtsp_host
        url = "rtsp://admin:p@ss:word@10.0.0.1:554/stream"
        assert _rtsp_host(url) == "10.0.0.1"

    def test_urlencoded_at_in_password(self):
        from infra.cameras import _rtsp_host
        url = "rtsp://admin:p%40ssword@10.0.0.2:554/stream"
        assert _rtsp_host(url) == "10.0.0.2"

    def test_ipv6_host(self):
        from infra.cameras import _rtsp_host
        assert _rtsp_host("rtsp://admin:secret@[::1]:554/stream") == "::1"

    def test_no_auth(self):
        """Some test fixtures use host:port without user:pass@ — still parses."""
        from infra.cameras import _rtsp_host
        assert _rtsp_host("rtsp://10.0.0.5:554/stream") == "10.0.0.5"

    def test_empty_string_returns_none(self):
        from infra.cameras import _rtsp_host
        assert _rtsp_host("") is None

    def test_non_rtsp_scheme_returns_none(self):
        from infra.cameras import _rtsp_host
        assert _rtsp_host("http://10.0.0.1/admin") is None

    def test_malformed_no_port_returns_host(self):
        """Edge case — host with no port. Should still extract host."""
        from infra.cameras import _rtsp_host
        assert _rtsp_host("rtsp://10.0.0.7/stream") == "10.0.0.7"


class TestIpRotationWarning:
    """Phase.167 §13.4 — IP-rotation warning surfaced from camera_creds.

    Reolink DHCP can rotate a camera's IP between reboots. When the
    operator updates one of _IP / _RTSP_URL but not the other, capture
    scripts and browser-automation scripts end up talking to different
    cameras. Phase.166 §11.87.2 added this warning in infra.camera_creds;
    surfacing it from infra.cameras means every caller gets it for free.
    """

    def test_match_means_no_warning(self, capsys):
        from infra.cameras import CameraSpec, _warn_if_ip_rotated
        spec = CameraSpec(
            code="CAM1", name="Test", ip="10.0.0.1",
            rtsp_url="rtsp://admin:secret@10.0.0.1:554/stream",
        )
        _warn_if_ip_rotated(spec)
        captured = capsys.readouterr()
        assert "IP rotation" not in captured.out

    def test_mismatch_emits_warning(self, capsys):
        from infra.cameras import CameraSpec, _warn_if_ip_rotated
        spec = CameraSpec(
            code="CAM1", name="Test Front", ip="10.0.0.99",
            rtsp_url="rtsp://admin:secret@10.0.0.1:554/stream",
        )
        _warn_if_ip_rotated(spec)
        captured = capsys.readouterr()
        assert "IP rotation detected" in captured.out
        assert "Test Front" in captured.out
        assert "10.0.0.99" in captured.out
        assert "10.0.0.1" in captured.out

    def test_no_rtsp_url_means_no_warning(self, capsys):
        """Cameras without an RTSP URL can't drift — no comparison possible."""
        from infra.cameras import CameraSpec, _warn_if_ip_rotated
        spec = CameraSpec(code="CAM1", name="Test", ip="10.0.0.1", rtsp_url="")
        _warn_if_ip_rotated(spec)
        captured = capsys.readouterr()
        assert "IP rotation" not in captured.out

    def test_malformed_rtsp_url_means_no_warning(self, capsys):
        """_rtsp_host returns None → no comparison → no warning."""
        from infra.cameras import CameraSpec, _warn_if_ip_rotated
        spec = CameraSpec(
            code="CAM1", name="Test", ip="10.0.0.1",
            rtsp_url="not-a-rtsp-url",
        )
        _warn_if_ip_rotated(spec)
        captured = capsys.readouterr()
        assert "IP rotation" not in captured.out

    def test_warning_emitted_via_load_cameras(self, tmp_path: Path, capsys):
        """End-to-end: load_cameras() triggers the warning on drift."""
        env_file = tmp_path / "drift.env"
        env_file.write_text(
            "CAM1_IP=10.0.0.99\n"
            "CAM1_NAME=Test Front\n"
            "CAM1_RTSP_URL=rtsp://admin:secret@10.0.0.1:554/stream\n"
        )
        from infra.cameras import load_cameras
        load_cameras(str(env_file))
        captured = capsys.readouterr()
        assert "IP rotation detected" in captured.out

    def test_no_warning_via_load_cameras_when_match(self, tmp_path: Path, capsys):
        """End-to-end: load_cameras() silent when _IP and RTSP host agree."""
        env_file = tmp_path / "match.env"
        env_file.write_text(
            "CAM1_IP=10.0.0.1\n"
            "CAM1_NAME=Test Front\n"
            "CAM1_RTSP_URL=rtsp://admin:secret@10.0.0.1:554/stream\n"
        )
        from infra.cameras import load_cameras
        load_cameras(str(env_file))
        captured = capsys.readouterr()
        assert "IP rotation" not in captured.out


# ---------------------------------------------------------------------------
# Phase.167 §13.5 Commit 13: display_name_for / code_for
# ---------------------------------------------------------------------------


class TestPhase6B167DisplayNameFor:
    """display_name_for(identifier) resolves name OR code → spec.name."""

    def test_code_resolves_to_spec_name(self, new_schema_env: Path):
        # CAM1's synthetic name is "Front Porch" (per the fixture).
        assert cameras.display_name_for("CAM1", env_path=str(new_schema_env)) == "Front Porch"
        assert cameras.display_name_for("CAM2", env_path=str(new_schema_env)) == "Back Yard"
        assert cameras.display_name_for("CAM3", env_path=str(new_schema_env)) == "Side Garage"

    def test_name_resolves_to_self(self, new_schema_env: Path):
        # The helper is its own inverse when given the display name.
        assert cameras.display_name_for("Front Porch", env_path=str(new_schema_env)) == "Front Porch"
        assert cameras.display_name_for("Back Yard", env_path=str(new_schema_env)) == "Back Yard"

    def test_unknown_code_returns_input_unchanged(self, new_schema_env: Path):
        # Graceful fallback: unknown identifier is returned as-is so the
        # caller (formatter) doesn't crash on stale fixtures / legacy
        # callers. The spec for Commit 13 calls this out as the contract.
        assert cameras.display_name_for("CAM9", env_path=str(new_schema_env)) == "CAM9"
        assert cameras.display_name_for("", env_path=str(new_schema_env)) == ""

    def test_unknown_name_returns_input_unchanged(self, new_schema_env: Path):
        # If a caller passes a friendly name that's no longer in the
        # registry (operator renamed a camera), the body shows the
        # original string. Better to leak a stale label than to crash.
        assert cameras.display_name_for("Fake Porch", env_path=str(new_schema_env)) == "Fake Porch"

    def test_is_pure_no_module_mutation(self, new_schema_env: Path):
        # Two consecutive calls return the same value and don't mutate
        # internal state. load_cameras() should cache, but display_name_for
        # shouldn't introduce new globals.
        a = cameras.display_name_for("CAM1", env_path=str(new_schema_env))
        b = cameras.display_name_for("CAM1", env_path=str(new_schema_env))
        assert a == b == "Front Porch"

    def test_no_pii_in_returned_names(self, new_schema_env: Path):
        # Regression guard: the fixture uses synthetic names (Front Porch,
        # Back Yard, Side Garage). If a future refactor leaks operator
        # names into the fixture or helper, this catches it.
        names = [
            cameras.display_name_for(code, env_path=str(new_schema_env))
            for code in ("CAM1", "CAM2", "CAM3")
        ]
        assert names == ["Front Porch", "Back Yard", "Side Garage"]
        # Hard guard: none of the legacy operator-flavored tokens.
        for n in names:
            assert "Front Door" not in n
            assert "Outside" not in n
            assert "CAM5" not in n


class TestPhase6B167CodeFor:
    """code_for(identifier) is the inverse: name OR code → spec.code."""

    def test_code_returns_unchanged(self, new_schema_env: Path):
        # CAM1 is already a code → returns unchanged.
        assert cameras.code_for("CAM1", env_path=str(new_schema_env)) == "CAM1"
        assert cameras.code_for("CAM2", env_path=str(new_schema_env)) == "CAM2"

    def test_name_returns_matching_code(self, new_schema_env: Path):
        # "Front Porch" → "CAM1" via the synthetic fixture.
        assert cameras.code_for("Front Porch", env_path=str(new_schema_env)) == "CAM1"
        assert cameras.code_for("Back Yard", env_path=str(new_schema_env)) == "CAM2"
        assert cameras.code_for("Side Garage", env_path=str(new_schema_env)) == "CAM3"

    def test_unknown_identifier_returns_unchanged(self, new_schema_env: Path):
        # Same fallback contract as display_name_for: audit-log writers
        # that get an unrecognized identifier don't crash, they record
        # what they were given.
        assert cameras.code_for("CAM9", env_path=str(new_schema_env)) == "CAM9"
        assert cameras.code_for("Fake Porch", env_path=str(new_schema_env)) == "Fake Porch"

    def test_round_trip_display_then_code(self, new_schema_env: Path):
        # display_name_for ∘ code_for = identity on codes.
        for code in ("CAM1", "CAM2", "CAM3"):
            name = cameras.display_name_for(code, env_path=str(new_schema_env))
            assert cameras.code_for(name, env_path=str(new_schema_env)) == code

    def test_round_trip_code_then_display(self, new_schema_env: Path):
        # code_for ∘ display_name_for = identity on names.
        for name in ("Front Porch", "Back Yard", "Side Garage"):
            code = cameras.code_for(name, env_path=str(new_schema_env))
            assert cameras.display_name_for(code, env_path=str(new_schema_env)) == name

# -----------------------------------------------------------------------------
# §11.115.1 — RTSP-presence filter (PLAN §11.115 "RTSP = gatekeeper" rule)
# -----------------------------------------------------------------------------
#
# Note directive 2026-09-02 PM: "we should ignore any web hook from a camera
# that does not have an RTSP stream running. So by definition any camera that
# has an RTSP stream set up is a gatekeeper and everything else gets ignored."
#
# The listener must drop any alert whose camera has no RTSP URL configured.
# Cameras with a non-empty rtsp:// URL pass; everything else drops.


class TestHasRtsp:
    """cameras.has_rtsp(camera_name) -> bool — RTSP-presence filter."""

    def test_returns_true_when_rtsp_url_set(self, new_schema_env: Path):
        """CAM1 has rtsp_url=rtsp://admin:secret1@10.0.0.1:554/stream1 → True."""
        assert cameras.has_rtsp("Front Porch", env_path=str(new_schema_env)) is True

    def test_returns_false_when_rtsp_url_empty(self, tmp_path: Path) -> None:
        """Camera exists in registry but rtsp_url is empty → False (drop alert)."""
        env = tmp_path / "cams.env"
        env.write_text(
            "CAM1_IP=10.0.0.1\n"
            "CAM1_NAME=No Rtsp Cam\n"
            # no CAM1_RTSP_URL line
        )
        assert cameras.has_rtsp("No Rtsp Cam", env_path=str(env)) is False

    def test_returns_false_when_rtsp_url_malformed(self, tmp_path: Path) -> None:
        """rtsp_url that doesn't start with rtsp:// → False."""
        env = tmp_path / "cams.env"
        env.write_text(
            "CAM1_IP=10.0.0.1\n"
            "CAM1_NAME=Bad Rtsp Cam\n"
            "CAM1_RTSP_URL=http://wrong-scheme.example/stream\n"
        )
        assert cameras.has_rtsp("Bad Rtsp Cam", env_path=str(env)) is False

    def test_returns_false_for_unknown_camera(self, new_schema_env: Path) -> None:
        """Unknown camera name (not in registry) → False (drop alert).

        Defensive: a webhook from a camera we don't know about is not a
        gatekeeper. Better to drop than to risk processing an alert we
        can't tie back to a real device.
        """
        assert cameras.has_rtsp("Ghost Camera", env_path=str(new_schema_env)) is False

    def test_legacy_schema_camera_with_rtsp(self, tmp_path: Path) -> None:
        """Legacy FRONT_IP-style camera with RTSP URL → True."""
        env = tmp_path / "cams.env"
        env.write_text(
            "FRONT_IP=10.0.0.1\n"
            "FRONT_RTSP_URL=rtsp://admin:secret@10.0.0.1:554/stream1\n"
        )
        assert cameras.has_rtsp("Front Door Outside", env_path=str(env)) is True

    def test_legacy_schema_camera_without_rtsp(self, tmp_path: Path) -> None:
        """Legacy camera exists (has _IP) but no _RTSP_URL → False."""
        env = tmp_path / "cams.env"
        env.write_text(
            "FRONT_IP=10.0.0.1\n"
            # no FRONT_RTSP_URL
        )
        assert cameras.has_rtsp("Front Door Outside", env_path=str(env)) is False
