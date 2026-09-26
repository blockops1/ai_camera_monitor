# Test Failure Triage - US-089

**Run date:** 2026-09-26 (re-verified run 960)
**Baseline:** 432 tests, 372 passed, 60 failed, 2 skipped, 7 warnings
**Confirmed pre-existing:** same failure class at HEAD 1b709b5 (pre-today).

---

## Summary by category

### A. Infra import bootstrap (env var not set at individual-file collection) -- 5 files

`infra/__init__.py` imports `alert_overrides_baseline` which imports `quick_classifier` which calls `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level. When running a single test file, pytest does not set this env var, so `infra/__init__.py` raises `RuntimeError` during test collection. In the full suite, conftest.py sets the env var (via `monkeypatch` or env setup in a fixture) so collection succeeds.

| File | Full-suite failures | Individual-file failures | Root cause | Fix needed |
|---|---|---|---|---|
| `test_cross_diff_bbox_e2e.py` | 22 | 1 (collection crash) | Infra import crashes at module level | Fix `infra/__init__.py` to lazy-load env vars, OR set env var in conftest.py |
| `test_recipe.py` | 11 | 1 (collection crash) | Same -- collection fails | Same fix |
| `test_paths.py` | 2 | 1 (collection crash) | Same -- collection fails | Same fix |
| `test_quiet_hours.py` | 1 | 8 (collection crash) | Same -- collection fails | Same fix |
| `test_verify_recipe_live.py` | 1 | 1 (collection crash) | Same -- collection fails | Same fix |
| `test_farm_api_telegram_e2e.py` | 1 | 6 (runtime crash, not collection) | Same import path, runtime failure | Same fix |
| `test_time_of_day.py` | 4 | 17 (collection crash) | Same -- collection fails | Same fix |

**Note:** In the full suite, `MOTION_GATE_NIGHT_SUPPRESS_ENABLED` is set by conftest, so these files pass collection. The 1 failure each in `test_quiet_hours.py` and `test_farm_api_telegram_e2e.py` are runtime failures (the env var IS set, but something else fails). All others (22+11+2+1+4 = 40) are actual execution failures in the full suite.

**Total individual-file crashes: ~29 collection crashes across 7 files.**

### B. Full-suite execution failures (env is set, but tests fail at runtime) -- 34 failures across 7 files

These pass collection (env var is set by conftest) but fail during test execution.

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_cross_diff_bbox_e2e.py` | 22 | E2E fixture/data mismatch -- bbox assertions fail on actual event data (IOU, center_x, no_fallback_warning) | Fix test thresholds or update test data |
| `test_recipe.py` | 11 | `FileNotFoundError` -- recipe fixture files missing | Create fixture files or fix test paths |
| `test_time_of_day.py` | 4 | Logic errors: `test_05_41_is_day` asserts True is False; sunset+buffer tests fail | Fix time-of-day logic or test assumptions |
| `test_verify_recipe_live.py` | 1 | `assert '[REDACTED-LAN-IP]' == '192.168.1.39'` -- test expects raw IP but code redacts | Fix test to expect redacted format |
| `test_farm_api_telegram_e2e.py` | 1 | `test_error_count` -- infra import crash on individual run | Category A fix resolves |
| `test_register_cameras.py` | 2 | LAN IP is `[REDACTED-LAN-IP]` not `192.168.x.x` | Fix test assertions |
| `test_quiet_hours.py` | 1 | `test_kill_switch_falsy_values[]` -- RuntimeErr in full run | Infra import crash (Category A) |

**Subtotal: 42 failures in full suite (22+11+4+1+1+2+1)**

### C. Scanner tests -- production tree has real violations -- 8 failures across 4 files

