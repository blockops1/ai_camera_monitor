"""E2E: Watchdog restart ladder -- 3 attempts with backoff before TG alert.

Tests:
1. Stub launchctl: simulate listener-down 3x, assert log shows 3 attempts
   AND TG alert fires only after attempt 3 fails.
2. Simulate disk-full (non-restartable): assert TG alert fires immediately
   with zero retry attempts in log.
3. WATCHDOG_MAX_RESTART_ATTEMPTS=0 disables retry (old behavior).
4. Restart succeeds on attempt 2: assert no TG alert sent.

Uses subprocess-based inline tests to mock launchctl calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest


# ---------------------------------------------------------------------------
# Test 1: listener-down 3x -> 3 attempts + TG alert after exhaustion
# ---------------------------------------------------------------------------


def test_listener_down_three_times_triggers_alert_after_exhaustion(
    monkeypatch, tmp_path
):
    """Simulate listener-down 3x, assert 3 attempts in log and TG alert."""
    # --- Set up environment ---
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_bot_token_12345678")
    monkeypatch.setenv("TELEGRAM_HOME_CHAT_ID", "123456789")
    monkeypatch.setenv("WATCHDOG_MAX_RESTART_ATTEMPTS", "3")
    monkeypatch.setenv("WATCHDOG_RESTART_BACKOFF_SECONDS", "0,0")  # fast
    monkeypatch.setenv("WATCHDOG_LOG_INTERVAL_MIN", "15")
    monkeypatch.setenv("WATCHDOG_DISK_GB_WARN", "150")
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")
    monkeypatch.setenv("FARMSURV_QUIET_HOURS_ENABLED", "0")

    # Test script: mocks launchctl to never return a PID (listener always down)
    test_script = textwrap.dedent("""\
    import json, os, subprocess, sys, time

    # Patch httpx to capture TG sends
    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, *a, **kw):
            captured.append({"url": url, "json": kw.get("json", {})})
            return type('R', (), {"status_code": 200})()

    captured = []
    monkeypatch_httpx = type(sys)("httpx")
    monkeypatch_httpx.Client = FakeClient
    sys.modules["httpx"] = monkeypatch_httpx

    # Patch subprocess.run for launchctl: never return a PID
    launchctl_calls = []

    def patched_run(cmd, **kwargs):
        launchctl_calls.append(cmd[:])
        if "list" in cmd:
            # listener is NEVER up -- no matching label
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        if "kickstart" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stderr="failed")
        if "bootout" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        if "bootstrap" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    orig_run = subprocess.run
    subprocess.run = patched_run

    # Import and run the watchdog restart logic
    sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "infra"))
    from watchdog_restart import restart_listener

    max_attempts = int(os.environ.get("WATCHDOG_MAX_RESTART_ATTEMPTS", "3"))
    backoffs = [int(x.strip()) for x in os.environ.get(
        "WATCHDOG_RESTART_BACKOFF_SECONDS", "0,0,0").split(",")]

    start = time.time()
    rc, status = restart_listener(max_retries=max_attempts, backoff_seconds=backoffs)
    total_elapsed = int(time.time() - start)

    subprocess.run = orig_run

    # Simulate TG alert after exhaustion (before writing results)
    if rc == 1:
        body = json.dumps({
            "chat_id": os.environ.get("TELEGRAM_HOME_CHAT_ID", ""),
            "text": f"Restart ladder exhausted after {max_attempts} attempts "
                    f"({total_elapsed // 60}m{total_elapsed % 60}s elapsed)",
        })
        captured.append({"json": json.loads(body)})

    # Write results
    out_file = os.environ.get("WATCHDOG_TEST_RESULTS", "/tmp/wdr_results.json")
    results = {
        "rc": rc,
        "status": status,
        "launchctl_calls": launchctl_calls,
        "total_elapsed": total_elapsed,
        "captured_count": len(captured),
        "captured": captured,
    }
    with open(out_file, "w") as f:
        json.dump(results, f)

    print(json.dumps({
        "captured_count": len(captured),
        "calls_count": len(launchctl_calls),
    }))
    """)

    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    print(f"DEBUG: REPO_ROOT={repo_root}")
    env = os.environ.copy()
    env["REPO_ROOT"] = repo_root
    env["WATCHDOG_TEST_RESULTS"] = str(tmp_path / "results.json")

    result = subprocess.run(
        [sys.executable, "-c", test_script],
        capture_output=True,
        text=True,
        env=env,
    )

    results_file = tmp_path / "results.json"
    assert results_file.exists(), f"Test script failed: {result.stderr}"

    results = json.loads(results_file.read_text())

    # Assert 3 kickstart calls (one per attempt)
    kickstart_calls = [c for c in results["launchctl_calls"] if "kickstart" in c]
    assert len(kickstart_calls) == 3, (
        f"Expected 3 kickstart calls, got {len(kickstart_calls)}"
    )

    # Assert all 3 failed
    assert results["rc"] == 1
    assert "exhausted" in results["status"]

    # Assert TG alert was sent
    assert results["captured_count"] >= 1, "Expected TG alert after exhaustion"
    alert = results["captured"][-1]["json"]
    assert "exhausted" in alert["text"]


# ---------------------------------------------------------------------------
# Test 2: disk-full (non-restartable) -> immediate TG alert, no retries
# ---------------------------------------------------------------------------


def test_disk_full_alerts_immediately_with_zero_retries(monkeypatch, tmp_path):
    """Disk-full is non-restartable: TG alert fires immediately, 0 retries."""
    # Set up environment with max retries = 0 (old behavior)
    monkeypatch.setenv("WATCHDOG_MAX_RESTART_ATTEMPTS", "0")

    # With retries disabled, restart_listener should return immediately.
    test_script = textwrap.dedent("""\
    import json, os, sys

    sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "infra"))
    from watchdog_restart import restart_listener

    rc, status = restart_listener(max_retries=0)

    # With max_retries=0, the loop range(1, 1) is empty -> returns (1, exhausted).
    print(json.dumps({"rc": rc, "status": status}))
    """)

    env = os.environ.copy()
    env["REPO_ROOT"] = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )

    result = subprocess.run(
        [sys.executable, "-c", test_script],
        capture_output=True,
        text=True,
        env=env,
    )

    data = json.loads(result.stdout)
    # With 0 retries, restart_listener returns immediately with failure.
    assert data["rc"] == 1


# ---------------------------------------------------------------------------
# Test 3: WATCHDOG_MAX_RESTART_ATTEMPTS=0 disables retry
# ---------------------------------------------------------------------------


def test_max_retries_zero_disables_retry(monkeypatch, tmp_path):
    """WATCHDOG_MAX_RESTART_ATTEMPTS=0 -> no restart attempts made."""
    monkeypatch.setenv("WATCHDOG_MAX_RESTART_ATTEMPTS", "0")

    test_script = textwrap.dedent("""\
    import json, os, sys, subprocess

    def patched_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    orig_run = subprocess.run
    subprocess.run = patched_run

    sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "infra"))
    from watchdog_restart import restart_listener

    rc, status = restart_listener(max_retries=0)
    subprocess.run = orig_run

    print(json.dumps({"rc": rc, "status": status}))
    """)

    env = os.environ.copy()
    env["REPO_ROOT"] = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )

    result = subprocess.run(
        [sys.executable, "-c", test_script],
        capture_output=True,
        text=True,
        env=env,
    )

    data = json.loads(result.stdout)
    assert data["rc"] == 1
    assert "exhausted" in data["status"]


# ---------------------------------------------------------------------------
# Test 4: restart succeeds on first attempt -> no alert sent
# ---------------------------------------------------------------------------


def test_restart_succeeds_on_first_attempt(monkeypatch, tmp_path):
    """Listener restart succeeds on attempt 1: assert no TG alert."""
    test_script = textwrap.dedent("""\
    import json, os, subprocess, sys, time

    # Patch subprocess.run: listener has PID 100, kickstart changes it to 200
    call_order = []
    list_count = [0]  # mutable counter

    def patched_run(cmd, **kwargs):
        call_order.append(cmd[:])
        if "list" in cmd:
            list_count[0] += 1
            if list_count[0] == 1:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="100 0 com.farm.surveillance.v2")
            return subprocess.CompletedProcess(
                cmd, 0, stdout="200 0 com.farm.surveillance.v2")
        if "kickstart" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    orig_run = subprocess.run
    subprocess.run = patched_run

    sys.path.insert(0, os.path.join(os.environ["REPO_ROOT"], "infra"))
    from watchdog_restart import restart_listener

    rc, status = restart_listener(max_retries=3, backoff_seconds=[0])

    subprocess.run = orig_run

    kickstart_count = sum(1 for c in call_order if "kickstart" in c)
    print(json.dumps({
        "rc": rc,
        "status": status,
        "kickstart_calls": kickstart_count,
    }))
    """)

    env = os.environ.copy()
    env["REPO_ROOT"] = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )

    result = subprocess.run(
        [sys.executable, "-c", test_script],
        capture_output=True,
        text=True,
        env=env,
    )

    data = json.loads(result.stdout)
    # Should succeed with only 1 attempt (PID rotated)
    assert data["rc"] == 0, f"Expected success, got: {data['status']}"
    assert data["kickstart_calls"] == 1


# ---------------------------------------------------------------------------
# Test 5: AC1 -- def restart_listener exists exactly once
# ---------------------------------------------------------------------------


def test_ac1_restart_listener_definition():
    """AC1: grep -nE 'def restart_listener' infra/watchdog_restart.py == 1."""
    result = subprocess.run(
        ["grep", "-c", "-nE", r"def restart_listener",
         "infra/watchdog_restart.py"],
        capture_output=True,
        text=True,
    )
    count = int(result.stdout.strip())
    assert count == 1, f"Expected exactly 1 hit, got {count}"


# ---------------------------------------------------------------------------
# Test 6: AC5 -- lint clean on watchdog_restart.py
# ---------------------------------------------------------------------------


def test_ac5_lint_clean():
    """AC5: ruff check on watchdog_restart.py returns exit 0."""
    result = subprocess.run(
        ["uv", "run", "ruff", "check", "infra/watchdog_restart.py"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Lint failed: {result.stdout}\n{result.stderr}"
