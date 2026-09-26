# Test Failure Triage — US-089

**Run date:** 2026-09-26
**Baseline:** 432 tests, 376 passed, 56 failed, 2 skipped, 7 warnings
**Confirmed pre-existing:** same 56 failures at HEAD 1b709b5 (pre-today).

---

## Summary by category

### A. Infra import bootstraps (env var not set at import time) — 47 failures across 6 test files

These fail because `infra/__init__.py` imports `alert_overrides_baseline` which imports `quick_classifier` which calls `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level. When running a single test file (not the full suite), `pytest` does not set this env var, so `infra/__init__.py` raises `RuntimeError` during test collection.

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_cross_diff_bbox_e2e.py` | 24 | Collection fails — infra imports crash | Fix `infra/__init__.py` to lazy-load env vars, OR set env var in conftest.py |
| `test_quiet_hours.py` | 27 | Same — collection fails | Same fix |
| `test_time_of_day.py` | 17 (reported as 4 in summary, 17 total) | Same — collection fails | Same fix |
| `test_recipe.py` | 7 | Collection fails | Same fix |
| `test_verify_recipe_live.py` | 1 | Collection fails | Same fix |
| `test_farm_api_telegram_e2e.py::test_error_count` | 1 | Collection fails (only 1 of 16 tests in that file; rest pass) | Same fix |
| `test_paths.py` | 2 | Collection fails | Same fix |

**Total in this category: ~47 failures (some overlap with other categories)**

### B. Scanner tests — production tree has real violations — 8 failures across 3 files

These tests run the scanner scripts (`check_no_jpeg.py`, `check_no_orphan_modules.py`, `check_no_private_data.py`) against the production tree. The scanner correctly finds violations that should have been cleaned up.

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_check_no_jpeg.py` | 2 | Scanner finds `.jpg` literals in `telegram_formatter/dispatcher.py:113,124` | Remove/add to allowlist in scanner |
| `test_check_no_orphan_modules.py` | 2 | Scanner finds 2 orphan modules: `infra/env_utils.py`, `telegram_formatter/no_match_telegram.py` | Delete orphans or add to allowlist |
| `test_check_no_private_data.py` | 3 | Scanner finds production IPs in `infra/farm_api/cameras.py:102-104` | Whitelist these or remove IP literals |
| `test_no_image_resize.py` | 2 | Scanner finds resize pattern in `infra/quick_classifier.py:714` | Remove reference or add to allowlist |

### C. Fixture / test design bugs — 6 failures

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_telegram_html_rendering.py::TestDispatchPayloadShape` | 6 | `tests/e2e/conftest.py::mock_llama_server` overrides the root conftest fixture and yields `None` instead of the mock for `test_telegram_html_rendering.py`. Tests receive `None` as `mock_llama_server` → `AttributeError: 'NoneType' object has no attribute 'post'` | Fix `e2e/conftest.py` to preserve the root mock for telegram tests |
| `test_home_env.py::test_telegram_token_length_revealed_not_value` | 1 | Test asserts no log message contains the token value, but a log message does include it | Fix test or fix the log code |
| `test_register_cameras.py::test_password_not_in_argv` | 1 | Asserts password token < 8 chars, but actual token is `[REDACTED-LAN-IP]:9000` (22 chars). The test checks the wrong value — it's a LAN IP, not a password leak | Fix test assertion |
| `test_register_cameras.py::test_all_hosts_specified` | 1 | Asserts host starts with `192.168.` but actual host is `[REDACTED-LAN-IP]:9000` | Fix test to handle the redacted IP format |

### D. Other / miscellaneous

| File | Failures | Root cause | Fix needed |
|---|---|---|---|
| `test_verify_recipe_live.py` | 1 | (covered under category A — infra import crash) | |

---

## Grand totals by severity

| Severity | Count | Files |
|---|---|---|
| **Critical — infra import crash** | ~47 | 6+ files |
| **High — scanner finds real violations** | 8 | 4 files |
| **Medium — fixture design bugs** | 7 | 2 files |
| **Low — test assertion mismatches** | 2 | 1 file |
| **TOTAL** | **56** | **15 files** |

---

## Root causes (ordered by impact)

1. **`infra/__init__.py` imports `alert_overrides_baseline` at module level** which imports `quick_classifier` which calls `_require_env_lower_bool("MOTION_GATE_NIGHT_SUPPRESS_ENABLED")` at module level. This means ANY test that imports from `infra` (directly or transitively) will crash if this env var is not set. The fix is to make these env reads lazy or to set the env var in `conftest.py` for all test runs.

2. **Scanner tests are too strict / allowlist is stale.** The scanner scripts detect real violations in `telegram_formatter/`, `infra/farm_api/cameras.py`, and `infra/quick_classifier.py`. Either the violations need to be cleaned up, or the allowlists in the scanner scripts need to be updated.

3. **`tests/e2e/conftest.py` breaks the root `mock_llama_server` fixture.** The e2e conftest's `mock_llama_server` fixture yields `None` for non-server-E2E tests, which breaks `test_telegram_html_rendering.py` that expects a mock httpx client.

4. **Test assertions expect values that changed.** `test_register_cameras.py` tests assume LAN IPs like `192.168.x.x` but the actual value is `[REDACTED-LAN-IP]:9000`.
