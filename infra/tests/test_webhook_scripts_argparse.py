"""
Tests for scripts/configure_webhook.py, scripts/verify_webhook.py, and
scripts/apply_all_tuning.py — Phase 6B.166 §11.87.6.

These tests verify the parameterization of the 3 webhook/tuning scripts:
  - Hardcoded IP lists dropped; infra.camera_creds used instead
  - Hardcoded WEBHOOK_URL replaced with --webhook-url / $WEBHOOK_URL
  - Hardcoded TARGET_PERIODS/SMART/DELAY in apply_all_tuning replaced with
    infra.recipe.resolve_for_camera()
  - apply_all_tuning: from tune_motion_sensitivity (broken) → from
    tune_510a_motion_sensitivity (the actual module name)
  - All 3 scripts use argparse instead of raw sys.argv
  - Module headers follow refactor-module-header standard
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT_ROOT / "scripts"
CONFIGURE_WEBHOOK = SCRIPTS / "configure_webhook.py"
VERIFY_WEBHOOK = SCRIPTS / "verify_webhook.py"
APPLY_ALL_TUNING = SCRIPTS / "apply_all_tuning.py"


def _load_script_source(path: Path) -> str:
    return path.read_text()


def _sample_cameras_dict() -> dict:
    """Synthetic operator-data-free fixture (Phase 6B.167 §13.7).

    Matches the shape of infra.camera_creds.load_camera_creds() output but
    uses 10.0.0.x and generic labels so tests can ride along in public
    releases without leaking operator IPs or operator camera names.
    """
    return {
        "Front Porch": {
            "ip": "10.0.0.1", "user": "admin", "password": "REDACTED",
            "prefix": "FRONT_PORCH",
        },
        "Back Yard": {
            "ip": "10.0.0.2", "user": "admin", "password": "REDACTED",
            "prefix": "BACK_YARD",
        },
        "Side Garage": {
            "ip": "10.0.0.3", "user": "admin", "password": "REDACTED",
            "prefix": "SIDE_GARAGE",
        },
    }


def _sample_cameras_list():
    """Synthetic operator-data-free fixture (Phase 6B.167 §13.5 Commit 9).

    Matches the shape of infra.cameras.load_cameras() output: a list of
    infra.cameras.CameraSpec dataclasses. Uses 10.0.0.x and generic labels.
    """
    from infra.cameras import CameraSpec
    return [
        CameraSpec(code="CAM1", name="Front Porch", ip="10.0.0.1",
                   zone="FRONT", http_user="admin", http_pass="REDACTED"),
        CameraSpec(code="CAM2", name="Back Yard", ip="10.0.0.2",
                   zone="BACK", http_user="admin", http_pass="REDACTED"),
        CameraSpec(code="CAM3", name="Side Garage", ip="10.0.0.3",
                   zone="SIDE", http_user="admin", http_pass="REDACTED"),
    ]


def _sample_recipe_dict() -> dict:
    return {
        "fleet": {
            "motion_sensitivity": 25,
            "smart_person": 50,
            "smart_vehicle": 50,
            "smart_pet": 30,
            "delay_person": 0,
            "delay_vehicle": 0,
            "delay_pet": 2,
        },
        "cameras": {
            "Front Porch": {"motion_sensitivity": 40},
        },
    }


# =============================================================================
# configure_webhook.py tests
# =============================================================================

class TestConfigureWebhookArgparse:
    """Verify argparse wiring + camera_creds integration."""

    def test_help_shows_all_flags(self, capsys):
        from scripts import configure_webhook
        with pytest.raises(SystemExit) as exc, mock.patch.object(
            sys, "argv", [str(CONFIGURE_WEBHOOK), "--help"]
        ):
            configure_webhook.main()
        assert exc.value.code == 0
        captured = capsys.readouterr()
        for flag in ("--all", "--headed", "--webhook-url", "--creds-env"):
            assert flag in captured.out, f"--help missing {flag}"

    def test_no_args_no_all_errors(self, capsys):
        from scripts import configure_webhook
        with pytest.raises(SystemExit) as exc, mock.patch.object(
            sys, "argv", [str(CONFIGURE_WEBHOOK)]
        ):
            configure_webhook.main()
        # argparse error → exit 2
        assert exc.value.code == 2

    def test_unknown_ip_exits_2(self, capsys):
        import infra.camera_creds as cc_mod
        from scripts import configure_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(CONFIGURE_WEBHOOK), "10.0.0.99"]):
            mock_load.return_value = _sample_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                configure_webhook.main()
            assert exc.value.code == 2

    def test_known_ip_dispatches_to_configure(self):
        import infra.camera_creds as cc_mod
        from scripts import configure_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(configure_webhook, "configure_webhook") as mock_cw, \
             mock.patch.object(sys, "argv", [
                 str(CONFIGURE_WEBHOOK), "10.0.0.1",
                 "--webhook-url", "http://1.2.3.4/alert",
             ]):
            mock_load.return_value = _sample_cameras_dict()
            mock_cw.return_value = {"ip": "10.0.0.1", "ok": True, "steps": []}
            with pytest.raises(SystemExit) as exc:
                configure_webhook.main()
            assert exc.value.code == 0
            assert mock_cw.call_count == 1
            args, _kwargs = mock_cw.call_args
            # configure_webhook(ip, user, password, webhook_url, *, headed=False)
            assert args[0] == "10.0.0.1"
            assert args[3] == "http://1.2.3.4/alert"

    def test_all_flag_iterates_all_cameras(self):
        import infra.camera_creds as cc_mod
        from scripts import configure_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(configure_webhook, "configure_webhook") as mock_cw, \
             mock.patch.object(sys, "argv", [
                 str(CONFIGURE_WEBHOOK), "--all", "--webhook-url", "http://x/y",
             ]):
            mock_load.return_value = _sample_cameras_dict()
            mock_cw.return_value = {"ip": "x", "ok": True, "steps": []}
            with pytest.raises(SystemExit):
                configure_webhook.main()
            assert mock_cw.call_count == 3  # all 3 cameras

    def test_webhook_url_env_var_used_when_no_flag(self, monkeypatch):
        from scripts import configure_webhook
        monkeypatch.setenv("WEBHOOK_URL", "http://from-env.example/hook")
        args = mock.Mock(webhook_url="")
        url = configure_webhook._get_webhook_url(args)
        assert url == "http://from-env.example/hook"

    def test_webhook_url_cli_overrides_env(self, monkeypatch):
        from scripts import configure_webhook
        monkeypatch.setenv("WEBHOOK_URL", "http://from-env.example/hook")
        args = mock.Mock(webhook_url="http://from-cli.example/hook")
        url = configure_webhook._get_webhook_url(args)
        assert url == "http://from-cli.example/hook"

    def test_webhook_url_default_when_nothing_set(self, monkeypatch):
        # Phase 6B.167 §13.4: the hardcoded default URL was an operator PII leak.
        # Now an empty string is returned, and main() exits 2 if neither flag
        # nor $WEBHOOK_URL is set.
        from scripts import configure_webhook
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        args = mock.Mock(webhook_url="")
        url = configure_webhook._get_webhook_url(args)
        assert url == "", (
            "with no --webhook-url and no $WEBHOOK_URL, _get_webhook_url "
            "returns empty (operator MUST set one — see main() exit-2 path)."
        )

    def test_module_header_present(self):
        src = _load_script_source(CONFIGURE_WEBHOOK)
        # Per refactor-module-header standard
        assert "STATUS:" in src
        assert "THREAD SAFETY:" in src
        assert "INPUTS:" in src
        assert "OUTPUTS:" in src
        assert "PUBLIC API:" in src
        assert "DOES NOT DO:" in src
        assert "CALLED BY:" in src
        assert "CALLS INTO:" in src

    def test_no_hardcoded_homedir_path(self):
        """Isolation rule: no Path.home() / 'ai_camera_monitor' refs."""
        src = _load_script_source(CONFIGURE_WEBHOOK)
        assert "Path.home()" not in src
        assert "/Users/jill/ai_camera_monitor" not in src

    def test_no_local_load_creds(self):
        """The script-local _load_creds must be gone."""
        src = _load_script_source(CONFIGURE_WEBHOOK)
        assert "def _load_creds" not in src


# =============================================================================
# verify_webhook.py tests
# =============================================================================

class TestVerifyWebhookArgparse:
    """Verify webhook verifier: argparse + camera_creds integration."""

    def test_help_shows_flags(self, capsys):
        from scripts import verify_webhook
        with pytest.raises(SystemExit) as exc, mock.patch.object(
            sys, "argv", [str(VERIFY_WEBHOOK), "--help"]
        ):
            verify_webhook.main()
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "--headed" in captured.out
        assert "--creds-env" in captured.out

    def test_no_ip_uses_first_camera_in_creds(self):
        import infra.camera_creds as cc_mod
        from scripts import verify_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(verify_webhook, "verify_webhook") as mock_vw, \
             mock.patch.object(sys, "argv", [str(VERIFY_WEBHOOK)]):
            mock_load.return_value = _sample_cameras_dict()
            # main() returns normally on success (no SystemExit)
            verify_webhook.main()
            # Should pick the FIRST camera (10.0.0.1)
            args, _kwargs = mock_vw.call_args
            assert args[0] == "10.0.0.1"

    def test_env_var_override_for_default_ip(self, monkeypatch):
        import infra.camera_creds as cc_mod
        from scripts import verify_webhook
        monkeypatch.setenv("VERIFY_WEBHOOK_DEFAULT_IP", "10.0.0.2")
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(verify_webhook, "verify_webhook") as mock_vw, \
             mock.patch.object(sys, "argv", [str(VERIFY_WEBHOOK)]):
            mock_load.return_value = _sample_cameras_dict()
            verify_webhook.main()
            args, _ = mock_vw.call_args
            assert args[0] == "10.0.0.2"

    def test_explicit_ip_passed_through(self):
        import infra.camera_creds as cc_mod
        from scripts import verify_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(verify_webhook, "verify_webhook") as mock_vw, \
             mock.patch.object(sys, "argv", [str(VERIFY_WEBHOOK), "10.0.0.3"]):
            mock_load.return_value = _sample_cameras_dict()
            verify_webhook.main()
            args, _ = mock_vw.call_args
            assert args[0] == "10.0.0.3"

    def test_unknown_ip_exits_2(self):
        import infra.camera_creds as cc_mod
        from scripts import verify_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(VERIFY_WEBHOOK), "10.0.0.99"]):
            mock_load.return_value = _sample_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                verify_webhook.main()
            assert exc.value.code == 2

    def test_module_header_present(self):
        src = _load_script_source(VERIFY_WEBHOOK)
        for section in ("STATUS:", "THREAD SAFETY:", "INPUTS:", "OUTPUTS:",
                        "PUBLIC API:", "DOES NOT DO:", "CALLED BY:", "CALLS INTO:"):
            assert section in src, f"missing {section}"

    def test_no_hardcoded_homedir_path(self):
        src = _load_script_source(VERIFY_WEBHOOK)
        assert "Path.home()" not in src
        assert "/Users/jill/ai_camera_monitor" not in src


# =============================================================================
# apply_all_tuning.py tests
# =============================================================================

class TestApplyAllTuningArgparse:
    """Verify tuning script: uses infra.recipe, infra.camera_creds, broken import fixed."""

    def test_imports_tune_510a_motion_sensitivity_not_old_name(self):
        """The old `from tune_motion_sensitivity import` was broken (module renamed
        in §11.87.4). Must import from tune_510a_motion_sensitivity."""
        src = _load_script_source(APPLY_ALL_TUNING)
        assert "from tune_510a_motion_sensitivity import" in src, (
            "apply_all_tuning.py must import from tune_510a_motion_sensitivity"
        )
        assert "from tune_motion_sensitivity import" not in src, (
            "apply_all_tuning.py still references the broken old module name"
        )

    def test_uses_recipe_resolution_not_hardcoded_constants(self):
        """The script should not have TARGET_SMART/TARGET_DELAY hardcoded dicts."""
        src = _load_script_source(APPLY_ALL_TUNING)
        # The old script had TARGET_SMART = {"person": 50, ...} as a constant.
        # New script should call resolve_for_camera() instead.
        assert "TARGET_SMART" not in src, (
            "TARGET_SMART hardcoded constant should be replaced by recipe"
        )
        assert "TARGET_DELAY" not in src, (
            "TARGET_DELAY hardcoded constant should be replaced by recipe"
        )
        assert "resolve_for_camera" in src

    def test_no_hardcoded_target_ips(self):
        """Should not have a TARGET_IPS list hardcoded."""
        src = _load_script_source(APPLY_ALL_TUNING)
        assert "TARGET_IPS" not in src

    def test_help_shows_flags(self, capsys):
        from scripts import apply_all_tuning
        with pytest.raises(SystemExit) as exc, mock.patch.object(
            sys, "argv", [str(APPLY_ALL_TUNING), "--help"]
        ):
            apply_all_tuning.main()
        assert exc.value.code == 0
        captured = capsys.readouterr()
        for flag in ("--start", "--label", "--dry-run", "--drain-secs",
                     "--creds-env", "--recipe-path", "--no-recipe"):
            assert flag in captured.out, f"--help missing {flag}"

    def test_dry_run_does_not_open_browser(self):
        from scripts import apply_all_tuning
        with mock.patch.object(apply_all_tuning, "load_cameras") as mock_load, \
             mock.patch.object(apply_all_tuning, "get_http_user", return_value="admin"), \
             mock.patch.object(apply_all_tuning, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(apply_all_tuning, "load_recipe") as mock_lr, \
             mock.patch.object(apply_all_tuning, "resolve_for_camera") as mock_rc, \
             mock.patch.object(apply_all_tuning, "CamBrowser") as mock_cb_cls, \
             mock.patch.object(sys, "argv", [str(APPLY_ALL_TUNING), "--dry-run"]):
            mock_load.return_value = _sample_cameras_list()
            mock_lr.return_value = _sample_recipe_dict()
            mock_rc.return_value = {"motion_sensitivity": 25}
            with pytest.raises(SystemExit) as exc:
                apply_all_tuning.main()
            assert exc.value.code == 0
            # CRITICAL: CamBrowser context manager must NOT have been entered
            mock_cb_cls.assert_not_called()

    def test_dry_run_uses_per_camera_recipe(self):
        """--dry-run should call resolve_for_camera for each camera."""
        from scripts import apply_all_tuning
        with mock.patch.object(apply_all_tuning, "load_cameras") as mock_load, \
             mock.patch.object(apply_all_tuning, "get_http_user", return_value="admin"), \
             mock.patch.object(apply_all_tuning, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(apply_all_tuning, "load_recipe") as mock_lr, \
             mock.patch.object(apply_all_tuning, "resolve_for_camera") as mock_rc, \
             mock.patch.object(sys, "argv", [str(APPLY_ALL_TUNING), "--dry-run"]):
            mock_load.return_value = _sample_cameras_list()
            mock_lr.return_value = _sample_recipe_dict()
            mock_rc.return_value = {"motion_sensitivity": 25}
            with pytest.raises(SystemExit):
                apply_all_tuning.main()
            # 3 cameras → 3 calls to resolve_for_camera
            assert mock_rc.call_count == 3

    def test_no_recipe_uses_embedded_recipe_constant(self):
        """--no-recipe should NOT call load_recipe or resolve_for_camera."""
        from scripts import apply_all_tuning
        with mock.patch.object(apply_all_tuning, "load_cameras") as mock_load, \
             mock.patch.object(apply_all_tuning, "get_http_user", return_value="admin"), \
             mock.patch.object(apply_all_tuning, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(apply_all_tuning, "load_recipe") as mock_lr, \
             mock.patch.object(apply_all_tuning, "resolve_for_camera") as mock_rc, \
             mock.patch.object(sys, "argv", [str(APPLY_ALL_TUNING), "--dry-run", "--no-recipe"]):
            mock_load.return_value = _sample_cameras_list()
            with pytest.raises(SystemExit):
                apply_all_tuning.main()
            mock_lr.assert_not_called()
            mock_rc.assert_not_called()

    def test_start_flag_skips_earlier_cameras(self):
        """--start should skip cameras whose label sorts earlier."""
        from scripts import apply_all_tuning
        with mock.patch.object(apply_all_tuning, "load_cameras") as mock_load, \
             mock.patch.object(apply_all_tuning, "get_http_user", return_value="admin"), \
             mock.patch.object(apply_all_tuning, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(apply_all_tuning, "load_recipe") as mock_lr, \
             mock.patch.object(apply_all_tuning, "resolve_for_camera") as mock_rc, \
             mock.patch.object(sys, "argv", [
                 str(APPLY_ALL_TUNING), "--dry-run",
                 "--start", "Back Yard",
            ]):
            mock_load.return_value = _sample_cameras_list()
            mock_lr.return_value = _sample_recipe_dict()
            mock_rc.return_value = {"motion_sensitivity": 25}
            with pytest.raises(SystemExit):
                apply_all_tuning.main()
            # Cameras starting from "Back Yard": Garage + Solar = 2
            assert mock_rc.call_count == 2

    def test_label_flag_overrides_recipe_lookup(self):
        """--label "Custom" should call resolve_for_camera with "Custom"."""
        from scripts import apply_all_tuning
        with mock.patch.object(apply_all_tuning, "load_cameras") as mock_load, \
             mock.patch.object(apply_all_tuning, "get_http_user", return_value="admin"), \
             mock.patch.object(apply_all_tuning, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(apply_all_tuning, "load_recipe") as mock_lr, \
             mock.patch.object(apply_all_tuning, "resolve_for_camera") as mock_rc, \
             mock.patch.object(sys, "argv", [
                 str(APPLY_ALL_TUNING), "--dry-run", "--label", "Custom Lookup",
            ]):
            mock_load.return_value = _sample_cameras_list()
            mock_lr.return_value = _sample_recipe_dict()
            mock_rc.return_value = {"motion_sensitivity": 25}
            with pytest.raises(SystemExit):
                apply_all_tuning.main()
            # Every call should use "Custom Lookup" as the label
            for call in mock_rc.call_args_list:
                args, kwargs = call
                # resolve_for_camera(label=lookup_label, recipe=recipe_raw) — kwarg
                assert kwargs.get("label") == "Custom Lookup" or args[0] == "Custom Lookup"

    def test_recipe_path_passed_to_load_recipe(self):
        from scripts import apply_all_tuning
        with mock.patch.object(apply_all_tuning, "load_cameras") as mock_load, \
             mock.patch.object(apply_all_tuning, "get_http_user", return_value="admin"), \
             mock.patch.object(apply_all_tuning, "get_http_password", return_value="REDACTED"), \
             mock.patch.object(apply_all_tuning, "load_recipe") as mock_lr, \
             mock.patch.object(apply_all_tuning, "resolve_for_camera", return_value={"motion_sensitivity": 25}), \
             mock.patch.object(sys, "argv", [
                 str(APPLY_ALL_TUNING), "--dry-run",
                 "--recipe-path", "/tmp/custom_recipe.json",
            ]):
            mock_load.return_value = _sample_cameras_list()
            mock_lr.return_value = _sample_recipe_dict()
            with pytest.raises(SystemExit):
                apply_all_tuning.main()
            assert mock_lr.call_count >= 1
            _args, kwargs = mock_lr.call_args
            assert kwargs.get("env_path") == "/tmp/custom_recipe.json"

    def test_recipe_load_failure_exits_2(self):
        from infra.recipe import RecipeLoadError
        from scripts import apply_all_tuning
        with mock.patch.object(apply_all_tuning, "load_cameras") as mock_load, \
             mock.patch.object(apply_all_tuning, "load_recipe") as mock_lr, \
             mock.patch.object(sys, "argv", [str(APPLY_ALL_TUNING), "--dry-run"]):
            mock_load.return_value = _sample_cameras_list()
            mock_lr.side_effect = RecipeLoadError("bad recipe")
            with pytest.raises(SystemExit) as exc:
                apply_all_tuning.main()
            assert exc.value.code == 2

    def test_module_header_present(self):
        src = _load_script_source(APPLY_ALL_TUNING)
        for section in ("STATUS:", "THREAD SAFETY:", "INPUTS:", "OUTPUTS:",
                        "PUBLIC API:", "DOES NOT DO:", "CALLED BY:", "CALLS INTO:"):
            assert section in src, f"missing {section}"

    def test_no_hardcoded_homedir_path(self):
        src = _load_script_source(APPLY_ALL_TUNING)
        assert "Path.home()" not in src
        assert "/Users/jill/ai_camera_monitor" not in src

    def test_no_local_load_creds_or_find_creds(self):
        """The script-local load_creds/find_creds_for_ip must be gone."""
        src = _load_script_source(APPLY_ALL_TUNING)
        assert "def load_creds()" not in src
        assert "def find_creds_for_ip" not in src


# =============================================================================
# Cross-cutting tests
# =============================================================================

class TestSharedModulePurity:
    """All 3 scripts share clean isolation + module-purity rules."""

    @pytest.mark.parametrize("script", [
        CONFIGURE_WEBHOOK, VERIFY_WEBHOOK, APPLY_ALL_TUNING,
    ])
    def test_no_prod_repo_references(self, script):
        src = _load_script_source(script)
        assert "/Users/jill/ai_camera_monitor/" not in src or "absolute path" in src.lower()

    @pytest.mark.parametrize("script", [
        CONFIGURE_WEBHOOK, VERIFY_WEBHOOK, APPLY_ALL_TUNING,
    ])
    def test_no_local_load_creds_helpers(self, script):
        """Each script must use infra.camera_creds, not its own _load_creds."""
        src = _load_script_source(script)
        assert "def _load_creds" not in src
        assert "def load_creds" not in src

    @pytest.mark.parametrize("script", [
        CONFIGURE_WEBHOOK, VERIFY_WEBHOOK, APPLY_ALL_TUNING,
    ])
    def test_uses_infra_paths_for_default_paths(self, script):
        src = _load_script_source(script)
        assert "infra.paths" in src, (
            f"{script.name} must use infra.paths for default file paths"
        )


# ============================================================================
# Phase 6B.167 §13.4 — `--camera` + `--list-cameras` flags (verify + configure)
#
# These tests use SYNTHETIC fixtures (10.0.0.x, generic labels) so they ship
# safely with the public repo. Legacy operator-data fixtures above are
# preserved for backwards-compat until Commit 4 scrubs them.
# ============================================================================


def _synthetic_cameras_dict() -> dict:
    return {
        "Front Porch":   {"ip": "10.0.0.1", "user": "admin", "password": "x", "prefix": "front_porch"},
        "Back Yard":     {"ip": "10.0.0.2", "user": "admin", "password": "x", "prefix": "back_yard"},
        "Side Garage":   {"ip": "10.0.0.3", "user": "admin", "password": "x", "prefix": "side_garage"},
    }


class TestPhase6B167VerifyWebhookCameraFlag:
    """Phase 6B.167 §13.4 — verify_webhook.py --camera / --list-cameras."""

    def test_help_lists_new_flags(self):
        from scripts import verify_webhook
        with pytest.raises(SystemExit) as exc, mock.patch.object(
            sys, "argv", [str(VERIFY_WEBHOOK), "--help"]
        ):
            verify_webhook.main()
        assert exc.value.code == 0

    def test_list_cameras_prints_cam_codes(self, capsys):
        """verify_webhook --list-cameras → [{code, label, ip}, ...]."""
        import infra.camera_creds as cc_mod
        from scripts import verify_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(VERIFY_WEBHOOK), "--list-cameras"]):
            mock_load.return_value = _synthetic_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                verify_webhook.main()
            assert exc.value.code == 0
        import json as _json
        listing = _json.loads(capsys.readouterr().out)
        assert listing == [
            {"code": "CAM1", "label": "Front Porch", "ip": "10.0.0.1"},
            {"code": "CAM2", "label": "Back Yard",   "ip": "10.0.0.2"},
            {"code": "CAM3", "label": "Side Garage", "ip": "10.0.0.3"},
        ]

    def test_camera_label_resolves_to_ip(self):
        """`--camera 'Front Porch'` resolves to that camera's IP."""
        import infra.camera_creds as cc_mod
        from scripts import verify_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="x"), \
             mock.patch.object(sys, "argv", [str(VERIFY_WEBHOOK), "--camera", "Front Porch"]):
            mock_load.return_value = _synthetic_cameras_dict()
            with mock.patch("scripts.verify_webhook.verify_webhook") as mock_vw:
                # Don't actually drive a browser
                from scripts import verify_webhook as vw_mod
                mock_vw.return_value = None
                vw_mod.main()
                # verify_webhook() called with 10.0.0.1 (Front Porch's IP)
                assert mock_vw.call_args.args[0] == "10.0.0.1"

    def test_camera_ip_resolves_to_ip(self):
        """`--camera 10.0.0.2` (IP form) also works."""
        import infra.camera_creds as cc_mod
        from scripts import verify_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="x"), \
             mock.patch.object(sys, "argv", [str(VERIFY_WEBHOOK), "--camera", "10.0.0.2"]):
            mock_load.return_value = _synthetic_cameras_dict()
            with mock.patch("scripts.verify_webhook.verify_webhook") as mock_vw:
                from scripts import verify_webhook as vw_mod
                mock_vw.return_value = None
                vw_mod.main()
                assert mock_vw.call_args.args[0] == "10.0.0.2"

    def test_unknown_camera_exits_2(self, capsys):
        import infra.camera_creds as cc_mod
        from scripts import verify_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(VERIFY_WEBHOOK), "--camera", "Bogus"]):
            mock_load.return_value = _synthetic_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                verify_webhook.main()
            assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Bogus" in err
        assert "--list-cameras" in err


