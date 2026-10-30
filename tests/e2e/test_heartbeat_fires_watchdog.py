"""E2E: WatchdogLoop fires bin/health_watchdog.sh on cadence.

Tests:
1. WatchdogLoop starts and fires the script at the configured interval.
2. LISTENER_HEALTH_WATCHDOG_ENABLED=0 suppresses all fires.
3. WATCHDOG_SILENT=1 suppresses all fires (script-level off-switch).

Fixture: a temp directory with a stub script that writes a marker file.
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_watchdog_dir():
    """Create a temp dir with a stub health_watchdog.sh and return its path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        script = os.path.join(tmpdir, "health_watchdog.sh")
        marker = os.path.join(tmpdir, "watchdog_fired.marker")

        # Write a stub script that creates a marker file (mimics real script behavior).
        with open(script, "w") as f:
            f.write(
                f"#!/usr/bin/env bash\n"
                f"touch {marker}\n"
                f"echo 'watchdog pass'\n"
                f"exit 0\n"
            )
        os.chmod(script, 0o755)

        # Return (script_path, marker_path, tmpdir) so the test can also
        # override env vars like FARMSURV_PROJECT_ROOT if needed.
        yield script, marker, tmpdir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_watchdog_loop_fires_at_interval(monkeypatch, stub_watchdog_dir):
    """WatchdogLoop fires the script once at the configured interval."""
    script, marker, _ = stub_watchdog_dir

    # Override the interval to 2 seconds so the test finishes quickly.
    monkeypatch.setenv("LISTENER_HEALTH_WATCHDOG_INTERVAL_SECONDS", "2")
    # Enable the dispatcher.
    monkeypatch.setenv("LISTENER_HEALTH_WATCHDOG_ENABLED", "1")
    # Clear WATCHDOG_SILENT so the script-level gate does not block.
    monkeypatch.setenv("WATCHDOG_SILENT", "0")
    # Point _WATCHDOG_SCRIPT at our stub.
    monkeypatch.setenv("FARMSURV_PROJECT_ROOT", os.path.dirname(script))

    # We must patch the module-level constants that health.py reads at import
    # time. The cleanest approach: reload the module after setting env vars.
    # But since health.py is already imported, we monkeypatch the runtime
    # variables directly.
    import listener.health as health_mod

    monkeypatch.setattr(health_mod, "_WATCHDOG_INTERVAL_SECONDS", 2)
    monkeypatch.setattr(health_mod, "_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(health_mod, "_WATCHDOG_SCRIPT", script)

    from listener.health import _WatchdogLoop

    wl = _WatchdogLoop()
    wl.start()

    try:
        # The first fire happens after ~2 seconds. Wait up to 5 seconds.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.path.exists(marker):
                break
            time.sleep(0.2)

        assert os.path.exists(marker), (
            f"Watchdog did not fire within 5s; marker={marker} not found"
        )
    finally:
        wl.stop()


def test_watchdog_disabled_env_var_skips_fire(monkeypatch, stub_watchdog_dir):
    """LISTENER_HEALTH_WATCHDOG_ENABLED=0 suppresses all fires."""
    script, marker, _ = stub_watchdog_dir

    monkeypatch.setenv("LISTENER_HEALTH_WATCHDOG_INTERVAL_SECONDS", "2")
    monkeypatch.setenv("LISTENER_HEALTH_WATCHDOG_ENABLED", "0")
    monkeypatch.setenv("WATCHDOG_SILENT", "0")
    monkeypatch.setenv("FARMSURV_PROJECT_ROOT", os.path.dirname(script))

    import listener.health as health_mod

    monkeypatch.setattr(health_mod, "_WATCHDOG_INTERVAL_SECONDS", 2)
    monkeypatch.setattr(health_mod, "_WATCHDOG_ENABLED", False)
    monkeypatch.setattr(health_mod, "_WATCHDOG_SCRIPT", script)

    from listener.health import _WatchdogLoop

    wl = _WatchdogLoop()
    wl.start()

    # Wait past two intervals to confirm no fire.
    time.sleep(5)

    assert not os.path.exists(marker), (
        "Watchdog fired despite WATCHDOG_ENABLED=0"
    )

    wl.stop()


def test_watchdog_silent_env_var_skips_fire(monkeypatch, stub_watchdog_dir):
    """WATCHDOG_SILENT=1 suppresses all fires (script-level off-switch)."""
    script, marker, _ = stub_watchdog_dir

    monkeypatch.setenv("LISTENER_HEALTH_WATCHDOG_INTERVAL_SECONDS", "2")
    monkeypatch.setenv("LISTENER_HEALTH_WATCHDOG_ENABLED", "1")
    monkeypatch.setenv("WATCHDOG_SILENT", "1")
    monkeypatch.setenv("FARMSURV_PROJECT_ROOT", os.path.dirname(script))

    import listener.health as health_mod

    monkeypatch.setattr(health_mod, "_WATCHDOG_INTERVAL_SECONDS", 2)
    monkeypatch.setattr(health_mod, "_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(health_mod, "_WATCHDOG_SCRIPT", script)

    from listener.health import _WatchdogLoop

    wl = _WatchdogLoop()
    wl.start()

    # Wait past two intervals to confirm no fire.
    time.sleep(5)

    assert not os.path.exists(marker), (
        "Watchdog fired despite WATCHDOG_SILENT=1"
    )

    wl.stop()
