"""
Tests for scripts/read_alarm_settings.py — Phase 6B.166 §11.87.5.

These tests verify the parameterization of read_alarm_settings.py:
  - load_creds() / ALL_CAMERAS were removed; infra.camera_creds used instead
  - new flags: --label, --recipe, --recipe-path
  - JSON mode shape preserved (per proposal acceptance criterion)
  - recipe comparison produces OK/� markers per slider

The actual CamBrowser integration is NOT exercised here — these tests
monkeypatch the load_camera_creds / read_camera / print_human / recipe
helpers to avoid network access.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path("ai_camera_monitor")
SCRIPT = REPO / "scripts" / "read_alarm_settings.py"


# --- Helpers ---------------------------------------------------------------

def _load_script_module():
    """Load scripts/read_alarm_settings.py as a module via runpy."""
    import runpy
    return runpy.run_path(
        str(SCRIPT),
        run_name="__not_main__",
    )


def _sample_recipe_dict():
    """A minimal config/motion_recipe.json payload."""
    return {
        "_comment": "test fixture",
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
            "Front Porch": {
                "motion_sensitivity": 40,
                "_comment": "raised 2026-08-30",
            },
        },
    }


def _sample_cameras_dict():
    """What infra.camera_creds.load_camera_creds() returns.

    Phase 6B.167 §13.7: synthetic data only (10.0.0.x, generic labels).
    No operator IPs or operator camera names. New tests should not add
    operator data — extend _SYNTHETIC_CAMERAS (the canonical test fixture)
    or add a new fixture here, not operator IPs.
    """
    return {
        "Front Porch":      {"ip": "10.0.0.1", "user": "admin", "password": "REDACTED", "prefix": "front_porch"},
        "Back Yard":        {"ip": "10.0.0.2", "user": "admin", "password": "REDACTED", "prefix": "back_yard"},
        "Side Garage":      {"ip": "10.0.0.3", "user": "admin", "password": "REDACTED", "prefix": "side_garage"},
    }


def _sample_camera_result():
    """What read_camera() returns per camera."""
    return {
        "ip": "10.0.0.1",
        "name": "",
        "login_ok": True,
        "motion_detection": {"sliders": [{"value": 40, "min": 1, "max": 50, "y": 100, "icon": "person"}]},
        "smart_detection":  {"sliders": [
            {"value": 50, "max": 100, "y": 200, "icon": "person"},
            {"value": 30, "max": 100, "y": 240, "icon": "vehicle"},
            {"value": 30, "max": 100, "y": 280, "icon": "pet"},
        ]},
        "alarm_delay":      {"sliders": [
            {"value": 0, "max": 8, "y": 400, "icon": "person"},
            {"value": 2, "max": 8, "y": 440, "icon": "vehicle"},
            {"value": 2, "max": 8, "y": 480, "icon": "pet"},
        ]},
        "object_size": {"set_up_buttons": []},
        "raw_inputs": [],
        "error": None,
    }


# --- Tests ----------------------------------------------------------------

class TestLocalCredsRemoved:
    """Verify that the script-local load_creds() and ALL_CAMERAS were removed."""

    def test_load_creds_is_gone(self):
        mod = _load_script_module()
        assert not hasattr(mod, "load_creds"), (
            "scripts/read_alarm_settings.py must NOT export load_creds(); "
            "use infra.camera_creds.get_http_password() instead (§11.87.5)."
        )

    def test_all_cameras_constant_is_gone(self):
        mod = _load_script_module()
        assert not hasattr(mod, "ALL_CAMERAS"), (
            "scripts/read_alarm_settings.py must NOT export ALL_CAMERAS; "
            "use infra.camera_creds.load_camera_creds() instead (§11.87.5)."
        )

    def test_no_local_password_loading(self):
        mod = _load_script_module()
        # Should not have a get_password_for helper either
        assert not hasattr(mod, "get_password_for"), (
            "scripts/read_alarm_settings.py must NOT have a get_password_for() "
            "helper; use infra.camera_creds.get_http_password() instead."
        )


class TestArgparseFlagsPresent:
    """Verify the new CLI flags were added."""

    def test_help_shows_new_flags(self, capsys):
        """--help must list --label, --recipe, --recipe-path."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, cwd=str(REPO),
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)},
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout + result.stderr
        assert "--label" in out, f"--help missing --label flag:\n{out}"
        assert "--recipe" in out, f"--help missing --recipe flag:\n{out}"
        assert "--recipe-path" in out, f"--help missing --recipe-path flag:\n{out}"

    def test_existing_flags_preserved(self):
        """--json, --headed, ip positional must still exist."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, cwd=str(REPO),
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)},
        )
        out = result.stdout + result.stderr
        assert "--json" in out
        assert "--headed" in out
        # positional 'ip'
        assert "ip" in out


class TestArgparseFlagParse:
    """Verify flags parse correctly when invoked."""

    def test_ip_positional_parsed(self):
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human"), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.1", "--json"]):
            mock_load.return_value = _sample_cameras_dict()
            mock_read.return_value = _sample_camera_result()
            with mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
                with pytest.raises(SystemExit) as exc:
                    rmod.main()
                assert exc.value.code == 0

    def test_no_args_falls_back_to_all_cameras(self):
        """Without ip, script should iterate all from load_camera_creds."""
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human"), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "--json"]):
            mock_load.return_value = _sample_cameras_dict()
            mock_read.return_value = _sample_camera_result()
            with mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
                with pytest.raises(SystemExit) as exc:
                    rmod.main()
                # 3 synthetic cameras × 1 read each
                assert mock_read.call_count == 3
                assert exc.value.code == 0

    def test_unknown_ip_exits_2(self, capsys):
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.99.99"]):
            mock_load.return_value = _sample_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                rmod.main()
            assert exc.value.code == 2

    def test_empty_creds_file_exits_2(self, capsys):
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "--json"]):
            mock_load.return_value = {}  # empty
            with pytest.raises(SystemExit) as exc:
                rmod.main()
            assert exc.value.code == 2


class TestRecipeFlag:
    """Verify --recipe integrates infra.recipe.resolve_for_camera()."""

    def test_recipe_default_off(self):
        """Without --recipe, print_human is called with recipe=None."""
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human") as mock_print, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.1"]):
            mock_load.return_value = _sample_cameras_dict()
            mock_read.return_value = _sample_camera_result()
            with mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
                with pytest.raises(SystemExit):
                    rmod.main()
                for call in mock_print.call_args_list:
                    assert call.kwargs.get("recipe") is None, (
                        "Without --recipe, recipe kwarg must be None (backward-compat)"
                    )

    def test_recipe_on_calls_resolve_for_camera(self):
        """With --recipe, resolve_for_camera() is called with the right label."""
        import infra.camera_creds as cc_mod
        import infra.recipe as recipe_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human") as mock_print, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.1", "--recipe"]):
            mock_load.return_value = _sample_cameras_dict()
            mock_read.return_value = _sample_camera_result()
            with mock.patch.object(recipe_mod, "load_recipe") as mock_load_recipe, \
                 mock.patch.object(recipe_mod, "resolve_for_camera") as mock_resolve, \
                 mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
                mock_load_recipe.return_value = _sample_recipe_dict()
                mock_resolve.return_value = {
                    "motion_sensitivity": 40,
                    "smart_person": 50,
                    "smart_vehicle": 30,
                    "smart_pet": 30,
                    "delay_person": 0,
                    "delay_vehicle": 2,
                    "delay_pet": 2,
                }
                with pytest.raises(SystemExit):
                    rmod.main()
                # Should call resolve_for_camera once for CAM5
                assert mock_resolve.call_count == 1
                args, kwargs = mock_resolve.call_args
                lookup_label = kwargs.get("label") or args[0]
                assert lookup_label == "Front Porch"
                # And print_human should get a recipe kwarg that's not None
                for call in mock_print.call_args_list:
                    assert call.kwargs.get("recipe") is not None

    def test_recipe_path_override(self):
        """--recipe-path should be passed to load_recipe()."""
        import infra.camera_creds as cc_mod
        import infra.recipe as recipe_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human"), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.1", "--recipe", "--recipe-path", "/tmp/custom_recipe.json"]):
            mock_load.return_value = _sample_cameras_dict()
            mock_read.return_value = _sample_camera_result()
            with mock.patch.object(recipe_mod, "load_recipe") as mock_load_recipe, \
                 mock.patch.object(recipe_mod, "resolve_for_camera"), \
                 mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
                mock_load_recipe.return_value = _sample_recipe_dict()
                with pytest.raises(SystemExit):
                    rmod.main()
                _args, kwargs = mock_load_recipe.call_args
                assert kwargs.get("env_path") == "/tmp/custom_recipe.json"

    def test_label_flag_overrides_derived_label(self):
        """--label "Custom" should be used for recipe lookup, not the auto-derived."""
        import infra.camera_creds as cc_mod
        import infra.recipe as recipe_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human"), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.1", "--recipe", "--label", "Custom Name"]):
            mock_load.return_value = _sample_cameras_dict()
            mock_read.return_value = _sample_camera_result()
            with mock.patch.object(recipe_mod, "load_recipe") as mock_load_recipe, \
                 mock.patch.object(recipe_mod, "resolve_for_camera") as mock_resolve, \
                 mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
                mock_load_recipe.return_value = _sample_recipe_dict()
                mock_resolve.return_value = {}
                with pytest.raises(SystemExit):
                    rmod.main()
                args, kwargs = mock_resolve.call_args
                lookup_label = kwargs.get("label") or args[0]
                assert lookup_label == "Custom Name", (
                    f"--label flag must override the auto-derived label; got {lookup_label}"
                )

    def test_recipe_load_failure_exits_2(self, capsys):
        """If load_recipe raises, exit 2."""
        import infra.camera_creds as cc_mod
        import infra.recipe as recipe_mod
        from infra.recipe import RecipeLoadError
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.1", "--recipe"]):
            mock_load.return_value = _sample_cameras_dict()
            with mock.patch.object(recipe_mod, "load_recipe") as mock_load_recipe:
                mock_load_recipe.side_effect = RecipeLoadError("test failure")
                with pytest.raises(SystemExit) as exc:
                    rmod.main()
                assert exc.value.code == 2


class TestJsonOutputShapePreserved:
    """Acceptance criterion from PROPOSAL: --json shape identical to old."""

    def test_json_shape_has_required_keys(self, capsys):
        """Each result dict must have all required keys."""
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.1", "--json"]), \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
            mock_load.return_value = _sample_cameras_dict()
            mock_read.return_value = _sample_camera_result()
            with pytest.raises(SystemExit):
                rmod.main()

        captured = capsys.readouterr()
        # stdout should be valid JSON
        out = [l for l in captured.out.splitlines() if l.strip()]
        parsed = json.loads("\n".join(out))
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        r = parsed[0]
        # Required keys from old script
        for key in ("ip", "name", "login_ok", "motion_detection",
                    "smart_detection", "alarm_delay", "object_size",
                    "raw_inputs", "error"):
            assert key in r, f"--json output missing key {key!r}"

    def test_recipe_flag_does_not_alter_json_output(self, capsys):
        """--recipe should only affect human output, not JSON."""
        import infra.camera_creds as cc_mod
        import infra.recipe as recipe_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.1", "--recipe", "--json"]):
            mock_load.return_value = _sample_cameras_dict()
            mock_read.return_value = _sample_camera_result()
            with mock.patch.object(recipe_mod, "load_recipe") as mock_load_recipe, \
                 mock.patch.object(recipe_mod, "resolve_for_camera"), \
                 mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
                mock_load_recipe.return_value = _sample_recipe_dict()
                with pytest.raises(SystemExit):
                    rmod.main()
        captured = capsys.readouterr()
        out = [l for l in captured.out.splitlines() if l.strip()]
        parsed = json.loads("\n".join(out))
        # No `recipe` key should leak into the result dict
        r = parsed[0]
        assert "recipe" not in r, (
            "--recipe should not add a key to the JSON output; it's human-output-only"
        )


class TestPrintHumanRecipeDiff:
    """Verify print_human() shows OK/✗ markers per slider."""

    def test_diff_marker_shows_mismatch(self, capsys):
        from scripts import read_alarm_settings as rmod
        # recipe says motion=25, camera reports 40 → should show DIFF
        recipe = {
            "motion_sensitivity": 25,
            "smart_person": 50,
            "smart_vehicle": 30,
            "smart_pet": 30,
            "delay_person": 0,
            "delay_vehicle": 2,
            "delay_pet": 2,
        }
        rmod.print_human("Front Porch", _sample_camera_result(), recipe=recipe)
        captured = capsys.readouterr()
        assert "DIFF" in captured.out, (
            f"Mismatch should produce DIFF marker; output:\n{captured.out}"
        )

    def test_ok_marker_when_match(self, capsys):
        from scripts import read_alarm_settings as rmod
        recipe = {
            "motion_sensitivity": 40,  # matches the sample
            "smart_person": 50,
            "smart_vehicle": 30,
            "smart_pet": 30,
            "delay_person": 0,
            "delay_vehicle": 2,
            "delay_et": 2,
        }
        rmod.print_human("Front Porch", _sample_camera_result(), recipe=recipe)
        captured = capsys.readouterr()
        assert "ALL VALUES MATCH RECIPE." in captured.out, (
            f"All-match should produce 'ALL VALUES MATCH RECIPE.'; output:\n{captured.out}"
        )

    def test_no_recipe_means_no_diff_marker(self, capsys):
        from scripts import read_alarm_settings as rmod
        rmod.print_human("Front Porch", _sample_camera_result())  # no recipe
        captured = capsys.readouterr()
        assert "DIFF" not in captured.out, (
            f"No recipe → no DIFF marker; output:\n{captured.out}"
        )
        assert "ALL VALUES MATCH" not in captured.out

    def test_recipe_with_unknown_keys_ignored(self, capsys):
        """Recipe may have extra keys; only the 7 known keys get diffed."""
        from scripts import read_alarm_settings as rmod
        recipe = {
            "motion_sensitivity": 40,
            "smart_person": 50,
            "smart_vehicle": 30,
            "smart_pet": 30,
            "delay_person": 0,
            "delay_vehicle": 2,
            "delay_pet": 2,
            "unknown_key": 999,
            "_comment": "ignored",
        }
        # Should not crash
        rmod.print_human("Front Porch", _sample_camera_result(), recipe=recipe)
        captured = capsys.readouterr()
        assert "ALL VALUES MATCH RECIPE." in captured.out

    def test_login_failure_short_circuits(self, capsys):
        """When login_ok=False, only the LOGIN FAILED line should print."""
        from scripts import read_alarm_settings as rmod
        bad = dict(_sample_camera_result())
        bad["login_ok"] = False
        bad["error"] = "auth refused"
        rmod.print_human("Bad Camera", bad)
        captured = capsys.readouterr()
        assert "LOGIN FAILED" in captured.out
        assert "Motion Detection:" not in captured.out  # didn't try to render sliders


class TestCameraListSource:
    """Verify camera list comes from infra.camera_creds, not hardcoded."""

    def test_cameras_dict_used_as_iteration_source(self, capsys):
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        # Use only 1 camera instead of 4 to prove list comes from load_camera_creds
        single = {"Test Cam": {"ip": "10.0.0.1", "user": "admin", "password": "REDACTED", "prefix": "test_cam"}}
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human"), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "--json"]), \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"):
            mock_load.return_value = single
            mock_read.return_value = _sample_camera_result()
            with pytest.raises(SystemExit):
                rmod.main()
            # Only 1 camera read (not 4 or 6 from the old hardcoded list)
            assert mock_read.call_count == 1, (
                f"Expected 1 read (single camera from load_camera_creds); got {mock_read.call_count}"
            )


class TestImportsAreClean:
    """Sanity-check the import surface."""

    def test_source_references_infra_paths(self):
        """The script source should reference CAMERA_CREDS_FILE / MOTION_RECIPE_FILE."""
        src = SCRIPT.read_text()
        assert "from infra.paths import" in src, (
            "read_alarm_settings.py should import CAMERA_CREDS_FILE from infra.paths"
        )
        assert "CAMERA_CREDS_FILE" in src
        assert "MOTION_RECIPE_FILE" in src


# ============================================================================
# Phase 6B.167 §13.4 — `--camera` + `--list-cameras` flags
#
# These tests use SYNTHETIC fixtures (CAM1/CAM2/CAM3, 10.0.0.x, generic labels)
# so they ship safely with the public repo. The legacy operator-data fixtures
# above are preserved for backwards-compat until Commit 4 (§13.4) scrubs them.
# ============================================================================


def _synthetic_cameras_dict():
    """Generic cameras — no operator PII. Order = CAM1, CAM2, CAM3."""
    return {
        "Front Porch":      {"ip": "10.0.0.1", "user": "admin", "password": "x", "prefix": "front_porch"},
        "Back Yard":        {"ip": "10.0.0.2", "user": "admin", "password": "x", "prefix": "back_yard"},
        "Side Garage":      {"ip": "10.0.0.3", "user": "admin", "password": "x", "prefix": "side_garage"},
    }


def _synthetic_camera_result(ip="10.0.0.1"):
    return {
        "ip": ip,
        "name": "",
        "login_ok": True,
        "motion_detection": {"sliders": []},
        "smart_detection":  {"sliders": []},
        "alarm_delay":      {"sliders": []},
        "object_size": {"set_up_buttons": []},
        "raw_inputs": [],
        "error": None,
    }


class TestPhase6B167CameraFlag:
    """Phase 6B.167 §13.4 — `--camera <label>` filters to a single camera."""

    def test_camera_flag_present_in_help(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, cwd=str(REPO),
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)},
        )
        assert result.returncode == 0
        out = result.stdout + result.stderr
        assert "--camera" in out, "--help must list --camera"
        assert "--list-cameras" in out, "--help must list --list-cameras"

    def test_list_cameras_prints_cam1_cam2_cam3(self, capsys):
        """--list-cameras emits {code, label, ip} in declaration order."""
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "--list-cameras"]):
            mock_load.return_value = _synthetic_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                rmod.main()
            assert exc.value.code == 0
        out = capsys.readouterr().out
        listing = json.loads(out)
        assert listing == [
            {"code": "CAM1", "label": "Front Porch", "ip": "10.0.0.1"},
            {"code": "CAM2", "label": "Back Yard",   "ip": "10.0.0.2"},
            {"code": "CAM3", "label": "Side Garage", "ip": "10.0.0.3"},
        ]

    def test_camera_label_filters_to_single(self):
        """`--camera 'Front Porch'` runs the script on one camera only."""
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human"), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "--camera", "Front Porch", "--json"]):
            mock_load.return_value = _synthetic_cameras_dict()
            mock_read.return_value = _synthetic_camera_result("10.0.0.1")
            with mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="x"):
                with pytest.raises(SystemExit) as exc:
                    rmod.main()
                assert exc.value.code == 0
                assert mock_read.call_count == 1
                # The single camera read should be 10.0.0.1 (Front Porch)
                assert mock_read.call_args.args[0] == "10.0.0.1"

    def test_camera_ip_filters_to_single(self):
        """`--camera 10.0.0.2` filters by IP — same code path."""
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human"), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "--camera", "10.0.0.2", "--json"]):
            mock_load.return_value = _synthetic_cameras_dict()
            mock_read.return_value = _synthetic_camera_result("10.0.0.2")
            with mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="x"):
                with pytest.raises(SystemExit) as exc:
                    rmod.main()
                assert exc.value.code == 0
                assert mock_read.call_count == 1
                assert mock_read.call_args.args[0] == "10.0.0.2"

    def test_unknown_camera_exits_2(self, capsys):
        """`--camera bogus` exits 2 with a helpful message including --list-cameras."""
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(SCRIPT), "--camera", "Bogus", "--json"]):
            mock_load.return_value = _synthetic_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                rmod.main()
            assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Bogus" in err
        assert "--list-cameras" in err  # hints at the diagnostic flag

    def test_camera_flag_beats_deprecated_ip_positional(self):
        """When both are given, --camera wins (operator migration path)."""
        import infra.camera_creds as cc_mod
        from scripts import read_alarm_settings as rmod
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(rmod, "read_camera") as mock_read, \
             mock.patch.object(rmod, "print_human"), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "10.0.0.3", "--camera", "Front Porch", "--json"]):
            mock_load.return_value = _synthetic_cameras_dict()
            mock_read.return_value = _synthetic_camera_result("10.0.0.1")
            with mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
                 mock.patch.object(cc_mod, "get_http_password", return_value="x"):
                with pytest.raises(SystemExit) as exc:
                    rmod.main()
                assert exc.value.code == 0
                # --camera 'Front Porch' (10.0.0.1) wins over positional 10.0.0.3
                assert mock_read.call_count == 1
                assert mock_read.call_args.args[0] == "10.0.0.1"
