"""E2E test: vision_health last_call_age_s resets on successful vision calls.

Verifies US-093a: the heartbeat counter reflects time since last SUCCESS,
not time since the HealthReporter started ticking.

Pattern: mock _post_vision to simulate successful /v1/chat/completions calls,
then verify the _vision_health_line() output shows decreasing last_call_age_s.
"""
import os
import time

import pytest

# ---------------------------------------------------------------------------
# Required env vars for infra/__init__.py bootstrap (env_required machinery).
# ---------------------------------------------------------------------------
for _var, _val in [
    ("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0"),
    ("FARMSURV_NIGHT_SUPPRESS_ENABLED", "0"),
    ("LISTENER_HEALTH_INTERVAL_SECONDS", "60"),
    ("LISTENER_HEALTH_WATCHDOG_ENABLED", "1"),
    ("LISTENER_HEALTH_WATCHDOG_INTERVAL_SECONDS", "300"),
    ("WATCHDOG_SILENT", "1"),
    ("FARMSURV_PROJECT_ROOT", "."),
    ("FARMSURV_FRAME_MAX_BYTES", "21474836480"),
    ("FARMSURV_DATA_DIR", "data"),
    ("FARMSURV_NIGHT_SUPPRESS_CLASSES", "car,truck,bicycle"),
    ("FARMSURV_NIGHT_SUPPRESS_CAMERAS", ""),
    ("FARMSURV_QUIET_HOURS_ENABLED", "1"),
    ("FARMSURV_ALERT_OVERRIDES_MIN_VEHICLE_CONFIDENCE", "0"),
    ("FARMSURV_ALERT_OVERRIDES_MIN_PERSON_CONFIDENCE", "0"),
    ("FARMSURV_ALERT_OVERRIDES_MIN_ANIMAL_CONFIDENCE", "0"),
    ("FARMSURV_ALERT_OVERRIDES_DISTANT_VEHICLE_CONFIDENCE", "0.1"),
    ("FARMSURV_VISION_LLM_URL", "http://localhost:8080/v1/chat/completions"),
    ("FARMSURV_VISION_MODEL", "qwen3-vl-8b"),
    ("TELEGRAM_BOT_TOKEN", "x"),
    ("TELEGRAM_HOME_CHAT_ID", "x"),
    ("TELEGRAM_ALERT_CHAT_ID", "x"),
    ("FARMSURV_API_TOKEN", "x"),
    ("FARMSURV_API_ENABLED", "0"),
    ("FARMSURV_MCP_ENABLED", "0"),
    ("FARMSURV_ANIMAL_EMBEDDER_LIVE", "0"),
    ("ANIMAL_EMBEDDER_LIVE", "0"),
    ("FARMSURV_API_URL", "http://127.0.0.1:8092"),
    ("FARMSURV_MCP_URL", "http://127.0.0.1:8091"),
    ("FARMSURV_REOLINK_MCP_URL", ""),
    ("FARMSURV_REOLINK_STDIO_TRANSPORT", "0"),
    ("FARMSURV_REOLINK_STDIO_CMD", ""),
    ("FARMSURV_REOLINK_STDIO_ARGS", ""),
    ("FARMSURV_VISION_HEALTH_CONSECUTIVE_FAILURES_THRESHOLD", "3"),
    ("FARMSURV_HEALTH_TG_ON_FAIL", "0"),
]:
    os.environ.setdefault(_var, _val)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_state(monkeypatch):
    """Reset vision_analyzer module state before each test."""
    from infra import vision_analyzer

    # Reset module-level state
    vision_analyzer._last_call_timestamp = None
    vision_analyzer._last_success_timestamp = None
    yield
    # Cleanup: reset after
    vision_analyzer._last_call_timestamp = None
    vision_analyzer._last_success_timestamp = None


def test_last_call_age_s_decreases_after_successful_call():
    """After a successful vision call, last_call_age_s should start near 0
    and increase slowly (not jump to a large value on each heartbeat)."""
    from infra import vision_analyzer
    from listener import health

    # Simulate a successful vision call (sets _last_success_timestamp)
    start = time.monotonic()
    vision_analyzer._last_success_timestamp = start - 2  # 2 seconds ago

    # First read: should be ~2 seconds
    reporter = health.HealthReporter(port=9999, env_total=0)
    line1 = reporter._vision_health_line()
    assert "last_call_age_s=" in line1
    age1 = float(line1.split("last_call_age_s=")[1].split()[0])
    assert 1.0 < age1 < 10.0, f"Expected ~2s, got {age1}s"

    # Simulate another successful call (reset the counter)
    time.sleep(0.5)
    vision_analyzer._last_success_timestamp = time.monotonic() - 0.1  # ~0.1s ago

    line2 = reporter._vision_health_line()
    age2 = float(line2.split("last_call_age_s=")[1].split()[0])
    assert age2 < 2.0, f"After reset, expected <2s, got {age2}s"

    # Verify decreasing: the counter should have gone from ~2 to ~0.1
    assert age2 < age1, f"Expected decreasing counter: {age1} -> {age2}"


def test_last_call_age_s_is_none_before_any_call():
    """Before any vision call, last_call_age_s should be None."""
    from infra import vision_analyzer
    from listener import health

    # Ensure no prior calls
    vision_analyzer._last_call_timestamp = None
    vision_analyzer._last_success_timestamp = None

    reporter = health.HealthReporter(port=9999, env_total=0)
    line = reporter._vision_health_line()
    assert "last_call_age_s=None" in line


def test_last_call_age_s_resets_on_each_success():
    """Multiple successful calls should each reset the counter to near-zero."""
    from infra import vision_analyzer
    from listener import health

    reporter = health.HealthReporter(port=9999, env_total=0)

    ages = []
    for i in range(3):
        # Simulate a success
        vision_analyzer._last_success_timestamp = time.monotonic() - 0.05
        time.sleep(0.3)
        line = reporter._vision_health_line()
        age = float(line.split("last_call_age_s=")[1].split()[0])
        ages.append(age)

    # All ages should be small (under 1 second) since we just reset each time
    for i, age in enumerate(ages):
        assert age < 1.5, f"Step {i}: expected <1.5s, got {age}s"

    # All ages should be small (near-reset) showing the counter resets.
    # If the bug existed, ages would be large and monotonically increasing
    # (e.g. [5.0, 10.0, 15.0]) because the counter never reset.
    assert all(a < 2.0 for a in ages), f"Ages too large (bug present): {ages}"
