"""telegram_formatter — pure functions that turn structured data into
Telegram message bodies.

STATUS: stable
THREAD SAFETY: pure functions, no shared state
INPUTS: structured dataclasses (MotionTelegramInput, etc.) — see per-module
OUTPUTS: Telegram message body strings — no I/O, no network
PUBLIC API:
    (submodule re-exports)
    from telegram_formatter.motion_telegram import (
        MotionTelegramInput,
        build_motion_telegram_body,
        build_minimal_motion_telegram_body,  # Phase 6B.89 — first-alert body
    )
    from telegram_formatter.render_qwen import render_qwen_dict_lines
DOES NOT DO:
    - Sends Telegram messages (caller does that via infra.send_telegram / notify)
    - Imports the vehicle matcher (match_telegram / no_match_telegram are
      loaded lazily by their callers; eager loading here previously pulled
      `MatchVerdict` into the first-alert import chain and shadowed
      `vehicle_matcher` against `infra/vehicle_matcher.py`, causing
      ImportError that swallowed the lead motion Telegram — see 2026-08-19).
    - Owns any state, cooldown, persistence, or alert history
CALLED BY:
    - listener/listener.py  (motion_telegram for 6B.89/6B.79)
    - pipeline/orchestrator.py  (all three submodules, lazy)
    - scripts/probe_minimal_motion_alert.py
    - telegram_formatter/tests/  (one test file per submodule)
CALLS INTO: nothing domain-side; render_qwen for Qwen dict formatting
RELATED: PLAN.md §11 (alert pipeline phases 6B.57+, 6B.77, 6B.79, 6B.89, 6B.93)
"""

# Eager import ONLY for the first-alert path (motion_telegram). Match-,
# no-match-, and composite-telegram formatters are imported lazily by the
# post-notify path (which runs only AFTER the first alert has been sent).
# This keeps the first-alert import chain free of the `MatchVerdict`
# shadowing that previously broke CAM1/CAM2 vehicle arrivals (2026-08-19)
# AND free of the heavy cv2/numpy dependency chain that the composite
# renderer pulls in (Phase 6B.111).
from .motion_telegram import (
    MotionTelegramInput,
    build_minimal_motion_telegram_body,
    build_motion_telegram_body,
)
from .render_qwen import render_qwen_dict_lines

__all__ = [
    "MotionTelegramInput",
    "build_minimal_motion_telegram_body",
    "build_motion_telegram_body",
    "render_qwen_dict_lines",
]
