"""
test_cooldown.py — Per-behavior pins for infra.cooldown.

Phase 6B.101 — pins for is_in_vision_block_cooldown (new), plus coverage
for the existing is_in_cooldown / is_in_bucket_cooldown / make_bucket_key
/ clear_all_cooldowns (no existing test file).

Per the test-serves-design rule (maintainer 2026-08-19): pin behavior, not
implementation. These tests use small windows (1-2 s) and call clear_all_cooldowns
to isolate state. If the function shape changes, the tests will need to
be updated — that's correct, not fragile.
"""

from __future__ import annotations

import pytest

from infra import cooldown
from infra.cooldown import (
    DEFAULT_BUCKET_COOLDOWN,
    DEFAULT_VISION_BLOCK_COOLDOWN,
    clear_all_cooldowns,
    is_in_bucket_cooldown,
    is_in_cooldown,
    is_in_vision_block_cooldown,
    make_bucket_key,
)


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    """Drop all three cooldown maps before each test.

    The cooldown module is in-memory global state; without this fixture
    the tests would carry state between cases and time-dependent tests
    would be flaky.
    """
    clear_all_cooldowns()
    yield
    clear_all_cooldowns()


# -----------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------


def test_vision_block_default_is_30_minutes():
    """Default must match maintainer's request: 30 min (1800 s)."""
    assert DEFAULT_VISION_BLOCK_COOLDOWN == 1800


# -----------------------------------------------------------------------
# is_in_vision_block_cooldown — the new global rate-limit
# -----------------------------------------------------------------------


def test_vision_block_first_call_returns_false_and_records():
    """First call after clear must return False (not in cooldown) and
    record the timestamp so the next call within the window returns True."""
    assert is_in_vision_block_cooldown() is False
    # Second call within the (long) default window: still False because
    # the first call set the timestamp, so a 3rd call within 1800s
    # should be True. Two calls back-to-back: 2nd is True (recorded already).
    assert is_in_vision_block_cooldown() is True


def test_vision_block_global_across_alerts():
    """Phase 6B.101 requirement: the rate-limit is GLOBAL, not per-alert.

    Simulate two unrelated alerts hitting the rate-limit back-to-back.
    First call: False (send). Second call: True (suppress), regardless
    of any "alert id" — there is no alert id here. The map is keyed
    by a single global "_last_sent" key.
    """
    assert is_in_vision_block_cooldown() is False  # 1st alert — send
    assert is_in_vision_block_cooldown() is True   # 2nd alert — suppress
    assert is_in_vision_block_cooldown() is True   # 3rd alert — suppress
    assert is_in_vision_block_cooldown() is True   # 4th alert — suppress


def test_vision_block_window_resets_after_expiry():
    """After the cooldown window elapses, the next call returns False.

    Use a 1-second window for fast testing.
    """
    assert is_in_vision_block_cooldown(cooldown_seconds=1) is False  # record t0
    import time
    time.sleep(1.1)
    assert is_in_vision_block_cooldown(cooldown_seconds=1) is False  # window expired


def test_vision_block_does_not_share_state_with_other_cooldowns():
    """Hitting the vision-block cooldown must not affect the alert or
    bucket cooldowns — they are independent maps."""
    # Fire the vision block twice; second is suppressed.
    assert is_in_vision_block_cooldown() is False
    assert is_in_vision_block_cooldown() is True

    # Alert-level cooldown still works fresh.
    assert is_in_cooldown("alert-A", cooldown_seconds=60) is False
    assert is_in_cooldown("alert-A", cooldown_seconds=60) is True

    # Bucket-level cooldown still works fresh for a different key.
    assert is_in_bucket_cooldown("FrontDoor:Normal", bucket_cooldown_seconds=60) is False
    assert is_in_bucket_cooldown("FrontDoor:Normal", bucket_cooldown_seconds=60) is True


def test_clear_all_cooldowns_resets_vision_block():
    """clear_all_cooldowns() must reset the vision-block map so the next
    call returns False (send, not suppress)."""
    assert is_in_vision_block_cooldown() is False  # record
    assert is_in_vision_block_cooldown() is True   # suppressed
    clear_all_cooldowns()
    assert is_in_vision_block_cooldown() is False  # reset — send again


# -----------------------------------------------------------------------
# Existing is_in_cooldown (per-alert_id) — pins
# -----------------------------------------------------------------------


def test_is_in_cooldown_first_call_false_then_true():
    assert is_in_cooldown("a-123", cooldown_seconds=60) is False
    assert is_in_cooldown("a-123", cooldown_seconds=60) is True


def test_is_in_cooldown_different_alert_ids_are_independent():
    assert is_in_cooldown("a-1", cooldown_seconds=60) is False
    assert is_in_cooldown("a-2", cooldown_seconds=60) is False  # different id
    assert is_in_cooldown("a-1", cooldown_seconds=60) is True   # a-1 still in cooldown


# -----------------------------------------------------------------------
# Existing is_in_bucket_cooldown — pins
# -----------------------------------------------------------------------


def test_bucket_cooldown_empty_key_returns_false():
    """Empty key means "no bucket" — caller skipped the check.
    Must NOT record a timestamp."""
    assert is_in_bucket_cooldown("", bucket_cooldown_seconds=60) is False
    assert is_in_bucket_cooldown("", bucket_cooldown_seconds=60) is False  # still no record


