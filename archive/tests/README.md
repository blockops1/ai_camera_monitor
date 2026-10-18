# Archived test suites

Retired test suites that no longer apply to the current codebase. Moved here per
the "archive-first, never delete" convention — git history preserves them.

- `test_watchdog_restart_ladder.py` — moved from `tests/e2e/`; retired because the
  `watchdog_restart` module was deleted in PRD V2-049 (watchdog thread replaced by
  in-thread periodic teardown in `infra/frame_capture.py`). Steady-state recovery
  coverage now lives in `tests/test_frame_capture.py`.
