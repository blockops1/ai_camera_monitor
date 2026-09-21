#!/usr/bin/env python3.11
"""Register all farm cameras as reolink-cli aliases.

Reads HTTP credentials from camera-creds.env (via infra.paths.CAMERA_CREDS_FILE)
and runs ``reolink-cli device add`` with ``--password-stdin`` for each camera.

Idempotent: re-running succeeds without error or duplicate entries.

Usage::

    python scripts/register_cameras.py

Per AGENTS.md: password is NEVER passed via argv.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

# ---------------------------------------------------------------------------
# Camera definitions (alias -> (host, user, pass_key, description))
# ---------------------------------------------------------------------------

CAMERAS: list[tuple[str, str, str, str]] = [
    (
        "front",
        "192.168.1.39:9000",
        "FRONT_HTTP_PASS",
        "Front entrance",
    ),
    (
        "back",
        "192.168.1.85:9000",
        "BACK_HTTP_PASS",
        "Back door",
    ),
    (
        "outside_front_garage",
        "192.168.1.73:9000",
        "OUTSIDE_FRONT_GARAGE_HTTP_PASS",
        "Outside front garage",
    ),
    (
        "outside_front_solar",
        "192.168.1.103:9000",
        "OUTSIDE_FRONT_SOLAR_HTTP_PASS",
        "Outside front solar (gatekeeper)",
    ),
    (
        "outside_front_power",
        "192.168.1.108:9000",
        "OUTSIDE_FRONT_POWER_HTTP_PASS",
        "Outside front power",
    ),
    (
        "outside_back_solar",
        "192.168.1.113:9000",
        "OUTSIDE_BACK_SOLAR_HTTP_PASS",
        "Outside back solar",
    ),
]

# Credentials env key -> HTTP user (same user for all cameras)
USER_KEYS: dict[str, str] = {
    "FRONT_HTTP_PASS": "FRONT_HTTP_USER",
    "BACK_HTTP_PASS": "BACK_HTTP_USER",
    "OUTSIDE_FRONT_GARAGE_HTTP_PASS": "OUTSIDE_FRONT_GARAGE_HTTP_USER",
    "OUTSIDE_FRONT_SOLAR_HTTP_PASS": "OUTSIDE_FRONT_SOLAR_HTTP_USER",
    "OUTSIDE_FRONT_POWER_HTTP_PASS": "OUTSIDE_FRONT_POWER_HTTP_USER",
    "OUTSIDE_BACK_SOLAR_HTTP_PASS": "OUTSIDE_BACK_SOLAR_HTTP_USER",
}


def _load_creds(creds_file: str | None = None) -> dict[str, str | None]:
    """Load credentials from camera-creds.env."""
    path = creds_file or os.environ.get(
        "FARM_CAMERA_CREDS_FILE",
        str(Path.home() / "farm-surveillance-v2" / "camera-creds.env"),
    )
    return dotenv_values(path)


def register_all(
    creds_file: str | None = None,
    binary: str | None = None,
    dry_run: bool = False,
) -> list[tuple[str, bool, str | None]]:
    """Register all cameras via reolink-cli device add.

    Returns a list of (alias, success, error_or_None) tuples.
    """
    creds = _load_creds(creds_file)
    binary = binary or os.environ.get("FARM_REOLINK_CLI_BIN", "reolink-cli")
    results: list[tuple[str, bool, str | None]] = []

    for alias, host, pass_key, description in CAMERAS:
        password = creds.get(pass_key)
        user_key = USER_KEYS[pass_key]
        user = creds.get(user_key, "admin")

        if password is None:
            results.append((alias, False, f"Missing env key {pass_key}"))
            continue

        cmd = [binary, "device", "add", alias, "--host", host, "--user", user, "--description", description, "--password-stdin"]

        if dry_run:
            # Just log the command without running
            print(f"[dry-run] {' '.join(cmd)}")
            results.append((alias, True, None))
            continue

        try:
            proc = subprocess.run(
                cmd,
                input=password + "\n",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0:
                results.append((alias, True, None))
            else:
                # Idempotent: if device already exists, treat as success
                stderr = proc.stderr or ""
                if "already exists" in stderr.lower():
                    results.append((alias, True, None))
                else:
                    results.append((alias, False, stderr.strip()))
        except subprocess.TimeoutExpired:
            results.append((alias, False, "timeout"))
        except FileNotFoundError:
            results.append((alias, False, f"{binary} not found"))

    return results


def main() -> None:
    """Entry point: register all cameras and print a summary."""
    results = register_all()
    ok = sum(1 for _, success, _ in results if success)
    failed = len(results) - ok

    print(f"Registered {ok}/{len(results)} cameras successfully.")
    for alias, success, err in results:
        status = "OK" if success else f"FAIL: {err}"
        print(f"  {alias}: {status}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