def test_bucket_cooldown_keys_are_independent():
    assert is_in_bucket_cooldown("Cam1:Title1", bucket_cooldown_seconds=60) is False
    assert is_in_bucket_cooldown("Cam2:Title1", bucket_cooldown_seconds=60) is False
    assert is_in_bucket_cooldown("Cam1:Title2", bucket_cooldown_seconds=60) is False


# -----------------------------------------------------------------------
# Existing make_bucket_key
# -----------------------------------------------------------------------


def test_make_bucket_key_uses_camera_and_title_prefix():
    alert = {"camera_name": "CAM5", "title": "Tractor Operation Detected"}
    key = make_bucket_key(alert)
    # "Tractor Operation Detected" is 26 chars — fits fully in the 30-char prefix.
    assert key == "CAM5:Tractor Operation Detected"


def test_make_bucket_key_handles_alternate_field_names():
    """Camera can be 'camera_name' OR 'camera'; title can be 'title' OR 'alert_title'."""
    a1 = {"camera_name": "X", "title": "Hello World"}
    a2 = {"camera": "X", "alert_title": "Hello World"}
    a3 = {"camera_name": "X", "alert_title": "Hello World"}
    a4 = {"camera": "X", "title": "Hello World"}
    assert make_bucket_key(a1) == make_bucket_key(a2) == make_bucket_key(a3) == make_bucket_key(a4)


def test_make_bucket_key_empty_when_missing_required_fields():
    assert make_bucket_key({}) == ""
    assert make_bucket_key({"camera_name": "X"}) == ""
    assert make_bucket_key({"title": "T"}) == ""


# -----------------------------------------------------------------------
# Module default for the (existing) bucket cooldown
# -----------------------------------------------------------------------


def test_default_bucket_cooldown_is_30_minutes():
    """Existing default must not regress."""
    assert DEFAULT_BUCKET_COOLDOWN == 1800


# -----------------------------------------------------------------------
# Module existence — proves the symbol is exposed where callers expect it
# -----------------------------------------------------------------------


def test_module_exports_vision_block_function():
    """The new function must be importable from infra.cooldown (not just
    infra.notifier). Ensures future callers can use it without a notifier
    dependency."""
    assert hasattr(cooldown, "is_in_vision_block_cooldown")
    assert callable(cooldown.is_in_vision_block_cooldown)



# ---------------------------------------------------------------------------
# MotionCooldown (Phase 6B.108) — extracted from listener.py
# ---------------------------------------------------------------------------
# Per AGENTS.md Step 4.5, tests pin behavior not implementation. These tests
# use a freshly-instantiated MotionCooldown per test (not the module
# singleton MOTION_COOLDOWN) so they don't interfere with the listener's
# running singleton state.


def test_motion_cooldown_is_cool_false_for_unseen_key():
    """An unseen key is not cooled."""
    from infra.cooldown import MotionCooldown
    mc = MotionCooldown(window_seconds=60)
    assert mc.is_cool(("CAM3", "2026-08-21T14:00")) is False


def test_motion_cooldown_mark_then_is_cool_true():
    """After mark(), is_cool returns True within the window."""
    from infra.cooldown import MotionCooldown
    mc = MotionCooldown(window_seconds=60)
    key = ("CAM3", "2026-08-21T14:00")
    mc.mark(key)
    assert mc.is_cool(key) is True


def test_motion_cooldown_different_keys_independent():
    """Two different keys do not share cooldown state."""
    from infra.cooldown import MotionCooldown
    mc = MotionCooldown(window_seconds=60)
    mc.mark(("CAM3", "2026-08-21T14:00"))
    assert mc.is_cool(("CAM3", "2026-08-21T14:00")) is True
    assert mc.is_cool(("CAM3", "2026-08-21T14:01")) is False
    assert mc.is_cool(("CAM5", "2026-08-21T14:00")) is False


def test_motion_cooldown_window_expiry():
    """After the window passes, is_cool returns False again.

    Uses a tiny window (0.1s) + a short sleep to keep the test fast.
    """
    import time

    from infra.cooldown import MotionCooldown
    mc = MotionCooldown(window_seconds=1)  # 1 second, easier than 0.1
    key = ("CAM3", "2026-08-21T14:00")
    mc.mark(key)
    assert mc.is_cool(key) is True
    time.sleep(1.2)
    assert mc.is_cool(key) is False


def test_motion_cooldown_stats_reports_active_keys():
    """stats() reports the count of keys currently within their window."""
    from infra.cooldown import MotionCooldown
    mc = MotionCooldown(window_seconds=60)
    mc.mark(("CAM3", "2026-08-21T14:00"))
    mc.mark(("CAM3", "2026-08-21T14:01"))
    mc.mark(("CAM5", "2026-08-21T14:00"))
    stats = mc.stats()
    assert stats["window_seconds"] == 60
    assert stats["active_keys"] == 3
    assert stats["total_keys_tracked"] == 3


def test_motion_cooldown_module_singleton_exists():
    """The module-level MOTION_COOLDOWN singleton is exposed for the listener.

    Per the listener's /status handler contract: MOTION_COOLDOWN.stats() must
    be callable from the route handler. If this test fails, the singleton
    was renamed or removed and /status will break.
    """
    from infra import cooldown
    assert hasattr(cooldown, "MOTION_COOLDOWN")
    assert isinstance(cooldown.MOTION_COOLDOWN, cooldown.MotionCooldown)