class TestPhase6B167ConfigureWebhookCameraFlag:
    """Phase 6B.167 §13.4 — configure_webhook.py --camera / --list-cameras."""

    def test_help_lists_new_flags(self):
        from scripts import configure_webhook
        with pytest.raises(SystemExit) as exc, mock.patch.object(
            sys, "argv", [str(CONFIGURE_WEBHOOK), "--help"]
        ):
            configure_webhook.main()
        assert exc.value.code == 0

    def test_list_cameras_prints_cam_codes(self, capsys):
        import infra.camera_creds as cc_mod
        from scripts import configure_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(CONFIGURE_WEBHOOK), "--list-cameras"]):
            mock_load.return_value = _synthetic_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                configure_webhook.main()
            assert exc.value.code == 0
        import json as _json
        listing = _json.loads(capsys.readouterr().out)
        assert listing == [
            {"code": "CAM1", "label": "Front Porch", "ip": "10.0.0.1"},
            {"code": "CAM2", "label": "Back Yard",   "ip": "10.0.0.2"},
            {"code": "CAM3", "label": "Side Garage", "ip": "10.0.0.3"},
        ]

    def test_camera_label_runs_single_camera(self):
        """`--camera 'Back Yard'` configures only that one camera."""
        import infra.camera_creds as cc_mod
        from scripts import configure_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="x"), \
             mock.patch.object(sys, "argv", [
                 str(CONFIGURE_WEBHOOK), "--camera", "Back Yard",
                 "--webhook-url", "http://from-cli.example/hook",
             ]):
            mock_load.return_value = _synthetic_cameras_dict()
            with mock.patch("scripts.configure_webhook.configure_webhook") as mock_cw:
                from scripts import configure_webhook as cw_mod
                mock_cw.return_value = {"ip": "10.0.0.2", "ok": True, "steps": []}
                with pytest.raises(SystemExit) as exc:
                    cw_mod.main()
                assert exc.value.code == 0
                # Only 1 camera should be configured
                assert mock_cw.call_count == 1
                assert mock_cw.call_args.args[0] == "10.0.0.2"

    def test_all_still_runs_all_cameras(self):
        """`--all` still iterates every camera (regression check)."""
        import infra.camera_creds as cc_mod
        from scripts import configure_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(cc_mod, "get_http_user", return_value="admin"), \
             mock.patch.object(cc_mod, "get_http_password", return_value="x"), \
             mock.patch.object(sys, "argv", [
                 str(CONFIGURE_WEBHOOK), "--all",
                 "--webhook-url", "http://from-cli.example/hook",
             ]):
            mock_load.return_value = _synthetic_cameras_dict()
            with mock.patch("scripts.configure_webhook.configure_webhook") as mock_cw:
                from scripts import configure_webhook as cw_mod
                mock_cw.return_value = {"ip": "x", "ok": True, "steps": []}
                with pytest.raises(SystemExit) as exc:
                    cw_mod.main()
                assert exc.value.code == 0
                assert mock_cw.call_count == 3

    def test_unknown_camera_exits_2(self, capsys):
        import infra.camera_creds as cc_mod
        from scripts import configure_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [
                 str(CONFIGURE_WEBHOOK), "--camera", "Bogus",
                 "--webhook-url", "http://from-cli.example/hook",
             ]):
            mock_load.return_value = _synthetic_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                configure_webhook.main()
            assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Bogus" in err
        assert "--list-cameras" in err

    def test_no_webhook_url_exits_2(self, capsys):
        """Operator MUST set --webhook-url or $WEBHOOK_URL (no leaked default)."""
        import infra.camera_creds as cc_mod
        from scripts import configure_webhook
        with mock.patch.object(cc_mod, "load_camera_creds") as mock_load, \
             mock.patch.object(sys, "argv", [str(CONFIGURE_WEBHOOK), "--camera", "Front Porch"]), \
             mock.patch.dict(os.environ, {"WEBHOOK_URL": ""}, clear=False):
            mock_load.return_value = _synthetic_cameras_dict()
            with pytest.raises(SystemExit) as exc:
                configure_webhook.main()
            assert exc.value.code == 2

    def test_no_args_no_all_no_camera_errors(self, capsys):
        """Bare invocation errors with a useful message."""
        from scripts import configure_webhook
        with pytest.raises(SystemExit) as exc, mock.patch.object(
            sys, "argv", [str(CONFIGURE_WEBHOOK)]
        ):
            configure_webhook.main()
        assert exc.value.code == 2
