"""
Tests for infra/camera_aliases.py — canonical camera-name resolution.

Covers:
    - resolve_camera_name: alias → canonical, no alias → unchanged,
      case sensitivity, empty string
    - CAMERA_NAME_ALIASES: shape + immutability expectations

Phase.167 §13.4 Commit 17 (T3 C17): alias map is empty by design.
All tests below were rewritten to reflect that CAM{N} migration
removed operator-flavored friendly-name aliasing.
"""


from infra.camera_aliases import CAMERA_NAME_ALIASES, resolve_camera_name


class TestResolveCameraName:
    """Verify the alias → canonical mapping (always identity now)."""

    def test_legacy_alias_resolves_to_canonical(self):
        # §13.4: empty alias map — any input returns itself.
        assert resolve_camera_name("Front Corner Inside") == "Front Corner Inside"

    def test_canonical_name_unchanged(self):
        # Canonical names pass through (and so does anything else).
        assert resolve_camera_name("Front Door Outside") == "Front Door Outside"
        assert resolve_camera_name("Outside Back Solar") == "Outside Back Solar"

    def test_unknown_name_unchanged(self):
        # Unknown name returns as-is.
        assert resolve_camera_name("Some Random Camera") == "Some Random Camera"

    def test_case_sensitive(self):
        # The alias map is case-sensitive — lowercase variants don't resolve.
        assert resolve_camera_name("front corner inside") == "front corner inside"
        assert resolve_camera_name("FRONT CORNER INSIDE") == "FRONT CORNER INSIDE"

    def test_empty_string_unchanged(self):
        # Empty input returns empty (no alias match).
        assert resolve_camera_name("") == ""


class TestCameraNameAliasesConstant:
    """Verify the module-level alias map is empty post-§13.4."""

    def test_aliases_is_dict(self):
        assert isinstance(CAMERA_NAME_ALIASES, dict)

    def test_aliases_is_empty_post_13_4(self):
        # §13.4 migration: alias map is empty.
        assert CAMERA_NAME_ALIASES == {}

    def test_aliases_canonical_values_are_real_cameras(self):
        # §13.4: no values to validate; vacuously true.
        for value in CAMERA_NAME_ALIASES.values():
            assert value and isinstance(value, str)
            assert "_" not in value, f"Canonical name {value!r} uses underscores"
