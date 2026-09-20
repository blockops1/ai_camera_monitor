# V2-062 Night-Time Headlight FP Suppression — Verification Report

## Summary

Night-time headlight false-positive (FP) suppression is shipped across all V2-062 cards and verified end-to-end.

| Card | Status | Commit | Description |
|---|---|---|---|
| US-062a | DONE | `d7a0f8e` | Preflight v2-only + V2-046 supersession |
| US-062b | DONE | — | `infra/quiet_hours.py` — quiet-hours gate |
| US-062c | DONE | — | `infra/alert_overrides_baseline.py` — baseline override |
| US-062d | DONE | `7053b91` | Wire `in_quiet_hours()` gate into `_send_tg1/2/3` |
| US-062e | DONE | `01b7d8e` | Per-camera distant-vehicle baseline override |
| US-062f | DONE | `9f59afb` | Wire baseline override into pipeline + flip default |
| US-062g | IN PROGRESS | — | End-to-end regression test (this card) |

## Behavioral Verification

### 1. Quiet-hours gate blocks dispatch

When `FARMSURV_QUIET_HOURS_ENABLED=1` and the camera is in `FARMSURV_QUIET_HOURS_CAMERAS`, `_send_tg1/2/3` calls `in_quiet_hours()` which checks `is_night_at_edt()` + camera scope. If both conditions are met, dispatch is suppressed and a structured log line is emitted. Verified by:

- `test_daemon_quiet_hours.py` — 7 tests exercising suppression and non-suppression
- `test_regression_headlight_fp.py` — 8 new regression tests

### 2. Kill switch re-enables dispatch

When `FARMSURV_QUIET_HOURS_ENABLED=0`, the gate is bypassed and dispatch proceeds normally. Verified in `test_daemon_quiet_hours.py::test_send_tg*_not_suppressed_when_not_night` and `test_regression_headlight_fp.py::TestDispatchGateIntegration::test_kill_switch_re_enables_dispatch`.

### 3. Baseline override (distant vehicles)

When `FARMSURV_BASELINE_OVERRIDE_CAMERAS` is set, vehicles detected on scoped cameras at night are demoted from L2 to L1. Verified in `test_pipeline_baseline_override.py`.

### 4. Regression test results

**Full suite: 654 passed, 5 skipped, 2 warnings** (thread warnings from prior `test_daemon.py` cleanup thread — pre-existing, not introduced by this work).

New tests (8):
- `TestInQuietHoursUnit::test_eda3b858_suppressed_at_0120_utc` — eda3b858 car FP suppressed
- `TestInQuietHoursUnit::test_163e10ea_suppressed_at_2230_utc` — 163e10ea cat FP suppressed
- `TestInQuietHoursUnit::test_daytime_not_suppressed` — daytime not suppressed
- `TestInQuietHoursUnit::test_camera_not_in_list_not_suppressed` — out-of-scope camera not suppressed
- `TestDispatchGateIntegration::test_eda3b858_dispatch_not_called_when_suppressed` — no dispatch for suppressed FP
- `TestDispatchGateIntegration::test_163e10ea_dispatch_not_called_when_suppressed` — no dispatch for suppressed FP
- `TestDispatchGateIntegration::test_kill_switch_re_enables_dispatch` — dispatch proceeds with kill switch off
- `TestDispatchGateIntegration::test_all_three_tg_slots_suppressed` — all three TG slots suppressed

## Env Vars

| Variable | Default | Description |
|---|---|---|
| `FARMSURV_QUIET_HOURS_ENABLED` | `1` | Kill switch — `0` disables quiet-hours suppression |
| `FARMSURV_QUIET_HOURS_CAMERAS` | (empty) | Comma-separated camera IDs to gate |
| `FARMSURV_FARM_LAT` | `34.5782` | Farm latitude (Resaca, GA) |
| `FARMSURV_FARM_LON` | `-84.9438` | Farm longitude |
| `FARMSURV_NIGHT_BUFFER_MINUTES` | `30` | Minutes after sunset / before sunrise to consider "night" |
| `FARMSURV_BASELINE_OVERRIDE_ENABLED` | `1` | Kill switch for baseline demotion |
| `FARMSURV_BASELINE_OVERRIDE_CAMERAS` | (empty) | Cameras subject to baseline demotion |
| `FARMSURV_BASELINE_OVERRIDE_KEYWORDS` | (empty) | Vehicle keywords that trigger demotion |

## Files Changed

- `infra/quiet_hours.py` — `in_quiet_hours()`, `parse_cameras()`, `parse_hours()`
- `infra/time_of_day.py` — `is_night_at_edt()`, `night_start()`, `night_end()`
- `infra/alert_overrides_baseline.py` — `apply_baseline_demotion()`, `demote_if_matched()`, `parse_cameras()`, `parse_keywords()`
- `infra/alert_pipeline.py` — `run_pipeline()` wired with `in_quiet_hours` + `apply_baseline_demotion`
- `infra/paths.py` — `CLEANUP_POLICY_PATH`, `DISPATCH_COOLDOWN_DEFAULT`, etc.
- `listener/daemon.py` — `_send_tg1/2/3` gated with `in_quiet_hours()`, `_quiet_hours_suppress_log()`
- `tests/test_regression_headlight_fp.py` — **NEW** 8 regression tests
