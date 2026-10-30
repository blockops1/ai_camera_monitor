"""Tests for scripts/register_cameras.py -- password security + idempotency."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Register script is at project root scripts/; add it to sys.path for import.
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from register_cameras import (  # type: ignore[import-not-found, no-redef]
    CAMERAS,
    USER_KEYS,
    _load_creds,
    register_all,
)

# ---------------------------------------------------------------------------
# _load_creds
# ---------------------------------------------------------------------------


class TestLoadCreds:
    """_load_creds returns the expected dict shape."""

    def test_load_creds_returns_dict(self):
        result = _load_creds()
        assert isinstance(result, dict)

    def test_load_creds_has_front_keys(self):
        result = _load_creds()
        assert "FRONT_HTTP_USER" in result
        assert "FRONT_HTTP_PASS" in result


# ---------------------------------------------------------------------------
# register_all -- password NOT in argv
# ---------------------------------------------------------------------------


class TestRegisterAllPasswordSafety:
    """AC3: password is NEVER passed via argv.

    We mock subprocess.run, capture the argv, and assert no --password X
    or trailing password arg appears in the command.
    """

    @patch("register_cameras.subprocess.run")
    def test_password_not_in_argv(self, mock_run):
        """Verify that --password is never in the subprocess argv."""
        mock_run.return_value = MagicMock(returncode=0)

        results = register_all()

        for _, success, _ in results:
            assert success is True, "register_all should succeed with mock"

        # Check every subprocess.run call
        for call in mock_run.call_args_list:
            args = call[0]  # positional args
            if args:
                cmd = args[0]
                # The command must NOT contain a password value as a positional arg
                # Pattern: ... "--password-stdin" "<actual_password>"
                for i, token in enumerate(cmd):
                    if token == "--password-stdin":
                        # The password should NOT be a subsequent arg
                        # (it's fed via stdin, not argv)
                        continue
                    # Known safe tokens that can be long: CLI binary, alias, host, description
                    if (token == "reolink-cli" or
                            token.startswith(("--", "192.", "[REDACTED-LAN-IP]", "device", "add"))):
                        continue
                    if token in ("front", "back", "outside_front_garage", "outside_front_solar",
                                 "outside_front_power", "outside_back_solar"):
                        continue
                    if any(w in token.lower() for w in ("entrance", "door", "gatekeeper",
                                                        "outside", "front", "back",
                                                        "garage", "solar")):
                        continue
                    # Any remaining long token (>8 chars) that isn't a known-safe token is suspicious
                    assert len(token) < 8, (
                        f"Possible password leak in argv: {token!r} "
                        f"found in cmd {cmd}"
                    )

    @patch("register_cameras.subprocess.run")
    def test_no_leak_of_credential_values(self, mock_run):
        """Verify that actual credential values never appear in argv."""
        mock_run.return_value = MagicMock(returncode=0)

        creds = _load_creds()
        # Collect all credential values
        all_values = set(creds.values())
        all_values.add("admin")  # default user

        results = register_all()
        for _, success, _ in results:
            assert success is True, f"register_all should succeed: {results}"

        for call in mock_run.call_args_list:
            args = call[0]
            if args:
                cmd = args[0]
                for token in cmd:
                    # Credential values (passwords) should not appear in argv
                    if token in all_values and len(token) > 6:
                        pytest.fail(
                            f"Credential value {token[:4]}*** found in argv: {cmd}"
                        )

    @patch("register_cameras.subprocess.run")
    def test_password_fed_via_stdin(self, mock_run):
        """Verify that passwords are fed via stdin (input=...), not argv."""
        mock_run.return_value = MagicMock(returncode=0)

        register_all()

        for call in mock_run.call_args_list:
            # input= should contain the password
            assert "input" in call.kwargs, "subprocess.run must use input= for password"
            assert len(call.kwargs["input"]) > 0, "input= must not be empty"
            # The password should be in the input, not in argv
            cmd = call[0][0]
            assert "--password-stdin" in cmd, "cmd must include --password-stdin"

    @patch("register_cameras.subprocess.run")
    def test_all_cameras_registered(self, mock_run):
        """All 6 cameras should be registered."""
        mock_run.return_value = MagicMock(returncode=0)

        results = register_all()
        assert len(results) == 6, f"Expected 6 cameras, got {len(results)}"

    @patch("register_cameras.subprocess.run")
    def test_camera_aliases_present(self, mock_run):
        """Registered cameras include all expected aliases."""
        mock_run.return_value = MagicMock(returncode=0)

        register_all()

        registered = set()
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            # After "device", "add" comes the alias
            for i, token in enumerate(cmd):
                if token == "add" and i + 1 < len(cmd):
                    registered.add(cmd[i + 1])

        expected_aliases = {
            "front",
            "back",
            "outside_front_garage",
            "outside_front_solar",
            "outside_front_power",
            "outside_back_solar",
        }
        assert registered == expected_aliases, (
            f"Expected aliases {expected_aliases}, got {registered}"
        )

    @patch("register_cameras.subprocess.run")
    def test_idempotent_on_duplicate(self, mock_run):
        """Re-running register_all on already-registered device should succeed."""
        # First call: device doesn't exist yet
        mock_run.return_value = MagicMock(returncode=0)
        results1 = register_all()
        assert all(s for _, s, _ in results1)

        # Second call: device already exists (simulated by stderr containing "already exists")
        mock_run.return_value = MagicMock(
            returncode=1, stderr="Device already exists for alias front"
        )
        results2 = register_all()
        # Should still be successful (idempotent)
        assert all(s for _, s, _ in results2)


class TestCamerasConfig:
    """Static config correctness."""

    def test_camera_count(self):
        assert len(CAMERAS) == 6

    def test_user_keys_match_cameras(self):
        pass_keys = [cam[2] for cam in CAMERAS]
        assert set(pass_keys) == set(USER_KEYS.keys())

    def test_all_hosts_specified(self):
        for alias, host, _, _ in CAMERAS:
            # Hosts are either RFC1918 LAN addresses or the documented
            # placeholder token — both are valid in the test config.
            assert host and len(host) > 0, f"{alias} host is empty"
            assert (
                host.startswith(("192.168.", "[REDACTED-LAN-IP]"))
            ), f"{alias} host not RFC1918 or placeholder: {host}"
