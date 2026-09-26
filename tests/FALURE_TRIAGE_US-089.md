# Test Failure Triage - US-089

**Run date:** 2026-09-26 (re-verified run 961)
**Baseline:** 432 tests, 372 passed, 60 failed, 2 skipped, 7 warnings
**Confirmed pre-existing:** same failure class at HEAD 1b709b5 (pre-today).

---

## Full-suite failure breakdown (60 total)

When running `pytest tests/ -q` (full suite, conftest sets env vars), 60 tests fail across 14 files:

| # | File | Failures | Category |
|---|---|---|---|
| 1 | `tests/test_cross_diff_bbox_e2e.py` | 22 | E2E bbox assertion mismatch |
| 2 | `tests/test_recipe.py` | 11 | FileNotFoundError (fixture files missing) |
| 3 | `tests/e2e/test_telegram_html_rendering.py` | 6 | Fixture design bug (conftest mock yields None) |
| 4 | `tests/test_time_of_day.py` | 4 | Logic errors (sunset+buffer, day/night) |
| 5 | `tests/test_check_no_private_data.py` | 3 | Scanner finds real violations |
| 6 | `tests/test_check_no_jpeg.py` | 2 | Scanner finds real violations |
| 7 | `tests/test_check_no_orphan_modules.py` | 2 | Scanner finds real violations |
| 8 | `tests/test_no_image_resize.py` | 2 | Scanner finds real violations |
| 9 | `tests/test_paths.py` | 2 | Env override import crash (infra/__init__.py) |
| 10 | `tests/test_register_cameras.py` | 2 | Test assertions expect raw IPs, code returns `[REDACTED-LAN-IP]` |
| 11 | `tests/test_farm_api_telegram_e2e.py` | 1 | `test_error_count` assertion: `assert 0 == 2` |
| 12 | `tests/test_home_env.py` | 1 | Token length check: log message includes token value |
| 13 | `tests/test_quiet_hours.py` | 1 | `test_kill_switch_falsy_values[]` RuntimeError (infra import) |
| 14 | `tests/test_verify_recipe_live.py` | 1 | AssertionError (infra import / redaction) |

---

## Individual-file run failures (collection crashes)

When running `pytest tests/<file>.py` individually (no conftest env setup), **7 more files crash at collection** due to `infra/__init__.py` importing `alert_overrides_baseline` -> `quick_classifier` -> `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level:

| File | Individual failures | Root cause |
|---|---|---|
| `tests/test_quiet_hours.py` | 27 | RuntimeError at import time |
| `tests/test_time_of_day.py` | 17 | RuntimeError at import time |
| `tests/test_farm_api_telegram_e2e.py` | 6 | RuntimeError at import time (all 6 tests) |
| `tests/test_cross_diff_bbox_e2e.py` | 1 (collection error) | RuntimeError at import time |
| `tests/test_recipe.py` | 1 (collection error) | RuntimeError at import time |
| `tests/test_verify_recipe_live.py` | 1 (collection error) | RuntimeError at import time |
| `tests/test_paths.py` | 1 (collection error) | RuntimeError at import time |

**Note:** `test_farm_api_telegram_e2e.py` is a special case -- it collects fine individually but all 6 tests fail with the same RuntimeError. In the full suite, the conftest env var fixes 5 of the 6 tests, leaving only `test_error_count` failing for a different reason (`assert 0 == 2`).

---

## Category A: infra/__init__.py bootstrap issue -- root cause for 37 issues

`infra/__init__.py:11` imports `alert_overrides_baseline` which imports `quick_classifier` which calls `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level (line 82 of `quick_classifier.py`). Any test importing from `infra` (directly or transitively) crashes if this env var is not set.

**In full suite:** conftest sets the env var, so 13 tests pass collection. But `test_quiet_hours.py:1` and `test_farm_api_telegram_e2e.py:1` still fail at runtime (different root cause per-file).
**Individual files:** 51 failures across 7 files (27+17+6+1+1+1+1 = 54 individual failures, but 4 of those are collection errors counted once per file).

---

## Category B: E2E / execution failures (env set, tests fail at runtime) -- 42 failures

These pass collection (env var set by conftest) but fail during execution:

| File | Count | Root cause |
|---|---|---|
| `test_cross_diff_bbox_e2e.py` | 22 | Bbox IOU/center_x/no_fallback assertions fail on actual event data |
| `test_recipe.py` | 11 | `FileNotFoundError` -- recipe fixture files missing on disk |
| `test_time_of_day.py` | 4 | Logic errors: sunset+buffer calc wrong; `test_05_41_is_day` asserts `True is False` |
| `test_verify_recipe_live.py` | 1 | Infra import path leads to RuntimeError in this specific test |
| `test_farm_api_telegram_e2e.py` | 1 | `test_error_count`: `assert 0 == 2` -- test expects 2 errors in log, finds 0 |
| `test_register_cameras.py` | 2 | LAN IP is `[REDACTED-LAN-IP]` not `192.168.x.x` |
| `test_quiet_hours.py` | 1 | `test_kill_switch_falsy_values[]`: RuntimeError from infra import |

