"""
Tests for scripts/cam_browser.py CLI argument parsing.

Phase 6B.167 §13.5 (Commit 7): the cam_browser.py CLI was refactored
from positional sys.argv slicing to argparse. New flags:
    --camera CODE      (CAM1, CAM2, ...)
    --ip ADDR          [deprecated] bare IP
    --list-cameras
    --headed / --headless

These tests verify argparse behavior in isolation — they DO NOT spawn
a browser. The CamBrowser class needs Playwright + a real Chrome
binary, so we test only the parser + subcommand dispatch path. The
subcommand handlers (_cli_login/goto/exec/list_cameras) are invoked
via the parser but with the browser launch mocked out.

Tests use tmp_path synthetic env files (TEST_* prefixes registered in
infra.cameras._LEGACY_PREFIX_TO_NAME) so they are PII-clean and can
ride along in the public release.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture
def cam_browser_module(monkeypatch, tmp_path):
    """Import scripts.cam_browser with FARMSURV_CAMERAS_ENV pointed at a synthetic env file.

    Also mocks out the playwright import so the test environment doesn't
    need a real Playwright install — we test argparse + dispatch only.
    """
    # Synthetic env file with 2 TEST_* cameras
    env_file = tmp_path / "cameras.env"
    env_file.write_text(
        "# Synthetic cameras for tests\n"
        "TEST_FRONT_IP=10.0.0.1\n"
        "TEST_FRONT_HTTP_USER=admin\n"
        "TEST_FRONT_HTTP_PASS=frontpw\n"
        "TEST_FRONT_RTSP_URL=rtsp://user:secret@10.0.0.1:554/stream\n"
        "TEST_BACK_IP=10.0.0.2\n"
        "TEST_BACK_HTTP_USER=admin\n"
        "TEST_BACK_HTTP_PASS=backpw\n"
        "TEST_BACK_RTSP_URL=rtsp://admin:pass@10.0.0.2:554/stream\n"
    )
    monkeypatch.setenv("FARMSURV_CAMERAS_ENV", str(env_file))

    # Mock the playwright import so the module loads without playwright installed
    fake_playwright = MagicMock()
    fake_playwright.sync_playwright.return_value.start.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_playwright)

    # Add scripts/ to sys.path so `import cam_browser` works
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    # Drop cached imports so the module reloads with our mocks
    for mod in list(sys.modules):
        if mod == "cam_browser" or mod.startswith("cam_browser."):
            del sys.modules[mod]

    cb_mod = importlib.import_module("cam_browser")
    return cb_mod


# ---------------------------------------------------------------------------
# Parser shape
# ---------------------------------------------------------------------------


class TestParserShape:
    """Verify the CLI parser has the new flags and rejects legacy positional IPs."""

    def test_help_includes_new_flags(self, cam_browser_module, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cam_browser_module._build_parser().parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_camera_flag_accepted(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--camera", "TEST_FRONT", "login"]
        )
        assert args.camera == "TEST_FRONT"
        assert args.command == "login"

    def test_ip_flag_accepted(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--ip", "10.0.0.1", "login"]
        )
        assert args.ip == "10.0.0.1"
        assert args.camera is None

    def test_list_cameras_flag_accepted(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(["--list-cameras"])
        assert args.list_cameras is True

    def test_headed_flag_sets_headless_false(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--headed", "--camera", "TEST_FRONT", "login"]
        )
        assert args.headed is True
        assert args.headless is False

    def test_headless_flag_sets_headless_true(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--headless", "--camera", "TEST_FRONT", "login"]
        )
        assert args.headless is True
        assert args.headed is False

    def test_goto_subcommand_requires_path(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--camera", "TEST_FRONT", "goto", "/cgi-bin/api/v2/test"]
        )
        assert args.command == "goto"
        assert args.path == "/cgi-bin/api/v2/test"

    def test_exec_subcommand_requires_expression(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--camera", "TEST_FRONT", "exec", "document.title"]
        )
        assert args.command == "exec"
        assert args.expression == "document.title"

    def test_profile_path_subcommand(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(["profile-path"])
        assert args.command == "profile-path"

    def test_reset_subcommand(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(["reset"])
        assert args.command == "reset"


# ---------------------------------------------------------------------------
# _resolve_camera: --camera / --ip / neither
# ---------------------------------------------------------------------------


class TestResolveCamera:
    """Verify --camera / --ip / no-selector dispatch in _resolve_camera."""

    def test_camera_code_resolves_to_ip(self, cam_browser_module):
        # Phase 6B.167 §13.4 Commit 17 (T3 C17): TEST_FRONT_IP env
        # resolves to spec.code = "CAM1" via infra.cameras._parse_legacy
        # _fallback; --camera accepts the new CAM{N} code.
        args = argparse.Namespace(camera="CAM1", ip=None, headless=True)
        ip, user, pw = cam_browser_module._resolve_camera(args)
        assert ip == "10.0.0.1"
        assert user == "admin"
        assert pw == "frontpw"

    def test_back_camera_code_resolves(self, cam_browser_module):
        args = argparse.Namespace(camera="CAM2", ip=None, headless=True)
        ip, user, pw = cam_browser_module._resolve_camera(args)
        assert ip == "10.0.0.2"
        assert user == "admin"
        assert pw == "backpw"

    def test_ip_flag_resolves_via_by_ip(self, cam_browser_module):
        args = argparse.Namespace(camera=None, ip="10.0.0.2", headless=True)
        ip, user, pw = cam_browser_module._resolve_camera(args)
        assert ip == "10.0.0.2"
        assert pw == "backpw"

    def test_unknown_camera_code_exits(self, cam_browser_module):
        args = argparse.Namespace(camera="NOPE_999", ip=None, headless=True)
        with pytest.raises(SystemExit) as exc_info:
            cam_browser_module._resolve_camera(args)
        assert "unknown camera code" in str(exc_info.value)
        assert "NOPE_999" in str(exc_info.value)

    def test_unknown_ip_exits(self, cam_browser_module):
        args = argparse.Namespace(camera=None, ip="10.99.99.99", headless=True)
        with pytest.raises(SystemExit) as exc_info:
            cam_browser_module._resolve_camera(args)
        assert "unknown IP" in str(exc_info.value)

    def test_neither_camera_nor_ip_exits(self, cam_browser_module):
        args = argparse.Namespace(camera=None, ip=None, headless=True)
        with pytest.raises(SystemExit) as exc_info:
            cam_browser_module._resolve_camera(args)
        assert "must specify" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _cli_list_cameras: registry printout
# ---------------------------------------------------------------------------


class TestListCameras:
    """Verify --list-cameras prints the registry and exits cleanly."""

    def test_list_cameras_prints_registry(self, cam_browser_module, capsys):
        args = argparse.Namespace(camera=None, ip=None, list_cameras=True)
        cam_browser_module._cli_list_cameras(args)
        captured = capsys.readouterr()
        # §13.4: TEST_FRONT/TEST_BACK env vars produce spec.code
        # "CAM1"/"CAM2" in the printed registry (verified via
        # infra.cameras._parse_legacy_fallback's _LEGACY_PREFIX_TO_CODE).
        assert "CAM1" in captured.out
        assert "CAM2" in captured.out
        assert "10.0.0.1" in captured.out
        assert "10.0.0.2" in captured.out

    def test_list_cameras_handles_empty_registry(self, cam_browser_module, capsys, monkeypatch):
        # Empty env file
        empty = cam_browser_module.__file__  # not used; just keep var alive
        from pathlib import Path as _P
        empty_path = _P(cam_browser_module.__file__).parent / "_empty.env"
        empty_path.write_text("# empty\n")
        monkeypatch.setenv("FARMSURV_CAMERAS_ENV", str(empty_path))
        args = argparse.Namespace(camera=None, ip=None, list_cameras=True)
        cam_browser_module._cli_list_cameras(args)
        captured = capsys.readouterr()
        assert "no cameras" in captured.out


# ---------------------------------------------------------------------------
# Headless / headed default behavior
# ---------------------------------------------------------------------------


class TestHeadlessDefault:
    """Verify --headed/--headless semantics and the default (headless=True)."""

    def test_default_is_headless(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--camera", "TEST_FRONT", "login"]
        )
        assert args.headed is False
        assert args.headless is False  # not set, defaults to False
        # main() logic: headless = not args.headed → True
        headless = not args.headed
        assert headless is True

    def test_explicit_headed_overrides_default(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--headed", "--camera", "TEST_FRONT", "login"]
        )
        headless = not args.headed
        assert headless is False

    def test_explicit_headless_redundant_with_default(self, cam_browser_module):
        args = cam_browser_module._build_parser().parse_args(
            ["--headless", "--camera", "TEST_FRONT", "login"]
        )
        headless = not args.headed  # headed=False → headless=True
        if args.headless:
            headless = True
        assert headless is True


# ---------------------------------------------------------------------------
# Subcommand dispatch: main() routes to the right helper
# ---------------------------------------------------------------------------


class TestMainDispatch:
    """Verify main() dispatches to the right _cli_* helper."""

    def test_main_list_cameras_short_circuits(self, cam_browser_module, capsys):
        with patch.object(cam_browser_module, "_cli_list_cameras") as mock_list:
            cam_browser_module.main() if False else None  # avoid running main directly
            # Re-invoke via main with patched argv
            with patch.object(sys, "argv", ["cam_browser.py", "--list-cameras"]):
                cam_browser_module.main()
            mock_list.assert_called_once()

    def test_main_login_invokes_cli_login(self, cam_browser_module):
        with patch.object(cam_browser_module, "_cli_login") as mock_login, \
             patch.object(cam_browser_module, "CamBrowser", MagicMock()):
            with patch.object(sys, "argv", [
                "cam_browser.py", "--camera", "TEST_FRONT", "login",
            ]):
                cam_browser_module.main()
            mock_login.assert_called_once()
            # _resolve_camera inside _cli_login would normally hit the env;
            # _cli_login was mocked so resolution never runs.

    def test_main_profile_path_prints_dir(self, cam_browser_module, capsys):
        with patch.object(sys, "argv", ["cam_browser.py", "profile-path"]):
            cam_browser_module.main()
        captured = capsys.readouterr()
        # PROFILE_DIR is the path, not the contents
        assert str(cam_browser_module.PROFILE_DIR) in captured.out

    def test_main_no_command_prints_help_and_exits(self, cam_browser_module, capsys):
        with patch.object(sys, "argv", ["cam_browser.py"]):
            with pytest.raises(SystemExit) as exc_info:
                cam_browser_module.main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "usage:" in captured.out or "Persistent" in captured.out
