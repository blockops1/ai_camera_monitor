"""Regression test: first-alert import chain must NOT trigger `MatchVerdict` import.

Background (2026-08-19):
    On the day of the bug, `telegram_formatter/__init__.py` did
    `from .match_telegram import …`. `match_telegram.py` does
    `from vehicle_matcher import MatchVerdict` at module top-level.
    In the live listener, `import vehicle_matcher` resolved to
    `infra/vehicle_matcher.py` (the old module) instead of the
    `vehicle_matcher/` package, raising ImportError. The exception
    was swallowed by the outer try/except in listener.py line 2815/3902,
    which suppressed the lead motion Telegram for every CAM1/CAM2
    vehicle event that day.

Fix:
    - `telegram_formatter/__init__.py` no longer eagerly imports
      `match_telegram` or `no_match_telegram`. Only `motion_telegram`
      (the first-alert path) and `render_qwen` are eager-loaded.
    - The match loop is disabled on the first-alert path; see
      listener/listener.py lines ~3595-3625 (2026-08-19).

This test asserts the fix holds: importing `telegram_formatter` (or
its motion submodule) must not cause `match_telegram` or `no_match_telegram`
to be loaded. If a future change re-introduces the eager import,
this test fails.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def fresh_imports():
    """Snapshot and clear sys.modules for the telegram_formatter subtree.

    Forces a re-import so we observe what `import telegram_formatter`
    ACTUALLY triggers, not whatever was loaded by the test runner.
    Applied automatically to every test in this module.
    """
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k == "telegram_formatter"
        or k.startswith("telegram_formatter.")
    }
    for k in list(saved):
        sys.modules.pop(k, None)
    yield
    # restore so subsequent tests in the same session see the same import graph
    for k, v in saved.items():
        sys.modules[k] = v


def test_telegram_formatter_init_does_not_load_match_telegram():
    """Eager import of `telegram_formatter` must not pull in match_telegram."""
    import telegram_formatter  # noqa: F401

    assert "telegram_formatter.match_telegram" not in sys.modules, (
        "telegram_formatter/__init__.py must not eagerly import match_telegram — "
        "doing so drags `MatchVerdict` into the first-alert import chain and "
        "recreates the 2026-08-19 CAM1/CAM2 alert blackout. See SKILL.md / commit."
    )
    assert "telegram_formatter.no_match_telegram" not in sys.modules, (
        "telegram_formatter/__init__.py must not eagerly import "
        "no_match_telegram — same reason as match_telegram above."
    )


def test_motion_telegram_submodule_import_does_not_load_matchers():
    """Importing only `motion_telegram` (first-alert path) must not load matchers."""
    from telegram_formatter.motion_telegram import (  # noqa: F401
        MotionTelegramInput,
        build_minimal_motion_telegram_body,
    )

    assert "telegram_formatter.match_telegram" not in sys.modules
    assert "telegram_formatter.no_match_telegram" not in sys.modules


def test_motion_telegram_submodule_loads_alone():
    """Direct submodule import (the path the listener uses) works without errors."""
    # This is the exact import line in listener/listener.py:3194
    from telegram_formatter.motion_telegram import (  # noqa: F401
        MotionTelegramInput,
        build_minimal_motion_telegram_body,
        build_motion_telegram_body,
    )
    from telegram_formatter.render_qwen import render_qwen_dict_lines  # noqa: F401

    # And the build function actually works on a simple input.
    body = build_minimal_motion_telegram_body(
        MotionTelegramInput(
            camera_name="CAM1",
            captured_at_iso="2026-08-19T13:55:00.000-04:00",
            trajectory=("UM1", "UM1", "UM2", "LM2"),
            avg_area=19296,
            vision_result={
                "vehicles": [
                    {"description": "blue Tesla Model Y SUV", "color": "blue"}
                ],
                "confidence": 0.92,
            },
        ),
    )
    assert "blue Tesla Model Y SUV" in body
    assert "CAM1" in body