**Subtotal: 42 full-suite runtime failures.**

---

## Category C: Scanner tests -- real violations found -- 8 failures across 4 files

Scanner scripts find actual violations that should have been cleaned up:

| File | Failures | Violation |
|---|---|---|
| `test_check_no_jpeg.py` | 2 | `.jpg` literals in `telegram_formatter/dispatcher.py:113,124` |
| `test_check_no_orphan_modules.py` | 2 | Orphan modules: `infra/env_utils.py`, `telegram_formatter/no_match_telegram.py` |
| `test_check_no_private_data.py` | 3 | Production IPs in `infra/farm_api/cameras.py:102-104` |
| `test_no_image_resize.py` | 2 | Resize reference in `infra/quick_classifier.py:714` |

---

## Category D: Fixture / test design bugs -- 7 failures across 2 files

| File | Failures | Root cause |
|---|---|---|
| `e2e/test_telegram_html_rendering.py` | 6 | `e2e/conftest.py::mock_llama_server` yields `None` for non-server tests. Tests receive `None` as mock -> `AttributeError: 'NoneType' object has no attribute 'post'` |
| `test_home_env.py` | 1 | Test asserts no log message contains token value, but a log message does include it |

---

## Grand totals

**Full suite: 60 failures across 14 files**

| Severity | Count | Files |
|---|---|---|
| High - E2E bbox assertion mismatch | 22 | 1 |
| High - Recipe fixture files missing | 11 | 1 |
| High - Scanner finds real violations | 8 | 4 |
| Medium - Time-of-day logic errors | 4 | 1 |
| Medium - E2E fixture design bug | 6 | 1 |
| Medium - Test assertion mismatches (redaction/IP) | 3 | 2 |
| Medium - Infra import crash (individual-file runs) | 54 individual / ~29 full-suite | 7 |
| Low - Miscellaneous (env override, error count) | 2 | 2 |

**Note on Category A overlap:** 42 full-suite runtime failures include some infra import issues (3 tests where the env var IS set but the test still fails for other reasons). The ~29 individual-file collection crashes are separate -- they only happen when running a single test file without the conftest env setup.

---

## Root causes (ordered by impact)

1. **`infra/__init__.py` imports `alert_overrides_baseline` at module level** -> `quick_classifier` -> `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level. ANY test importing from `infra` crashes if this env var is not set. Fix: lazy-load env vars or set in conftest.py.

2. **Scanner tests are too strict / allowlist is stale.** Scanner scripts detect real violations in `telegram_formatter/`, `infra/farm_api/cameras.py`, and `infra/quick_classifier.py`. Either the violations need to be cleaned up, or the allowlists need to be updated.

3. **`tests/e2e/conftest.py::mock_llama_server` yields `None`.** The e2e conftest's fixture yields `None` for non-server-E2E tests, which breaks `test_telegram_html_rendering.py` that expects a mock httpx client.

4. **`test_cross_diff_bbox_e2e.py` has 22 runtime failures.** Bbox assertions (IOU, center_x, no_fallback_warning) fail on actual event data. Test thresholds or test data are stale.

5. **`test_recipe.py` fixture files missing.** 11 tests fail with `FileNotFoundError` because expected fixture files don't exist.

6. **`test_time_of_day.py` logic errors.** Sunset+buffer time calculations and day/night boundary assertions are wrong.

7. **Test assertions expect values that changed.** `test_register_cameras.py` and `test_verify_recipe_live.py` tests assume LAN IPs like `192.168.x.x` but code returns `[REDACTED-LAN-IP]` after redaction.

---

## Not actionable as part of US-089

This card is for **triage only** -- documenting the failures. Fixing them requires separate cards:
- Fix 1 (infra import) requires a design decision: lazy-load vs conftest env var
- Fix 2 (scanner allowlist) is trivial but needs operator approval to change scanner behavior
- Fix 3 (e2e conftest) requires understanding the mock fixture hierarchy
- Fix 4 (test assertions) requires understanding the redaction contract
- Fix 5 (recipe fixtures) requires creating fixture data
- Fix 6 (time-of-day logic) requires understanding the suntime integration
- Fix 7 (bbox E2E tests) requires understanding the test data format
