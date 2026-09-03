"""
Tests for infra.cameras.load_camera_aliases — operator shorthand resolver.

Phase 6B.167 §13.4 Commit 18 (T3 C18): operator-private shorthand is
read from CAM{N}_SNAPSHOT_ALIAS / CAM{N}_PREVIEW_ALIAS in cameras.env
(gitignored). Covers:

    - _parse_aliases_from_kv: regex parse, comma-separated shorthand,
      whitespace stripping, empty entries, unknown CAM{N} codes
    - load_camera_aliases: env_path override, default-path resolution,
      warnings for unknown cameras, missing file returns empty
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr

import pytest

from infra.cameras import _parse_aliases_from_kv, load_camera_aliases


class TestParseAliasesFromKv:
    """Direct unit tests for the alias parser (no I/O)."""

    def test_empty_kv_returns_empty_dicts(self):
        snap, prev = _parse_aliases_from_kv({})
        assert snap == {}
        assert prev == {}

    def test_snapshot_alias_singular(self):
        snap, prev = _parse_aliases_from_kv({"CAM5_SNAPSHOT_ALIAS": "OFS"})
        assert snap == {"OFS": "CAM5"}
        assert prev == {}

    def test_preview_alias_singular(self):
        snap, prev = _parse_aliases_from_kv({"CAM5_PREVIEW_ALIAS": "OFS"})
        assert snap == {}
        assert prev == {"OFS": "CAM5"}

    def test_comma_separated_shorthand(self):
        snap, _prev = _parse_aliases_from_kv(
            {"CAM5_SNAPSHOT_ALIAS": "OFS,of-solar,solar"}
        )
        assert snap == {"OFS": "CAM5", "of-solar": "CAM5", "solar": "CAM5"}

    def test_whitespace_around_shorthand_stripped(self):
        snap, _ = _parse_aliases_from_kv(
            {"CAM5_SNAPSHOT_ALIAS": " OFS , of-solar , solar "}
        )
        assert snap == {"OFS": "CAM5", "of-solar": "CAM5", "solar": "CAM5"}

    def test_empty_shorthand_entries_dropped(self):
        snap, _ = _parse_aliases_from_kv(
            {"CAM5_SNAPSHOT_ALIAS": "OFS,,,of-solar,"}
        )
        assert snap == {"OFS": "CAM5", "of-solar": "CAM5"}

    def test_empty_value_skipped(self):
        snap, prev = _parse_aliases_from_kv(
            {"CAM5_SNAPSHOT_ALIAS": "", "CAM6_PREVIEW_ALIAS": "   "}
        )
        assert snap == {}
        assert prev == {}

    def test_unknown_alias_kind_ignored(self):
        # CAM{N}_FOO_ALIAS doesn't match the regex → skipped.
        snap, prev = _parse_aliases_from_kv({"CAM5_FOO_ALIAS": "OFS"})
        assert snap == {}
        assert prev == {}

    def test_lowercase_alias_kind_ignored(self):
        # Regex requires uppercase suffix per the spec.
        snap, _prev = _parse_aliases_from_kv({"CAM5_snapshot_alias": "OFS"})
        assert snap == {}

    def test_multiple_cameras(self):
        snap, prev = _parse_aliases_from_kv({
            "CAM1_SNAPSHOT_ALIAS": "FDO",
            "CAM5_SNAPSHOT_ALIAS": "OFS,of-solar",
            "CAM5_PREVIEW_ALIAS": "OFS",
            "CAM6_SNAPSHOT_ALIAS": "OBS",
        })
        assert snap == {"FDO": "CAM1", "OFS": "CAM5", "of-solar": "CAM5",
                        "OBS": "CAM6"}
        assert prev == {"OFS": "CAM5"}

    def test_case_sensitive_shorthand(self):
        # The parser doesn't lowercase shorthand. Operator types OFS, not ofs.
        snap, _ = _parse_aliases_from_kv({"CAM5_SNAPSHOT_ALIAS": "OFS"})
        assert "OFS" in snap
        assert "ofs" not in snap


class TestLoadCameraAliasesIntegration:
    """End-to-end tests using real cameras.env + tmp_path overrides."""

    def test_real_cameras_env_has_all_six_cameras(self, monkeypatch):
        # Public-repo version: skip when no operator cameras.env is present.
        # (The original test loaded the operator's real cameras.env which
        # isn't shipped in the public repo.)
        import os as _os
        if not _os.path.exists("cameras.env"):
            pytest.skip("Public repo: requires operator's cameras.env (created by copying camera-creds.env.example)")
        # Integration check: when the real cameras.env is present
        # (which is true on this dev machine), we get 11+ alias
        # entries split across snapshot + preview dicts.
        monkeypatch.delenv("FARMSURV_CAMERAS_ENV", raising=False)
        snap, prev = load_camera_aliases()
        assert len(snap) >= 6
        assert len(prev) >= 6
        # CAM5 (Outside Front Solar) appears in both (it's the gatekeeper).
        assert snap.get("OFS") == "CAM5"
        assert prev.get("OFS") == "CAM5"

    def test_env_path_override(self, tmp_path):
        env_file = tmp_path / "alias_only.env"
        env_file.write_text(
            "CAM1_IP=10.0.0.1\n"
            "CAM1_NAME=Front\n"
            "CAM1_SNAPSHOT_ALIAS=FRONT,f1\n"
            "CAM1_PREVIEW_ALIAS=FRONT\n"
            "CAM2_IP=10.0.0.2\n"
            "CAM2_NAME=Back\n"
            "CAM2_SNAPSHOT_ALIAS=BACK,b2\n"
        )
        snap, prev = load_camera_aliases(env_path=str(env_file))
        assert snap == {"FRONT": "CAM1", "f1": "CAM1",
                        "BACK": "CAM2", "b2": "CAM2"}
        assert prev == {"FRONT": "CAM1"}

    def test_missing_env_file_returns_empty(self, tmp_path):
        missing = tmp_path / "does_not_exist.env"
        snap, prev = load_camera_aliases(env_path=str(missing))
        assert snap == {}
        assert prev == {}

    def test_unknown_camera_codes_warned_and_dropped(self, tmp_path):
        env_file = tmp_path / "bad_cams.env"
        env_file.write_text(
            "CAM1_IP=10.0.0.1\n"
            "CAM1_NAME=Front\n"
            "CAM1_SNAPSHOT_ALIAS=FDO\n"
            "CAM99_SNAPSHOT_ALIAS=GHOST\n"  # CAM99 not in spec list
        )
        # Capture stderr to verify the WARN line.
        err = io.StringIO()
        with redirect_stderr(err):
            snap, prev = load_camera_aliases(env_path=str(env_file))
        assert snap == {"FDO": "CAM1"}
        assert prev == {}
        assert "CAM99" in err.getvalue() or "GHOST" in err.getvalue()

    def test_empty_env_file_returns_empty(self, tmp_path):
        env_file = tmp_path / "empty.env"
        env_file.write_text("# only a comment\n\n")
        snap, prev = load_camera_aliases(env_path=str(env_file))
        assert snap == {}
        assert prev == {}
