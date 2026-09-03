# PROPOSAL — Operational script parameterization + refactor copy

**Status:** PROPOSAL (not approved, not started)  
**Author:** Jill, 2026-08-30, in response to Note's request to copy `<legacy-repo>/scripts/` operational scripts into the refactor and make them parameterized (no longer hardcoded).  
**Scope:** Operator scripts only. Diagnostic/probe scripts are explicitly out of scope (per the user's wording — "scripts in the farm listener that need to be copied to the farm listener refactor").

## Context

Note uses several operator scripts in `<legacy-repo>/scripts/` to drive Reolink cameras and tune sensitivity. Today, every value (camera IP, motion sensitivity number, webhook URL, browser path) is hardcoded at module level. Two consequences:

1. The refactor at `~/ai_camera_monitor/` only has `enroll_person.py` from the operational set. The other 7 scripts that Note actually uses (e.g., to bump camera sensitivity as he did this morning) are missing from the refactor.
2. The hardcoded values mean every tweak — like bumping OFS motion 30 → 40 — would either need a code edit or an env-var injection per-invocation. The right answer is CLI flags for per-invocation values, env vars for stable infrastructure, and JSON for bundled recipes.

**Immediate trigger:** 2026-08-30 morning, Note asked to raise OFS motion sensitivity. We had to drop into the old `<legacy-repo>/` repo to run the tuning script. After copy + parameterization, that operation will be: `cd ~/ai_camera_monitor && .venv/bin/python scripts/tune_510a_motion_sensitivity.py <CAM_IP> --motion-sensitivity 40`.

## Goal

1. **Copy** the 7 missing operational scripts + the shared `cam_browser.py` module from `<legacy-repo>/scripts/` to `~/ai_camera_monitor/scripts/`.
2. **Parameterize** them so:
   - **Camera IP** (which camera to operate on) stays a positional CLI arg.
   - **Motion / Smart Detection / Delay values** live in `config/motion_recipe.json` as documented fleet + per-camera defaults, AND become CLI flags for per-invocation overrides.
   - **Per-camera recipe overrides** live in the same JSON so OFS can sit at 40 while the fleet stays at 25, all visible in one file.
   - **Secrets (camera HTTP_PASS, webhook URL)** continue to live in `.env` (existing pattern, untouched).
   - **Browser path** reads from env (`BROWSER_CHROME_PATH` with a default).
3. **Tests:** every script gets a `--dry-run` flag that exercises the click sequence without writing to the camera. Existing pytest suite stays green; new tests for the JSON recipe loader + flag-parsing layer.

## Out of scope (explicit)

- Diagnostic / probe / sandbox scripts in `<legacy-repo>/scripts/` (25 files). They're not operational — they're per-investigation. They will NOT be copied.
- Person / animal / vehicle enrollment scripts (`enroll_person.py`, `enroll_animal.py`). `enroll_person.py` was just refactored and committed at `574a3c4`; it's already parameterized. `enroll_animal.py` was added during §11.86.5. Neither needs work.
- Touching `<legacy-repo>/` itself. The old repo stays untouched for rollback per the refactor's Step 3 isolation rule.
- Updating PLAN.md. Per Note's 2026-08-23 rule, this proposal becomes a PLAN §NN entry only after architecture is approved.

## Scope (concrete)

### Scripts to copy (8 files)

| # | Script | Notes |
|---|---|---|
| 1 | `cam_browser.py` | Shared module. Imports `CamBrowser` for browser-driven camera auth + nav. Must come first. |
| 2 | `read_alarm_settings.py` | Read motion/smart/delay sliders. CLI arg = camera IP. Add `--json` already exists. |
| 3 | `tune_510a_motion_sensitivity.py` | Apply recipe to one camera. CLI arg = camera IP. Add `--motion-sensitivity N`, `--smart-person N`, `--smart-vehicle N`, `--smart-pet N`, `--delay-person N`, `--delay-vehicle N`, `--delay-pet N`, `--no-recipe`. Reads from `config/motion_recipe.json` for fleet default + per-camera override. |
| 4 | `apply_all_tuning.py` | Apply recipe to all cameras. Add `--camera <label>` to scope, `--dry-run`. |
| 5 | `configure_webhook.py` | Set webhook URL on cameras. Add `--webhook-url URL` (defaults to `WEBHOOK_URL` env or existing default). |
| 6 | `configure_webhook_stepped.py` | Same with stepped verification. Add same flag. |
| 7 | `verify_webhook.py` | Verify webhook delivery. Add `--webhook-url URL`. |
| 8 | `enroll_vehicle_from_alert.py` | Enroll vehicle from alert dir. Need to read first to identify any hardcoded refs that the grep missed (0 IP hits means it's path/config-driven, not IP-driven). |

### Parameterization policy

Per Note's instinct (per-invocation values as CLI flags, not env vars) and existing pattern (camera passwords already in `camera-creds.env`):

| Value type | Mechanism | Defaults preserved? |
|---|---|---|
| Camera IP (positional, which camera) | CLI positional arg | n/a (always required) |
| Camera HTTP_PASS (secret) | Env var (`<LABEL>_HTTP_PASS`) — **unchanged** | Yes — falls back to error if missing |
| Motion sensitivity value | CLI flag `--motion-sensitivity N` | Yes — falls back to recipe value if omitted |
| Smart Person / Vehicle / Pet | CLI flags | Yes — recipe fallback |
| Delay Person / Vehicle / Pet | CLI flags | Yes — recipe fallback |
| Per-camera recipe overrides | `config/motion_recipe.json` keyed by camera label (fleet default in same file) | Fleet default in same file |
| Webhook URL | Env var `WEBHOOK_URL` (new in `camera-creds.env` or new `listener-creds.env`) | Yes — falls back to current hardcoded `http://<LISTENER_IP>:8090/alert` |
| Browser path | Env var `BROWSER_CHROME_PATH` | Yes — falls back to current `/Applications/Google Chrome.app/...` |
| Camera labels per IP | Read from `camera-creds.env` (already there: `OUTSIDE_FRONT_SOLAR_IP=<CAM_IP>` etc.) | Yes — env vars are canonical, hardcoded map is fallback |

### New files to create

1. `scripts/cam_browser.py` — copy from old repo, parameterize `CHROME_PATH` (env var), make IP→label lookup env-driven.
2. `scripts/read_alarm_settings.py` — copy, parameterize camera list to read from `camera-creds.env` env vars (already canonical).
| 3 | `tune_510a_motion_sensitivity.py` — copy, add CLI flags, add `--dry-run`, support JSON recipe. |
4. `scripts/apply_all_tuning.py` — copy, add `--camera` filter, `--dry-run`.
5. `scripts/configure_webhook.py` — copy, add `--webhook-url` flag.
6. `scripts/configure_webhook_stepped.py` — copy, same flag.
7. `scripts/verify_webhook.py` — copy, same flag.
8. `scripts/enroll_vehicle_from_alert.py` — copy, audit hardcoded refs (TBD).
9. `config/motion_recipe.json` — **new**, holds the fleet recipe + per-camera overrides:

```json
{
  "_comment": "Fleet default + per-camera overrides for RLC-510A motion/smart/delay. See README.md.",
  "fleet": {
    "motion_sensitivity": 25,
    "smart_person": 50,
    "smart_vehicle": 30,
    "smart_pet": 30,
    "delay_person": 0,
    "delay_vehicle": 0,
    "delay_pet": 2
  },
  "cameras": {
    "Outside Front Solar": {
      "motion_sensitivity": 40,
      "_comment": "2026-08-30: raised from 30 to catch slow vehicle arrivals. Watch for noise; revert if false-alarm rate climbs."
    },
    "Outside Front Garage": {},
    "Outside Front Power": {},
    "Outside Back Solar": {}
  }
}
```

10. `infra/paths.py` — add `MOTION_RECIPE_FILE` path (gitignored-free, committed JSON).
11. `infra/recipe.py` — **new small module**, single responsibility: load `config/motion_recipe.json`, resolve `fleet` + per-camera override, expose `get_recipe(camera_label) -> dict`. ~30 lines. Pure stdlib (`json`).

### New tests to add

- `tests/test_recipe.py` — load JSON, verify fleet default applied, verify per-camera override applied, verify missing override falls back, verify malformed JSON raises clear error.
- `tests/test_tune_510a_argparse.py` — verify all CLI flags parse, verify `--motion-sensitivity N` overrides recipe, verify `--no-recipe` skips JSON entirely (pure CLI).
- `tests/test_configure_webhook_argparse.py` — verify `--webhook-url` flag + env-var fallback chain.
- `tests/test_cam_browser.py` — verify `BROWSER_CHROME_PATH` env var read with fallback to default.

### Updated skill

- `~/.hermes/skills/local-ai/reolink-camera-config/SKILL.md` (and supporting files) — update to mention: (a) refactor scripts mirror the old ones with CLI flags + JSON recipe, (b) per-camera overrides documented, (c) `--dry-run` flag, (d) `BROWSER_CHROME_PATH` env var. Update IN THE SAME COMMIT (per the workflow rule "skill freshness on every change").

## Workflow / commit plan (per the user's commit-granularity rule)

Single-purpose commits, each independently revertable:

1. **Commit 1 — `copier`:** copy 8 scripts from old repo, no behavior change yet. Tests pass. Verify by reading the diff (should be pure copy).
2. **Commit 2 — `parameterize cam_browser`:** env-var-driven IP→label + `BROWSER_CHROME_PATH`. Tests added. No `.env` edits needed (the env vars already exist).
3. **Commit 3 — `recipe module`:** new `infra/recipe.py` + `config/motion_recipe.json` + `tests/test_recipe.py`. No script changes yet.
4. **Commit 4 — `parameterize tune_510a`:** add CLI flags + `--dry-run` + JSON integration. Tests added. Behavior: running with no flags == current `apply_recipe()` behavior.
5. **Commit 5 — `parameterize read_alarm_settings`:** env-var camera list. Behavior identical to current.
6. **Commit 6 — `parameterize configure_webhook + verify + stepped`:** add `--webhook-url` flag + `WEBHOOK_URL` env var. Behavior identical if env unset.
7. **Commit 7 — `parameterize apply_all_tuning`:** add `--camera` filter + `--dry-run`.
8. **Commit 8 — `parameterize enroll_vehicle_from_alert`:** audit + parameterize.
9. **Commit 9 — `skill update`:** `reolink-camera-config/SKILL.md` + `references/reolink-rlc510a-webhook-setup.md` (or current equivalent) updated.

All commits land on `main`. Branch stays clean.

## Push plan

Push approved for the script-copy commit only (commit 1). The remaining 8 commits stay local until Note reviews. **Per the existing push-approved list** (574a3c4 + earlier 6B.x), no push has been pre-authorized for these commits — flag this for explicit approval.

## Risk / mitigations

| Risk | Mitigation |
|---|---|
| Refactor copy diverges from prod copy (old repo updates don't propagate) | Add a `tests/test_scripts_match_old_repo.py` that does a syntax-equivalent diff. Optional; only if Note wants the guarantee. |
| Operator runs the wrong script and tweaks the wrong camera | `--dry-run` flag on every mutating script, prints intended changes without writing |
| Recipe JSON typo silently misapplies | `infra/recipe.py` validates on load: rejects unknown keys, rejects out-of-range values (motion 1–50, smart 0–100, delay 0–8) |
| Camera-creds.env doesn't have new `WEBHOOK_URL` / `BROWSER_CHROME_PATH` keys | Each script's parameter loader: env var → default value chain, never errors on missing env var, only on missing IP/secret |
| `enroll_vehicle_from_alert.py` has hidden hardcoded refs not caught by grep | Audit in commit 8; if found, parameterize in same commit (single-purpose scope) |
| Forgetting to update the skill | Per the skill workflow rule, commit 9 is mandatory, not optional |

## Open questions for Note

1. **Q1 — File location for the JSON:** `config/motion_recipe.json` (matches existing pattern: `motion_gate_thresholds.json`, `known_vehicles.json`). **Recommended: `config/`.**
2. **Q2 — .env file for new vars:** Extend `camera-creds.env` with `WEBHOOK_URL` and `BROWSER_CHROME_PATH`, or create `scripts-creds.env`? Default: extend `camera-creds.env` (one file to rule them all).
3. **Q3 — RECIPE removal from script:** Once `infra/recipe.py` + `config/motion_recipe.json` exist, the `RECIPE` dict in `tune_510a_motion_sensitivity.py` becomes a fallback default only. OK to delete? Or keep both for safety? Default: keep recipe dict in script as fallback for `--no-recipe --motion-sensitivity 50` style invocations (rare).
4. **Q4 — `apply_all_tuning.py` audience:** Is this still used, or has `tune_510a_motion_sensitivity.py --apply-all` replaced it? Default: copy + parameterize both, audit later.

## Verification plan

- `pytest tests/ -x` — all existing tests still pass (1538 pass baseline).
- `pytest tests/test_recipe.py tests/test_tune_510a_argparse.py tests/test_configure_webhook_argparse.py tests/test_cam_browser.py -v` — new tests pass.
- `ruff check scripts/ infra/ tests/` — zero new lint errors.
- Smoke: `.venv/bin/python scripts/read_alarm_settings.py <CAM_IP> --json` returns same JSON shape as old script.
- Smoke: `.venv/bin/python scripts/tune_510a_motion_sensitivity.py <CAM_IP> --read` returns same state as old script.
- Smoke (parameterization): `.venv/bin/python scripts/tune_510a_motion_sensitivity.py <CAM_IP> --motion-sensitivity 40 --dry-run` prints "would set motion_sensitivity=40" without touching the camera.
- Smoke (JSON recipe): `.venv/bin/python scripts/tune_510a_motion_sensitivity.py <CAM_IP> --dry-run` (no flag) prints resolved values from `config/motion_recipe.json` (fleet + per-camera).
- Skill freshness check: re-read `reolink-camera-config/SKILL.md` after commit 9, confirm new flags + JSON recipe doc mentioned.

## Estimated effort

- Commit 1 (copy): 30 min.
- Commits 2–8 (parameterize): 4–6 hours total (mostly straightforward; audit work in commit 8 is the wildcard).
- Commit 9 (skill): 30 min.
- Tests: 1–2 hours spread across commits.

Total: ~1 working day.

## What this proposal does NOT do

- Does NOT migrate from `<legacy-repo>/` to `~/ai_camera_monitor/` (cutover is a separate scope).
- Does NOT touch the listener code itself (only operator scripts).
- Does NOT add new features to the scripts (e.g., no new alarm types, no new cameras). Pure parameterization.
- Does NOT update PLAN.md (per the "approval-before-write" rule; this proposal file is the staging area).

---

**End of proposal. Awaiting Note's decision on:**
1. Approve as-is (start commit 1)?
2. Approve with changes (which Q1–Q4 to override)?
3. Defer (resume later)?
4. Reject (don't copy — keep using `<legacy-repo>/scripts/` directly)?