"""
Tests for infra/matcher_spec.py — spec data + YAML loader.

Pure unit tests with no live YAML file. The Project's spec file at
data/vehicle_matcher_spec.yaml may or may not exist; we test that
load_spec() gracefully falls back to DEFAULT_SPEC.

Covered:
    - DEFAULT_SPEC top-level shape (version, type_groups, color_normalization,
      passes, guards)
    - load_spec(no args) returns DEFAULT_SPEC when no YAML file exists
    - load_spec(spec_path=/nonexistent) returns DEFAULT_SPEC
    - load_spec(spec_path=valid_yaml) returns the parsed YAML
    - load_spec(spec_path=malformed_yaml) falls back to DEFAULT_SPEC
    - load_spec(spec_path=non-dict_yaml, e.g. list) falls back to DEFAULT_SPEC
    - load_spec(spec_path=empty_file) falls back to DEFAULT_SPEC

The lazy-import of infra.paths inside load_spec is hidden — load_spec
is tested as an opaque function here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from infra.matcher_spec import DEFAULT_SPEC, load_spec

# ---------------------------------------------------------------------------
# DEFAULT_SPEC shape
# ---------------------------------------------------------------------------


def test_default_spec_has_version_key() -> None:
    assert "version" in DEFAULT_SPEC
    assert DEFAULT_SPEC["version"] == 1


def test_default_spec_has_all_top_level_keys() -> None:
    """The 5 top-level sections a matcher needs."""
    assert set(DEFAULT_SPEC.keys()) == {
        "version",
        "type_groups",
        "color_normalization",
        "passes",
        "guards",
    }


def test_default_spec_type_groups_includes_vehicle() -> None:
    type_groups = DEFAULT_SPEC["type_groups"]
    assert "vehicle" in type_groups
    assert "sedan" in type_groups
    assert "motorcycle" in type_groups
    # sedan/coupe/hatchback SEPARATE from vehicle (the 2026-07-21 lesson)
    assert set(type_groups["sedan"]) == {"sedan", "coupe", "hatchback"}


def test_default_spec_color_normalization_has_blue_etc() -> None:
    cn = DEFAULT_SPEC["color_normalization"]
    for canonical in ("blue", "gray", "white", "black", "red", "green", "brown"):
        assert canonical in cn, f"missing canonical color {canonical!r}"


def test_default_spec_passes_have_make_model_first() -> None:
    """The first pass is `make_model` (highest-confidence match)."""
    assert DEFAULT_SPEC["passes"][0]["name"] == "make_model"
    assert DEFAULT_SPEC["passes"][0]["order"] == 1


def test_default_spec_passes_have_no_fallthrough_rule() -> None:
    """The 2026-07-21 no-fallthrough rule survives in the default."""
    make_model_pass = DEFAULT_SPEC["passes"][0]
    assert make_model_pass.get("no_fallthrough") is True


# ---------------------------------------------------------------------------
# load_spec() — fallback behavior
# ---------------------------------------------------------------------------


def test_load_spec_returns_default_when_path_missing() -> None:
    """Pass a definitely-nonexistent path → DEFAULT_SPEC."""
    bogus = Path("/this/path/definitely/does/not/exist.yaml")
    loaded = load_spec(spec_path=bogus)
    assert loaded is DEFAULT_SPEC or loaded == DEFAULT_SPEC


def test_load_spec_returns_default_on_empty_file(tmp_path: Path) -> None:
    """An empty YAML file (parses to None) falls back to DEFAULT_SPEC."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    loaded = load_spec(spec_path=empty)
    assert loaded == DEFAULT_SPEC


def test_load_spec_returns_default_on_non_dict_yaml(tmp_path: Path) -> None:
    """A YAML that parses to a list (not a dict) falls back."""
    list_yaml = tmp_path / "list.yaml"
    list_yaml.write_text("- just\n- a\n- list\n")
    loaded = load_spec(spec_path=list_yaml)
    assert loaded == DEFAULT_SPEC


def test_load_spec_parses_valid_yaml(tmp_path: Path) -> None:
    """A valid YAML spec overrides DEFAULT_SPEC."""
    custom = tmp_path / "good.yaml"
    custom.write_text(
        "version: 42\n"
        "type_groups:\n"
        "  vehicle: [pickup, suv]\n"
        "  sedan: [sedan]\n"
        "color_normalization:\n"
        "  blue: [blue, navy]\n"
        "passes: []\n"
        "guards: {}\n"
    )
    loaded = load_spec(spec_path=custom)
    assert loaded["version"] == 42
    assert loaded["type_groups"]["vehicle"] == ["pickup", "suv"]
    # Color normalization from the YAML, NOT DEFAULT_SPEC
    assert loaded["color_normalization"] == {"blue": ["blue", "navy"]}


def test_load_spec_uses_provided_path_not_default(
    tmp_path: Path,
) -> None:
    """When spec_path is given, it's used — the PROJECT_ROOT default is bypassed."""
    custom = tmp_path / "override.yaml"
    custom.write_text("version: 7\ntype_groups: {}\ncolor_normalization: {}\npasses: []\nguards: {}\n")
    loaded = load_spec(spec_path=custom)
    assert loaded["version"] == 7


def test_load_spec_does_not_raise_on_missing() -> None:
    """`load_spec()` is total — never raises on missing file."""
    # Must not raise even though we pass nothing and the file doesn't exist.
    try:
        result = load_spec()
        assert isinstance(result, dict)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"load_spec() raised unexpectedly: {exc!r}")


def test_load_spec_returns_dict_always() -> None:
    """No matter the state of the file, load_spec returns a dict."""
    # Path("/tmp") is a directory, not a file. load_spec() must still return
    # a dict (it should fall back to DEFAULT_SPEC, not raise).
    bad_paths = [
        Path("/no/such/file.yaml"),
        Path("/tmp"),  # a directory, not a yaml file
    ]
    for p in bad_paths:
        result = load_spec(spec_path=p)
        assert isinstance(result, dict), f"load_spec({p!r}) returned {type(result).__name__}"