These tests run the scanner scripts (`check_no_jpeg.py`, `check_no_orphan_modules.py`, `check_no_private_data.py`, `check_no_image_resize.py`) against the production tree. The scanner correctly finds violations that should have been cleaned up.

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_check_no_jpeg.py` | 2 | Scanner finds `.jpg` literals in `telegram_formatter/dispatcher.py:113,124` | Remove/add to allowlist in scanner |
| `test_check_no_orphan_modules.py` | 2 | Scanner finds 2 orphan modules: `infra/env_utils.py`, `telegram_formatter/no_match_telegram.py` | Delete orphans or add to allowlist |
| `test_check_no_private_data.py` | 3 | Scanner finds production IPs in `infra/farm_api/cameras.py:102-104` | Whitelist these or remove IP literals |
| `test_no_image_resize.py` | 2 | Scanner finds resize pattern in `infra/quick_classifier.py:714` | Remove reference or add to allowlist |

### D. Fixture / test design bugs -- 7 failures across 2 files

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `e2e/test_telegram_html_rendering.py` | 6 | `tests/e2e/conftest.py::mock_llama_server` overrides the root conftest fixture and yields `None` instead of the mock. Tests receive `None` as `mock_llama_server` -> `AttributeError: 'NoneType' object has no attribute 'post'` | Fix `e2e/conftest.py` to preserve the root mock for telegram tests |
| `test_home_env.py` | 1 | Test asserts no log message contains the token value, but a log message does include it | Fix test or fix the log code |

### E. Miscellaneous / overlap

| File | Failures | Root cause | Category |
|---|---|---|---|
| `test_register_cameras.py` | 2 | LAN IP format mismatch (`[REDACTED-LAN-IP]` vs `192.168.x.x`) | B (runtime) |
| `test_quiet_hours.py` | 1 | `test_kill_switch_falsy_values[]` -- RuntimeError in full run | A (collection) |

---

## Grand totals

**Full suite (60 failures):**

| Severity | Count | Files |
|---|---|---|
| **High - E2E fixture/data mismatch** | 22 | 1 file |
| **High - recipe fixture files missing** | 11 | 1 file |
| **High - scanner finds real violations** | 8 | 4 files |
| **High - time-of-day logic errors** | 4 | 1 file |
| **Medium - infra import crash (individual-file runs)** | ~29 collection crashes | 7 files |
| **Medium - fixture design bugs** | 7 | 2 files |
| **Medium - test assertion mismatches (redaction)** | 3 | 2 files |
| **Low - misc runtime errors** | 1 | 1 file |
| **TOTAL** | **60** | **14 files** |

---

## Root causes (ordered by impact)

1. **`infra/__init__.py` imports `alert_overrides_baseline` at module level** which imports `quick_classifier` which calls `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level. ANY test that imports from `infra` (directly or transitively) will crash if this env var is not set. Fix: lazy-load env vars or set in conftest.py.

2. **Scanner tests are too strict / allowlist is stale.** The scanner scripts detect real violations in `telegram_formatter/`, `infra/farm_api/cameras.py`, and `infra/quick_classifier.py`. Either the violations need to be cleaned up, or the allowlists in the scanner scripts need to be updated.

3. **`tests/e2e/conftest.py` breaks the root `mock_llama_server` fixture.** The e2e conftest's `mock_llama_server` fixture yields `None` for non-server-E2E tests, which breaks `test_telegram_html_rendering.py` that expects a mock httpx client.

4. **`test_recipe.py` fixture files missing.** In the full suite, recipe tests fail with `FileNotFoundError` because expected fixture files don't exist on disk.

5. **`test_cross_diff_bbox_e2e.py` has 22 runtime failures** -- bbox assertions (IOU, center_x, no_fallback_warning) fail on actual event data. The test thresholds or test data are stale.

6. **`test_time_of_day.py` logic errors.** Sunset+buffer time calculations and day/night boundary assertions are wrong.

7. **Test assertions expect values that changed.** `test_register_cameras.py` and `test_verify_recipe_live.py` tests assume LAN IPs like `192.168.x.x` but the actual value is `[REDACTED-LAN-IP]` after redaction.

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
