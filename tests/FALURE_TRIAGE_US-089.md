# Test Failure Triage - US-089

**Run date:** 2026-09-26 (fresh verified run, run 963)
**Baseline:** 432 tests, 372 passed, 60 failed, 2 skipped, 7 warnings
**Confirmed pre-existing:** same failure class at HEAD 1b709b5 (pre-today).

---

## Full-suite failure breakdown (60 total)

When running `pytest tests/ -q` (full suite, conftest + module-level setup),
60 tests fail across 14 files:

| # | File | Failures | Category |
|---|---|---|---|
| 1 | `tests/test_cross_diff_bbox_e2e.py` | 22 | E2E bbox assertion mismatch |
| 2 | `tests/test_recipe.py` | 11 | FileNotFoundError (fixture files missing) |
| 3 | `tests/e2e/test_telegram_html_rendering.py` | 6 | Fixture design bug (conftest mock yields None) |
| 4 | `tests/test_time_of_day.py` | 4 | Runtime errors (infra import -- env var not set individually) |
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

**Note on individual-file runs:** When running `pytest tests/test_quiet_hours.py` or
`pytest tests/test_time_of_day.py` individually (no conftest), all 27/17 tests crash
at import time with `RuntimeError` because `infra/__init__.py` imports
`alert_overrides_baseline` -> `quick_classifier` ->
`_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level.
In the full suite, these only show 1 and 4 failures respectively because:
- `tests/e2e/test_animal_recognition_e2e.py` sets
  `os.environ.setdefault("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")` at module level
  (line 30) and is collected FIRST alphabetically, so the env var is set before
  `test_quiet_hours.py` and `test_time_of_day.py` are collected.
- The remaining failures in those files are runtime assertion failures, not
  collection crashes.

**Note on test_farm_api_telegram_e2e.py:** Collects fine individually and in full
suite. In full suite, 5 of 6 tests pass (conftest's `mock_llama_server` fixture
provides the mock client), leaving only `test_error_count` failing with
`assert 0 == 2`. Individually, all 6 tests fail because `mock_llama_server`
returns `None` for non-server tests (see Category D).

**Note on test_paths.py:** 2 full-suite failures from `infra/__init__.py` import
crash (the env var IS set by animal_recognition_e2e.py during collection, but
the specific env override tests still fail because they expect specific behavior
from `infra.paths` that depends on the env var being set to a specific value).

---

## Individual-file run failures (collection crashes)

When running `pytest tests/<file>.py` individually (no conftest, no
animal_recognition_e2e.py to set the env var), **7 more files crash at
collection** due to the infra import chain:

| File | Individual failures | Root cause |
|---|---|---|
| `tests/test_quiet_hours.py` | 27 | RuntimeError at import time (all tests) |
| `tests/test_time_of_day.py` | 17 | RuntimeError at import time (all tests) |
| `tests/test_farm_api_telegram_e2e.py` | 6 | Runtime error at import time (infra import) |
| `tests/test_cross_diff_bbox_e2e.py` | 1 (collection error) | RuntimeError at import time |
| `tests/test_recipe.py` | 1 (collection error) | RuntimeError at import time |
| `tests/test_verify_recipe_live.py` | 1 (collection error) | RuntimeError at import time |
| `tests/test_paths.py` | 1 (collection error) | RuntimeError at import time |

---

## Category A: infra/__init__.py bootstrap issue -- root cause for individual-file crashes

`infra/__init__.py` imports `infra.alert_overrides_baseline` which imports
`infra.quick_classifier` which calls `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")`
at module level (line 82 of `quick_classifier.py`). Any test importing from
`infra` (directly or transitively) crashes if this env var is not set.

**Why full suite avoids most crashes:**
`tests/e2e/test_animal_recognition_e2e.py` (collected first alphabetically) sets
`os.environ.setdefault("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")` at module level
(line 30). This makes the env var available for all subsequent test collections.
**conftest.py does NOT set this env var.** The previous triage claimed conftest
sets it -- that was incorrect.

**In full suite:** 45 tests still fail at runtime (Category B below) because the
infra import succeeds (env var is set by animal_recognition_e2e.py during collection)
but the tests have other failures:
- test_cross_diff_bbox_e2e.py: 22 assertion failures on actual event data
- test_recipe.py: 11 FileNotFoundError (fixture files missing)
- test_time_of_day.py: 4 RuntimeError (infra import chain, see below)
- test_verify_recipe_live.py: 1 assertion error
- test_farm_api_telegram_e2e.py: 1 assertion error (`assert 0 == 2`)
- test_register_cameras.py: 2 assertion errors (redaction mismatch)
- test_quiet_hours.py: 1 RuntimeError
- test_paths.py: 2 env override test failures
- test_home_env.py: 1 token value in log

**Individual files:** 54 failures across 7 files (27+17+6+1+1+1+1).

---

## Category B: Runtime failures (infra import succeeds, tests fail at execution) -- 45 failures

These pass collection (env var is set by `tests/e2e/test_animal_recognition_e2e.py`
at module level) but fail during execution:

