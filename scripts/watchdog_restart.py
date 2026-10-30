"""Watchdog restart helpers -- listener restart with PID-verification.

This module provides a single public entry point:

    restart_listener() -> tuple[int, str]

It is intended to be called from bin/health_watchdog.sh via
execute_code (subprocess.run), NOT from the Hermes terminal tool.

The restart path is:
  1. launchctl kickstart -k gui/<uid>/com.farm.surveillance.v2
  2. Verify PID rotated (old_pid != new_pid).
  3. If unchanged, fall back to bootout/bootstrap cycle.
  4. Return (exit_code, status_string).

Logging: attempts are logged to the listener logger if available,
otherwise stderr.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LAUNCHD_PLIST = os.path.expanduser(
    "~/Library/LaunchAgents/com.farm.surveillance.v2.plist"
)


def _get_current_pid() -> str:
    """Return the current listener PID string, or '' if not running."""
    # Try launchctl list first (most reliable on macOS).
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "com.farm.surveillance.v2":
            return parts[0].strip()
    return ""


def restart_listener(
    max_retries: int = 3,
    backoff_seconds: list[int] | None = None,
) -> tuple[int, str]:
    """Restart the listener and verify PID rotation.

    Args:
        max_retries: Max number of restart attempts (default 3).
        backoff_seconds: Seconds to wait between attempts.
            Default [30, 60, 120].

    Returns:
        (exit_code, status_string) tuple.
    """
    if backoff_seconds is None:
        backoff_seconds = [30, 60, 120]

    uid = str(os.getuid())
    old_pid = _get_current_pid()
    status_parts: list[str] = []

    for attempt in range(1, max_retries + 1):
        elapsed_before = time.time()

        # --- Attempt kickstart ---
        kickstart_cmd = [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{uid}/com.farm.surveillance.v2",
        ]
        kick_result = subprocess.run(
            kickstart_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        kick_rc = kick_result.returncode

        # If kickstart fails (rc=5 = service not found), fall back to
        # bootout/bootstrap immediately.
        if kick_rc == 5 or kick_rc != 0:
            status_parts.append(
                f"kickstart rc={kick_rc} (service not found? "
                "falling back to bootout/bootstrap)"
            )

            # Bootout/bootstrap fallback.
            bootout_cmd = [
                "launchctl",
                "bootout",
                f"gui/{uid}/com.farm.surveillance.v2",
            ]
            subprocess.run(
                bootout_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            bootstrap_cmd = [
                "launchctl",
                "bootstrap",
                f"gui/{uid}",
                LAUNCHD_PLIST,
            ]
            boot_result = subprocess.run(
                bootstrap_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            boot_rc = boot_result.returncode
            new_pid = _get_current_pid()
            rotated = old_pid and new_pid and old_pid != new_pid

            elapsed = int(time.time() - elapsed_before)
            status = (
                f"attempt {attempt}: bootout/bootstrap "
                f"rc={boot_rc} new_pid={new_pid} "
                f"rotated={rotated} elapsed={elapsed}s"
            )
            status_parts.append(status)
            _log_attempt(status)
        else:
            # Give launchd time to spawn the new process.
            time.sleep(2)
            new_pid = _get_current_pid()
            rotated = old_pid and new_pid and old_pid != new_pid

            elapsed = int(time.time() - elapsed_before)
            status = (
                f"attempt {attempt}: kickstart "
                f"rc={kick_rc} new_pid={new_pid} "
                f"rotated={rotated} elapsed={elapsed}s"
            )
            status_parts.append(status)
            _log_attempt(status)

        if rotated:
            return (0, "success: listener restarted with new PID")

        # Backoff before next attempt (if there is one).
        if attempt < max_retries:
            wait = backoff_seconds[attempt - 1] if attempt - 1 < len(backoff_seconds) else backoff_seconds[-1]
            _log_attempt(f"backoff {wait}s before attempt {attempt + 1}")
            time.sleep(wait)

    # All attempts exhausted.
    final_pid = _get_current_pid()
    return (
        1,
        (
            f"exhausted: {max_retries} attempts, "
            f"last_pid={final_pid}, "
            f"all failed"
        ),
    )


def _log_attempt(message: str) -> None:
    """Log a restart attempt to the listener logger or stderr."""
    try:
        _logger.info("watchdog_restart: %s", message)
    except (ValueError, AttributeError):
        # Listener logger may not be configured when called from watchdog.
        print(f"watchdog_restart: {message}", flush=True)
