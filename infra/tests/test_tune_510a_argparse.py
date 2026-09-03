"""
Tests for scripts/tune_510a_motion_sensitivity.py — CLI argparse + recipe resolution.

Phase.166 §11.87.4. Covers:
  - All 7 slider CLI flags parse correctly (--motion-sensitivity,
    --smart-person/vehicle/pet, --delay-person/vehicle/pet)
  - --dry-run flag parses (does NOT trigger apply — that's runtime)
  - --no-recipe flag parses
  - --recipe-path override parses
  - --apply, --read, --apply-all, --confirm-all still work
  - resolve_recipe_for_run() helper:
      - JSON mode (default): uses fleet + per-camera override
      - JSON mode + label: applies per-camera override on top of fleet
      - JSON mode + CLI flags: CLI values override JSON
      - --no-recipe: skips JSON, uses embedded RECIPE
      - --no-recipe + CLI flags: RECIPE + CLI overlay
      - Out-of-range CLI value raises clear error
  - apply_recipe_with(page, recipe) accepts a recipe dict parameter
    (vs the old hardcoded RECIPE reference)
  - Default behavior preserved: no flags == current behavior (uses
    embedded RECIPE when JSON matches fleet, since both have same
    values today)

These tests are mostly pure-Python (no playwright). The
apply_recipe() / read_current_state() paths require a real browser
and are smoke-tested manually, not here.
"""

import argparse
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.recipe import (
    RECIPE_KEYS,
)
from scripts import tune_510a_motion_sensitivity as t510a

# ---------------------------------------------------------------------------
# Helpers — re-extract the parser from main() for unit testing.
# We can't call main() directly because it triggers --apply flows.
# So we mirror main()'s parser construction in tests below.
# ---------------------------------------------------------------------------