| File | Count | Root cause |
|---|---|---|
| `test_cross_diff_bbox_e2e.py` | 22 | Bbox IOU/center_x/no_fallback assertions fail on actual event data |
| `test_recipe.py` | 11 | `FileNotFoundError` -- recipe fixture files missing on disk |
| `test_time_of_day.py` | 4 | RuntimeError at import time (infra import chain) -- still fails because |
| | | | individual test imports don't go through animal_recognition_e2e.py |
| `test_verify_recipe_live.py` | 1 | Infra import path leads to assertion error |
| `test_farm_api_telegram_e2e.py` | 1 | `test_error_count`: `assert 0 == 2` -- log has 0 errors |
| `test_register_cameras.py` | 2 | LAN IP is `[REDACTED-LAN-IP]` not `192.168.x.x` |
| `test_quiet_hours.py` | 1 | `test_kill_switch_falsy_values[]`: RuntimeError from infra import |
| `test_paths.py` | 2 | Env override tests fail due to infra import |
| `test_home_env.py` | 1 | Token value in log message |

**Subtotal: 45 full-suite runtime failures.**

---

## Category C: Scanner tests -- real violations found -- 9 failures across 4 files

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

**Full suite: 60 failures across 14 files** (definitive count from
`pytest tests/ -q`)

The per-file breakdown above (section starting at "Full-suite failure breakdown")
is authoritative. The categories below group them by root cause for quick reference:

| Category | Count | Files |
|---|---|---|
| B - Bbox assertion mismatch | 22 | test_cross_diff_bbox_e2e.py |
| B - Recipe fixture files missing | 11 | test_recipe.py |
| C - Scanner finds real violations | 9 | 4 scanner files |
| D - E2E fixture design bug | 6 | test_telegram_html_rendering.py |
| B - Time-of-day runtime errors | 4 | test_time_of_day.py |
| B - Assertion mismatches (redaction/IP) | 4 | test_register_cameras(2), test_farm_api_telegram(1), test_verify_recipe_live(1) |
| B - Infra import crash (quiet_hours + paths) | 3 | test_quiet_hours(1), test_paths(2) |
| D - Miscellaneous (token log) | 1 | test_home_env.py |

**Sum check:** 22 + 11 + 9 + 6 + 4 + 4 + 3 + 1 = 60.

**Note on counting:** The 60 full-suite failures are the definitive count.
The ~54 individual-file collection crashes are separate -- they only occur when
running a single test file without the animal_recognition_e2e.py env setup.
Some individual-file crash files also have full-suite runtime failures
(test_quiet_hours.py: 27 individual + 1 full-suite = 28 total issues).

**Note on test_recipe.py:** 11 tests fail in full suite (5 TestApplyRecipe + 5
TestVerifyRecipe with FileNotFoundError, 3 TestParseRecipeToml pass).

---

## Root causes (ordered by impact)

1. **`infra/__init__.py` imports `alert_overrides_baseline` at module level** ->
   `quick_classifier` -> `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")`
   at module level. ANY test importing from `infra` crashes if this env var is not
   set. **Fix:** lazy-load env vars or set in conftest.py. Currently avoided in full
   suite only because `test_animal_recognition_e2e.py` (collected first) sets it via
   `os.environ.setdefault()` at module level -- a fragile workaround.

2. **Scanner tests are too strict / allowlist is stale.** Scanner scripts detect
   real violations in `telegram_formatter/`, `infra/farm_api/cameras.py`, and
   `infra/quick_classifier.py`. Either the violations need to be cleaned up, or the
   allowlists need to be updated.

3. **`tests/e2e/conftest.py::mock_llama_server` yields `None`.** The e2e conftest's
   fixture yields `None` for non-server-E2E tests, which breaks
   `test_telegram_html_rendering.py` that expects a mock httpx client.

4. **`test_cross_diff_bbox_e2e.py` has 22 runtime failures.** Bbox assertions (IOU,
   center_x, no_fallback_warning) fail on actual event data. Test thresholds or
   test data are stale.

5. **`test_recipe.py` fixture files missing.** 11 tests fail with `FileNotFoundError`
   (5 TestApplyRecipe + 5 TestVerifyRecipe). 3 TestParseRecipeToml pass.

6. **`test_time_of_day.py` / `test_quiet_hours.py` runtime errors.** Individual file
   runs crash at collection; full suite runs have 4+1 runtime failures for other
   reasons.

7. **Test assertions expect values that changed.** `test_register_cameras.py` and
   `test_verify_recipe_live.py` tests assume LAN IPs like `192.168.x.x` but code
   returns `[REDACTED-LAN-IP]` after redaction.

---

## Not actionable as part of US-089

This card is for **triage only** -- documenting the failures. Fixing them requires
separate cards:
- Fix 1 (infra import) requires a design decision: lazy-load vs conftest env var
- Fix 2 (scanner allowlist) is trivial but needs operator approval to change scanner behavior
- Fix 3 (e2e conftest) requires understanding the mock fixture hierarchy
- Fix 4 (test assertions) requires understanding the redaction contract
- Fix 5 (recipe fixtures) requires creating fixture data
- Fix 6 (time-of-day logic) requires understanding the suntime integration
- Fix 7 (bbox E2E tests) requires understanding the test data format
