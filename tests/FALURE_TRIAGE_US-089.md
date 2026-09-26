# Test Failure Triage - US-089

**Run date:** 2026-09-26 (re-verified run 959)
**Baseline:** 432 tests, 372 passed, 60 failed, 2 skipped, 7 warnings
**Confirmed pre-existing:** same failure class at HEAD 1b709b5 (pre-today), count shifted from 56 to 60 between 1b709b5 and 78cfb11 due to test suite changes.

---

## Summary by category

### A. Infra import bootstraps (env var not set at collection time) -- 12 failures across 4 files

These fail during collection when run individually (not in the full suite). `infra/__init__.py` imports `alert_overrides_baseline` which imports `quick_classifier` which calls `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level. When running a single test file, pytest does not set this env var, so `infra/__init__.py` raises `RuntimeError` during test collection. In the full suite, conftest.py sets the env var so collection succeeds and tests proceed to actual failures.

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_cross_diff_bbox_e2e.py` | 24 (collection crash) | Collection fails -- infra imports crash | Fix `infra/__init__.py` to lazy-load env vars, OR set env var in conftest.py |
| `test_recipe.py` | ~11 (collection crash) | Same -- collection fails | Same fix |
| `test_verify_recipe_live.py` | 1 (collection crash) | Same -- collection fails | Same fix |
| `test_paths.py` | 2 (collection crash) | Same -- collection fails | Same fix |

**Note:** `test_quiet_hours.py` (27 reported in prior triage) and `test_time_of_day.py` (17 reported) do NOT appear in the full-suite failure list -- conftest.py sets the env var for the full suite, so they pass. They only fail when run individually.

**Total in this category: ~38 failures (collection crash + individual-file runs)**

### B. Full-suite execution failures (env is set, but tests fail at runtime) -- 17 failures across 6 files

These pass collection (env var is set by conftest.py) but fail during test execution.

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_recipe.py` | 11 | `FileNotFoundError` -- recipe fixture files missing | Create fixture files or fix test paths |
| `test_cross_diff_bbox_e2e.py` | 22 | E2E fixture/data mismatch -- bbox assertions fail on actual event data | Fix test thresholds or update test data |
| `test_time_of_day.py` | 4 | Logic errors: `test_05_41_is_day` asserts True is False; sunset+buffer tests fail | Fix time-of-day logic or test assumptions |
| `test_verify_recipe_live.py` | 1 | `assert '[REDACTED-LAN-IP]' == '192.168.1.39'` -- test expects raw IP but code redacts | Fix test to expect redacted format |
| `test_farm_api_telegram_e2e.py` | 1 | `test_error_count` -- infra import crash on individual run | Category A fix resolves |
| `test_register_cameras.py` | 2 | LAN IP is `[REDACTED-LAN-IP]` not `192.168.x.x` | Fix test assertions |

### C. Scanner tests -- production tree has real violations -- 8 failures across 3 files

These tests run the scanner scripts (`check_no_jpeg.py`, `check_no_orphan_modules.py`, `check_no_private_data.py`, `check_no_image_resize.py`) against the production tree. The scanner correctly finds violations that should have been cleaned up.

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_check_no_jpeg.py` | 2 | Scanner finds `.jpg` literals in `telegram_formatter/dispatcher.py:113,124` | Remove/add to allowlist in scanner |
| `test_check_no_orphan_modules.py` | 2 | Scanner finds 2 orphan modules: `infra/env_utils.py`, `telegram_formatter/no_match_telegram.py` | Delete orphans or add to allowlist |
| `test_check_no_private_data.py` | 3 | Scanner finds production IPs in `infra/farm_api/cameras.py:102-104` | Whitelist these or remove IP literals |
| `test_no_image_resize.py` | 2 | Scanner finds resize pattern in `infra/quick_classifier.py:714` | Remove reference or add to allowlist |

### D. Fixture / test design bugs -- 6 failures across 2 files

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `e2e/test_telegram_html_rendering.py` | 6 | `tests/e2e/conftest.py::mock_llama_server` overrides the root conftest fixture and yields `None` instead of the mock. Tests receive `None` as `mock_llama_server` -> `AttributeError: 'NoneType' object has no attribute 'post'` | Fix `e2e/conftest.py` to preserve the root mock for telegram tests |
| `test_home_env.py` | 1 | Test asserts no log message contains the token value, but a log message does include it | Fix test or fix the log code |

### E. Other / miscellaneous

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_register_cameras.py` | 2 | LAN IP format mismatch (`[REDACTED-LAN-IP]` vs `192.168.x.x`) | Fix test assertions (also in Category B) |
| `test_quiet_hours.py` | 1 | `test_kill_switch_falsy_values[]` -- RuntimeErr in full run | Infra import crash (Category A) |

---

## Grand totals by severity

| Severity | Count | Files |
|---|---|---|
| **Critical - infra import crash** | ~38 (collection) + 3 (in full run) = ~41 | 6+ files |
| **High - scanner finds real violations** | 8 | 4 files |
| **High - full-suite runtime failures** | 17 | 6 files |
| **Medium - fixture design bugs** | 7 | 2 files |
| **TOTAL** | **60** | **14 files** |

---

## Root causes (ordered by impact)

1. **`infra/__init__.py` imports `alert_overrides_baseline` at module level** which imports `quick_classifier` which calls `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level. This means ANY test that imports from `infra` (directly or transitively) will crash if this env var is not set. The fix is to make these env reads lazy or to set the env var in `conftest.py` for all test runs.

2. **Scanner tests are too strict / allowlist is stale.** The scanner scripts detect real violations in `telegram_formatter/`, `infra/farm_api/cameras.py`, and `infra/quick_classifier.py`. Either the violations need to be cleaned up, or the allowlists in the scanner scripts need to be updated.

3. **`tests/e2e/conftest.py` breaks the root `mock_llama_server` fixture.** The e2e conftest's `mock_llama_server` fixture yields `None` for non-server-E2E tests, which breaks `test_telegram_html_rendering.py` that expects a mock httpx client.

4. **Test assertions expect values that changed.** `test_register_cameras.py` and `test_verify_recipe_live.py` tests assume LAN IPs like `192.168.x.x` but the actual value is `[REDACTED-LAN-IP]` after redaction.

5. **`test_recipe.py` fixture files missing.** In the full suite, recipe tests fail with `FileNotFoundError` because expected fixture files don't exist on disk.

6. **`test_time_of_day.py` logic errors.** Sunset+buffer time calculations and day/night boundary assertions are wrong.

---

## Not actionable as part of US-089

This card is for **triage only** -- documenting the failures. Fixing them requires separate cards:
- Fix 1 (infra import) requires a design decision: lazy-load vs conftest env var
- Fix 2 (scanner allowlist) is trivial but needs operator approval to change scanner behavior
- Fix 3 (e2e conftest) requires understanding the mock fixture hierarchy
- Fix 4 (test assertions) requires understanding the redaction contract
- Fix 5 (recipe fixtures) requires creating fixture data
- Fix 6 (time-of-day logic) requires understanding the suntime integration