def _build_parser():
    """Build a fresh argparse.ArgumentParser with the same flags as main()."""
    parser = argparse.ArgumentParser(
        description="Read or apply motion sensitivity recipe on Reolink RLC-510A cameras."
    )
    parser.add_argument("ip", nargs="?",
                        help="Camera IP address (omit with --apply-all).")
    parser.add_argument("--read", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-all", action="store_true")
    parser.add_argument("--confirm-all", action="store_true")
    parser.add_argument("--label", default="")
    parser.add_argument("--recipe", action="store_true")
    # Phase.166 §11.87.4 — new CLI flags
    parser.add_argument("--motion-sensitivity", type=int, default=None,
                        help="Override motion sensitivity (0-50).")
    parser.add_argument("--smart-person", type=int, default=None,
                        help="Override smart person (0-100).")
    parser.add_argument("--smart-vehicle", type=int, default=None,
                        help="Override smart vehicle (0-100).")
    parser.add_argument("--smart-pet", type=int, default=None,
                        help="Override smart pet (0-100).")
    parser.add_argument("--delay-person", type=int, default=None,
                        help="Override delay person (0-8 sec).")
    parser.add_argument("--delay-vehicle", type=int, default=None,
                        help="Override delay vehicle (0-8 sec).")
    parser.add_argument("--delay-pet", type=int, default=None,
                        help="Override delay pet (0-8 sec).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intended changes without applying.")
    parser.add_argument("--no-recipe", action="store_true",
                        help="Skip JSON recipe, use embedded RECIPE.")
    parser.add_argument("--recipe-path", type=str, default=None,
                        help="Path to motion recipe JSON (default: infra.paths.MOTION_RECIPE_FILE).")
    return parser


# ---------------------------------------------------------------------------
# CLI flag parsing
# ---------------------------------------------------------------------------


class TestCliFlags:
    """Verify the new flags parse cleanly with argparse."""

    def test_no_flags_parses(self):
        args = _build_parser().parse_args([])
        assert args.ip is None
        assert args.read is False
        assert args.apply is False
        assert args.apply_all is False
        assert args.motion_sensitivity is None
        assert args.smart_person is None
        assert args.smart_vehicle is None
        assert args.smart_pet is None
        assert args.delay_person is None
        assert args.delay_vehicle is None
        assert args.delay_pet is None
        assert args.dry_run is False
        assert args.no_recipe is False
        assert args.recipe_path is None

    def test_ip_alone_parses(self):
        args = _build_parser().parse_args(["192.168.1.103"])
        assert args.ip == "192.168.1.103"

    def test_read_flag_parses(self):
        args = _build_parser().parse_args(["192.168.1.103", "--read"])
        assert args.read is True
        assert args.apply is False

    def test_apply_flag_parses(self):
        args = _build_parser().parse_args(["192.168.1.103", "--apply"])
        assert args.apply is True

    def test_apply_all_requires_confirm(self):
        args = _build_parser().parse_args(["--apply-all"])
        assert args.apply_all is True
        assert args.confirm_all is False

    def test_apply_all_with_confirm_parses(self):
        args = _build_parser().parse_args(["--apply-all", "--confirm-all"])
        assert args.apply_all is True
        assert args.confirm_all is True

    def test_motion_sensitivity_flag_parses(self):
        args = _build_parser().parse_args(
            ["192.168.1.103", "--apply", "--motion-sensitivity", "40"]
        )
        assert args.motion_sensitivity == 40

    def test_all_seven_slider_flags_parse(self):
        args = _build_parser().parse_args([
            "192.168.1.103", "--apply",
            "--motion-sensitivity", "30",
            "--smart-person", "60",
            "--smart-vehicle", "40",
            "--smart-pet", "20",
            "--delay-person", "1",
            "--delay-vehicle", "3",
            "--delay-pet", "5",
        ])
        assert args.motion_sensitivity == 30
        assert args.smart_person == 60
        assert args.smart_vehicle == 40
        assert args.smart_pet == 20
        assert args.delay_person == 1
        assert args.delay_vehicle == 3
        assert args.delay_pet == 5

    def test_dry_run_flag_parses(self):
        args = _build_parser().parse_args(
            ["192.168.1.103", "--apply", "--dry-run"]
        )
        assert args.dry_run is True

    def test_no_recipe_flag_parses(self):
        args = _build_parser().parse_args(
            ["192.168.1.103", "--apply", "--no-recipe"]
        )
        assert args.no_recipe is True

    def test_recipe_path_flag_parses(self):
        args = _build_parser().parse_args(
            ["192.168.1.103", "--apply", "--recipe-path", "/tmp/custom.json"]
        )
        assert args.recipe_path == "/tmp/custom.json"

    def test_label_flag_parses(self):
        args = _build_parser().parse_args(
            ["192.168.1.103", "--apply", "--label", "CAM5"]
        )
        assert args.label == "CAM5"

    def test_recipe_print_flag_parses(self):
        args = _build_parser().parse_args(["--recipe"])
        assert args.recipe is True

    def test_negative_int_is_parsed_not_rejected(self):
        # argparse doesn't validate ranges — that's our job.
        # Verify the value flows through so we can produce a clear error.
        args = _build_parser().parse_args(
            ["192.168.1.103", "--apply", "--motion-sensitivity", "-1"]
        )
        assert args.motion_sensitivity == -1

    def test_non_int_is_rejected(self):
        # argparse with type=int rejects non-numeric input.
        with pytest.raises(SystemExit):
            _build_parser().parse_args(
                ["192.168.1.103", "--apply", "--motion-sensitivity", "abc"]
            )


# ---------------------------------------------------------------------------
# CLI flag validation (out-of-range check)
# ---------------------------------------------------------------------------


class TestCliFlagValidation:
    """Verify out-of-range CLI values are rejected with a clear error.

    argparse passes the value through; the script (or our helper)
    raises SystemExit with a clear message.
    """

    def test_motion_out_of_range_high(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            t510a._resolve_recipe_for_run(
                cli_overrides={"motion_sensitivity": 100},
                label=None,
                no_recipe=True,
                recipe_path=None,
            )
        # SystemExit is raised with code 2; the message is on stderr.
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "100" in captured.err
        assert "out of range" in captured.err.lower()
        assert "0..50" in captured.err

    def test_motion_out_of_range_low(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            t510a._resolve_recipe_for_run(
                cli_overrides={"motion_sensitivity": -1},
                label=None,
                no_recipe=True,
                recipe_path=None,
            )
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "-1" in captured.err
        assert "out of range" in captured.err.lower()

    def test_smart_out_of_range(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            t510a._resolve_recipe_for_run(
                cli_overrides={"smart_person": 200},
                label=None,
                no_recipe=True,
                recipe_path=None,
            )
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "200" in captured.err
        assert "out of range" in captured.err.lower()

    def test_delay_out_of_range(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            t510a._resolve_recipe_for_run(
                cli_overrides={"delay_vehicle": 50},
                label=None,
                no_recipe=True,
                recipe_path=None,
            )
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "50" in captured.err
        assert "out of range" in captured.err.lower()

    def test_non_recipe_key_in_cli_rejected(self, monkeypatch, capsys):
        # CLI flag typo (e.g. --smart-cars) shouldn't silently flow through.
        with pytest.raises(SystemExit) as exc_info:
            t510a._resolve_recipe_for_run(
                cli_overrides={"smart_cars": 50},
                label=None,
                no_recipe=True,
                recipe_path=None,
            )
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "smart-cars" in captured.err
        assert "not a known slider" in captured.err

    def test_unknown_cli_value_type_rejected(self, monkeypatch, capsys):
        # String value passed as int — argparse catches this earlier,
        # but if it slips through (e.g. from unit test), the helper
        # should still reject it.
        with pytest.raises(SystemExit) as exc_info:
            t510a._resolve_recipe_for_run(
                cli_overrides={"motion_sensitivity": "abc"},
                label=None,
                no_recipe=True,
                recipe_path=None,
            )
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "must be int" in captured.err


# ---------------------------------------------------------------------------
# _resolve_recipe_for_run — recipe resolution logic
# ---------------------------------------------------------------------------


class TestResolveRecipeForRun:
    """Verify the recipe resolution chain: JSON → per-camera → CLI.

    Chain (when JSON loaded):
      base = fleet
      if label in cameras: base = merge(base, cameras[label])
      for cli_key, cli_val in overrides: base[cli_key] = cli_val

    Chain (when --no-recipe):
      base = RECIPE (embedded)
      for cli_key, cli_val in overrides: base[cli_key] = cli_val
    """

    def test_no_recipe_no_overrides_returns_embedded_recipe(self):
        result = t510a._resolve_recipe_for_run(
            cli_overrides={},
            label=None,
            no_recipe=True,
            recipe_path=None,
        )
        assert result == t510a.RECIPE

    def test_no_recipe_with_override_overlays_embedded(self):
        result = t510a._resolve_recipe_for_run(
            cli_overrides={"motion_sensitivity": 35},
            label=None,
            no_recipe=True,
            recipe_path=None,
        )
        assert result["motion_sensitivity"] == 35
        # Other fields stay at embedded RECIPE
        assert result["smart_person"] == t510a.RECIPE["smart_person"]

    def test_json_mode_no_overrides_returns_fleet(self, monkeypatch):
        # Replace load_recipe with a stub that returns a known recipe.
        fake_recipe = {
            "fleet": {
                "motion_sensitivity": 25,
                "smart_person": 50,
                "smart_vehicle": 30,
                "smart_pet": 30,
                "delay_person": 0,
                "delay_vehicle": 2,
                "delay_pet": 2,
            },
            "cameras": {},
        }
        from infra import recipe as recipe_mod
        monkeypatch.setattr(recipe_mod, "load_recipe", lambda env_path=None: fake_recipe)
        result = t510a._resolve_recipe_for_run(
            cli_overrides={},
            label=None,
            no_recipe=False,
            recipe_path="/fake/path.json",
        )
        assert result == fake_recipe["fleet"]

    def test_json_mode_with_label_applies_per_camera_override(self, monkeypatch):
        fake_recipe = {
            "fleet": {
                "motion_sensitivity": 25,
                "smart_person": 50,
                "smart_vehicle": 30,
                "smart_pet": 30,
                "delay_person": 0,
                "delay_vehicle": 2,
                "delay_pet": 2,
            },
            "cameras": {
                "CAM5": {"motion_sensitivity": 40},
            },
        }
        from infra import recipe as recipe_mod
        monkeypatch.setattr(recipe_mod, "load_recipe", lambda env_path=None: fake_recipe)
        result = t510a._resolve_recipe_for_run(
            cli_overrides={},
            label="CAM5",
            no_recipe=False,
            recipe_path="/fake/path.json",
        )
        assert result["motion_sensitivity"] == 40
        assert result["smart_person"] == 50  # fleet default

    def test_json_mode_with_cli_override_takes_precedence(self, monkeypatch):
        # CLI override beats both per-camera override AND fleet.
        # Use a value in range so this tests override precedence,
        # not range validation (covered elsewhere).
        fake_recipe = {
            "fleet": {
                "motion_sensitivity": 25,
                "smart_person": 50,
                "smart_vehicle": 30,
                "smart_pet": 30,
                "delay_person": 0,
                "delay_vehicle": 2,
                "delay_pet": 2,
            },
            "cameras": {
                "CAM5": {"motion_sensitivity": 40},
            },
        }
        from infra import recipe as recipe_mod
        monkeypatch.setattr(recipe_mod, "load_recipe", lambda env_path=None: fake_recipe)
        result = t510a._resolve_recipe_for_run(
            cli_overrides={"motion_sensitivity": 45},
            label="CAM5",
            no_recipe=False,
            recipe_path="/fake/path.json",
        )
        assert result["motion_sensitivity"] == 45  # CLI wins

    def test_json_mode_with_unknown_label_falls_through_to_fleet(self, monkeypatch):
        fake_recipe = {
            "fleet": {
                "motion_sensitivity": 25,
                "smart_person": 50,
                "smart_vehicle": 30,
                "smart_pet": 30,
                "delay_person": 0,
                "delay_vehicle": 2,
                "delay_pet": 2,
            },
            "cameras": {
                "CAM5": {"motion_sensitivity": 40},
            },
        }
        from infra import recipe as recipe_mod
        monkeypatch.setattr(recipe_mod, "load_recipe", lambda env_path=None: fake_recipe)
        result = t510a._resolve_recipe_for_run(
            cli_overrides={},
            label="Unknown Camera",
            no_recipe=False,
            recipe_path="/fake/path.json",
        )
        # Unknown camera → fleet (25), not per-camera override (40)
        assert result["motion_sensitivity"] == 25

    def test_json_load_failure_with_no_cli_overrides_errors(self, monkeypatch):
        from infra import recipe as recipe_mod
        monkeypatch.setattr(
            recipe_mod, "load_recipe",
            lambda env_path=None: (_ for _ in ()).throw(FileNotFoundError("missing"))
        )
        # Missing recipe + no CLI overrides + JSON mode → SystemExit
        with pytest.raises(SystemExit):
            t510a._resolve_recipe_for_run(
                cli_overrides={},
                label=None,
                no_recipe=False,
                recipe_path="/nonexistent/path.json",
            )

    def test_json_load_failure_but_cli_overrides_present_uses_cli(self, monkeypatch, capsys):
        # If JSON fails but user passed CLI overrides, still proceed
        # with the overrides (warn the user).
        from infra import recipe as recipe_mod
        def boom(env_path=None):
            raise FileNotFoundError(f"no recipe at {env_path}")
        monkeypatch.setattr(recipe_mod, "load_recipe", boom)
        result = t510a._resolve_recipe_for_run(
            cli_overrides={"motion_sensitivity": 30},
            label=None,
            no_recipe=False,
            recipe_path="/nonexistent/path.json",
        )
        assert result["motion_sensitivity"] == 30
        # Should print a warning
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower() or "warn" in captured.err.lower()

    def test_recipe_path_passed_to_load_recipe(self, monkeypatch):
        captured_path = []
        from infra import recipe as recipe_mod
        def fake_load(env_path=None):
            captured_path.append(env_path)
            return {
                "fleet": dict.fromkeys(
                    ["motion_sensitivity", "smart_person", "smart_vehicle",
                     "smart_pet", "delay_person", "delay_vehicle", "delay_pet"],
                    0,
                ),
                "cameras": {},
            }
        monkeypatch.setattr(recipe_mod, "load_recipe", fake_load)
        t510a._resolve_recipe_for_run(
            cli_overrides={},
            label=None,
            no_recipe=False,
            recipe_path="/custom/path.json",
        )
        assert captured_path == ["/custom/path.json"]

    def test_returns_full_seven_key_dict(self):
        result = t510a._resolve_recipe_for_run(
            cli_overrides={},
            label=None,
            no_recipe=True,
            recipe_path=None,
        )
        assert set(result.keys()) == set(RECIPE_KEYS)

    def test_partial_cli_overrides_only_change_specified_keys(self):
        result = t510a._resolve_recipe_for_run(
            cli_overrides={"motion_sensitivity": 30},
            label=None,
            no_recipe=True,
            recipe_path=None,
        )
        assert result["motion_sensitivity"] == 30
        # All other keys at embedded RECIPE
        for k, v in t510a.RECIPE.items():
            if k != "motion_sensitivity":
                assert result[k] == v


# ---------------------------------------------------------------------------
# apply_recipe_with — function signature
# ---------------------------------------------------------------------------


class TestApplyRecipeWith:
    """Verify apply_recipe_with(page, recipe) accepts a recipe dict.

    The old apply_recipe(page) used module-level RECIPE. The new
    function takes a recipe parameter so callers can pass the
    resolved recipe (fleet + override + CLI).
    """

    def test_apply_recipe_with_exists(self):
        assert hasattr(t510a, "apply_recipe_with")

    def test_apply_recipe_original_still_exists_for_backcompat(self):
        # The old API should still work for callers that don't have a
        # reason to use the resolved recipe.
        assert hasattr(t510a, "apply_recipe")
        # Old signature: apply_recipe(page) — no recipe arg
        import inspect
        sig = inspect.signature(t510a.apply_recipe)
        params = list(sig.parameters.keys())
        assert "page" in params
        assert "recipe" not in params

    def test_apply_recipe_with_signature(self):
        import inspect
        sig = inspect.signature(t510a.apply_recipe_with)
        params = list(sig.parameters.keys())
        assert "page" in params
        assert "recipe" in params


# ---------------------------------------------------------------------------
# Default behavior preserved
# ---------------------------------------------------------------------------


class TestDefaultBehaviorPreserved:
    """Verify the contract: 'no flags == current apply_recipe() behavior'."""

    def test_embedded_recipe_matches_fleet_recipe_today(self):
        # Both sources of truth must agree today (they did when
        # §11.87.3 was committed; this is the safety pin).
        from infra.recipe import load_recipe
        try:
            fleet = load_recipe()["fleet"]
        except FileNotFoundError:
            pytest.skip("committed recipe file missing")
        for key, expected in t510a.RECIPE.items():
            assert fleet.get(key) == expected, (
                f"RECIPE drift: embedded RECIPE[{key}]={expected} "
                f"!= fleet[{key}]={fleet.get(key)}. "
                f"Decide which is authoritative and align."
            )

    def test_no_args_uses_embedded_recipe_via_no_recipe_path(self):
        # Default args + no_recipe=True should produce RECIPE unchanged.
        result = t510a._resolve_recipe_for_run(
            cli_overrides={},
            label=None,
            no_recipe=True,
            recipe_path=None,
        )
        assert result == t510a.RECIPE


# ===========================================================================
# Phase.167 §13.5 (Commit 8) — --camera / --list-cameras tests
# ===========================================================================

SYNTHETIC_CAMERAS_ENV = """\
# Synthetic test env file for Phase.167 Commit 8 (NEW schema)
CAM1_IP=10.10.1.21
CAM1_NAME=Front Porch
CAM1_ZONE=yard
CAM1_HTTP_USER=admin
CAM1_HTTP_PASS=secret1
CAM2_IP=10.10.1.22
CAM2_NAME=Back Yard
CAM2_ZONE=yard
CAM2_HTTP_USER=admin
CAM2_HTTP_PASS=secret2
CAM3_IP=10.10.1.23
CAM3_NAME=Side Garage
CAM3_ZONE=driveway
CAM3_HTTP_USER=admin
CAM3_HTTP_PASS=secret3
"""


def _write_synthetic_env(tmp_path, monkeypatch, content=SYNTHETIC_CAMERAS_ENV):
    """Write a synthetic cameras.env to tmp_path, point FARMSURV_CAMERAS_ENV at it."""
    env_file = tmp_path / "synthetic_cameras.env"
    env_file.write_text(content)
    monkeypatch.setenv("FARMSURV_CAMERAS_ENV", str(env_file))
    # Reset infra.cameras module cache so it picks up the new env
    import importlib
    import infra.cameras
    importlib.reload(infra.cameras)
    return env_file


class TestPhase6B167CameraFlag:
    """Commit 8: --camera / --ip / --list-cameras wiring (subprocess-based)."""

    def test_no_args_no_all_no_camera_prints_help_and_lists_cameras(self, tmp_path, monkeypatch):
        """Bare invocation (no target) prints help + camera list, exits 0.

        The script treats bare invocation as a 'show me what's available'
        hint, NOT an error. This matches scripts/cam_browser.py's contract.
        """
        env_file = _write_synthetic_env(tmp_path, monkeypatch)
        import subprocess, os
        result = subprocess.run(
            [sys.executable, "scripts/tune_510a_motion_sensitivity.py"],
            capture_output=True, text=True,
            env={**os.environ, "FARMSURV_CAMERAS_ENV": str(env_file),
                 "PYTHONPATH": os.getcwd()},
        )
        # Exit 0 + help text + camera list shown
        assert result.returncode == 0
        assert "usage:" in result.stdout
        assert "CAM1" in result.stdout
        assert "CAM2" in result.stdout
        assert "CAM3" in result.stdout


def test_phase_6b_167_list_cameras_against_synthetic_env(tmp_path, monkeypatch):
    """--list-cameras against a synthetic env shows the codes + IPs."""
    env_file = _write_synthetic_env(tmp_path, monkeypatch)
    import subprocess, os
    result = subprocess.run(
        [sys.executable, "scripts/tune_510a_motion_sensitivity.py", "--list-cameras"],
        capture_output=True, text=True,
        env={**os.environ, "FARMSURV_CAMERAS_ENV": str(env_file),
             "PYTHONPATH": os.getcwd()},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "CAM1" in result.stdout
    assert "CAM2" in result.stdout
    assert "CAM3" in result.stdout
