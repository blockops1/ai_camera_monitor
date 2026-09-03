# Skill Refactor — 2026-08-15 Audit Log

**Author:** Jill (AI operator) for Note (name one)
**Date:** 2026-08-15
**Scope:** All 19 skills in `~/.hermes/skills/` that were operational rather than narrative/historical.

## Context

On 2026-08-14 the listener cutover from `<legacy-repo>/` (old repo) to `~/ai_camera_monitor/` (live system) was completed. As of 2026-08-15, the refactor is the system of record; the old repo is kept for code-comparison and rollback purposes only.

This was decided by Note: *"We're past the point that we're gonna do a rollback. We're gonna operate on the new listener and continue to move forward with it."*

The skill library (`~/.hermes/skills/`) is the operational playbook an agent uses on <legacy-repo> work. Many of the skills predate the cutover and still hardcode `<legacy-repo>/` paths in operational commands (`cd`, `grep`, `pytest`, `PYTHONPATH=src`, `python3 -c <<EOF`). Those needed to be updated.

## Approach

Note instruction: *"Just do 1 skill at a time. This is important work."*

- One skill at a time, no delegation. Each skill got a full read → verify-against-refactor → patch → final-audit cycle before moving to the next.
- For each skill: surgical patches (per-occurrence with unique surrounding context) preferred over `replace_all` (a `replace_all` collision mid-refactor produced `ai_camera_monitor-refactor` in 8 lines on skill #4 — recovered immediately with a second targeted replace_all, no net damage).
- Historical phase numbers and module names in narrative/burn-story context were left in place (per the "old as reference" rule). Only operational paths and code references were updated.
- Scripts and modules that don't exist in the refactor were flagged with explicit "not yet ported" notes rather than silently removed. The skill body explains what they do; the note flags the gap.

## Files updated

| # | Skill | Type of change |
|---|---|---|
| 1 | `probe-ofs-motion-health` | Script path → refactor + status note (script not ported) |
| 2 | `local-ai/add-camera` | System Context → refactor; `cam_browser.py`, `probe_rtsp.py`, `cameras/*.md` flagged as old-repo-only |
| 3 | `devops/local-infra` | Credentials path → refactor; SMBFS launchd-asuser example → refactor |
| 4 | `local-ai/farm-vision-alert-routing` | Front-matter system-of-record note; 15 `cd` blocks + 2 PYTHONPATH refs; AST read path updated |
| 5 | `<legacy-repo>-telegram-tagging` | Header → refactor; sweep greps cover `infra/`, `listener/`, `telegram_formatter/`; PROJ.md → PLAN.md; launchd → `-refactor` |
| 6 | `farm-vision-6b19-multi-vehicle-motion` | 2 absolute paths in code samples → refactor |
| 7 | `local-ai/vehicle-enrollment` | 4 `cd` blocks added; `data/frames/` path; launchd label |
| 8 | `vision-queue-and-camera-allowlist-2026-07-20` | grep target → refactor; 3 PYTHONPATH fixes; `crop_face_region_from_4k` import corrected (it's in `infra/image_prep.py`, not `infra/frame_capture.py`) |
| 9 | `match-fleet-audit` | 3 `cd` blocks; script-not-ported warnings; imports → `vehicle_matcher.matcher` (vehicle_state retired) |
| 10 | `recommendations-backlog` | PROJ.md → PLAN.md; refactor pointer table; cross-doc verify command updated |
| 11 | `software-development/software-development-practices` | Reference example → refactor (26 modules, ~30 test files, 270+ tests) |
| 12 | `jill-workflow-style` | "copying 26 modules" → refactor; PROJ.md → PLAN.md in step-gates |
| 13 | `camera-ai-to-vision-prompt-gap` | `src/vision_analyzer.py` → `infra/vision_analyzer.py`; call-site lines updated (2297/2318/2335) |
| 14 | `farm-vision-decoupling` | `_send_motion_alert` → `listener/listener.py` |
| 15 | `local-ai/retire-camera` | Full table re-pointed: `vehicle_state` retired, `wildlife_scan` retired, `GATEKEEPER_CAMERA` → `infra/vision_queue.py`; Phase 4 scripts flagged as not ported |
| 16 | `local-ai/vision-result-overrides` | `infra/alert_generator.py`; `[OUTBOUND_TELEGRAM]` paths preserved; introspection recipe updated to refactor (line ~1052); BB Solar → Outside Back Solar note |
| 17 | `local-ai/reolink-new-firmware-automation` | System-of-record note; line 230 corrected — the script is genuinely canonical in the skill's `scripts/` (never had a `<legacy-repo>/...` copy) |
| 18 | `local-ai/structured-audit-logging` | `src/outbound_telegram.py` → `infra/audit_telegram.py`; line 1737 → 1756 |
| 19 | `software-development/queue-priority-dispatch` | `_ClassedWebhookExecutor` → `listener/listener.py`; test file flagged as not ported |
| 20 | `local-ai/farm-vision` | Footer note in front-matter (system of record statement + line 32 PROJ.md → PLAN.md) |

## Cross-cutting discoveries during the audit

These were changes that affected multiple skills. Documented here so the next skill-touching agent doesn't have to re-derive them.

### Modules/scripts that exist in old repo but NOT in refactor (agent must re-verify before depending on)

- `scripts/probe_ofs_motion_health.py` — old repo only
- `scripts/send_frame_dump.py` — old repo only
- `scripts/audit_body_style_flex.py` — old repo only
- `scripts/three_matcher_audit.py` — old repo only
- `scripts/probe_rtsp.py` — old repo only
- `scripts/cam_browser.py` — old repo only
- `scripts/probe_reolink_session_pool.py` — skill-pack only (in `~/.hermes/skills/local-ai/reolink-new-firmware-automation/scripts/`)
- `scripts/tune_510a_motion_sensitivity.py` — skill-pack only (same)
- `scripts/read_alarm_settings.py` — skill-pack only (same)
- `scripts/enroll_person.py` — old repo only
- `scripts/probe_reolink_cgi_support.py` — old repo only
- `tests/test_vehicle_event_handler.py` — old repo only
- `tests/test_phase_6b_16_classed_queues.py` — old repo only
- `cameras/RLC-*.md` operator runbooks — old repo only

### Modules retired in the refactor (no equivalent exists)

- `src/vehicle_state.py` — replaced by `vehicle_matcher/` (production matcher is `vehicle_matcher.matcher.match_vehicle_scored`)
- `src/wildlife_scan.py` — deleted (wildlife-scan no longer runs)
- `src/heartbeat.py` → moved to `infra/heartbeat.py` (renamed)
- `src/outbound_telegram.py` → moved to `infra/audit_telegram.py` (renamed)

### Module reassignments agents must know

- `GATEKEEPER_CAMERA` now lives at `infra/vision_queue.py` (was `src/vehicle_state.py`)
- `PRIORITY_OUTSIDE` now lives at `infra/vision_queue.py` (was `src/vision_queue.py`)
- `PHASE6A_ELIGIBLE_CAMERAS` now lives at `infra/vision_queue.py` (was `src/vision_queue.py`)
- `INF_VEHICLE_MATCHER` (the script `infra/vehicle_matcher.py`) is a re-export shim — the real matcher is `vehicle_matcher/matcher.py`
- `crop_face_region_from_4k` moved to `infra/image_prep.py` — NOT `infra/frame_capture.py` as the old code implied
- `[OUTBOUND_TELEGRAM]` audit-line tag is preserved (in `infra/audit_telegram.py` and `infra/notifier.py`) — operators grepping this tag keep working
- `camera_creds.env` (refactor) uses `OUTSIDE_FRONT_SOLAR_RTSP_URL` style env vars (the camera credential format is unchanged, but the env-var filename is `~/ai_camera_monitor/camera-creds.env`)

### Document rename

- `<legacy-repo>/PROJ-<legacy-repo>.md` → `~/ai_camera_monitor/PLAN.md`
- Refactor's `AGENTS.md` retained the "Recommendations backlog" pointer text verbatim from the old repo's `AGENTS.md`, so cross-doc references still resolve.

### Launchd label

- `ai.farm.surveillance` → `ai.farm.surveillance-listener-refactor`

## Verification recipes

For any future skill that needs to be refactored, here are the tools to use:

```bash
# 1. Find all operational path literals in the skill
grep -nE "<legacy-repo>/?[^r]|<legacy-repo>/?[^r]|cd ~/<legacy-repo>$|PYTHONPATH=src|src/alert_listener\.py|src/vehicle_state\.py" ~/.hermes/skills/<skill>/SKILL.md

# 2. Verify the refactor file existence before patching
cd ~/ai_camera_monitor && find . -name "<file>.py" -not -path "*/__pycache__/*" -not -path "*/.venv/*"

# 3. Verify line numbers cited in the skill still match the refactor
cd ~/ai_camera_monitor && grep -n "<symbol>" <refactor_module>.py
```

## Process notes for the next refactor

- **Catalog first, then patch.** Bulk mass-replace is dangerous; per-skill patches are safer.
- **Always verify refactor file existence** before changing a path. Several skills referenced `src/...` files that have no equivalent in the refactor.
- **When a script is in the skill pack but not the repo**, the skill-pack copy IS the canonical source. Don't point at a non-existent repo path.
- **When a document was renamed** (PROJ.md → PLAN.md), update ALL references in one skill, not piecemeal.
- **Per-skill code review**: the refactor was a structural reorganization, not a behavior change. Module imports changed, line numbers shifted, but the algorithm and order of operations are preserved. The skill patches are about path/line hygiene, not behavior.

## Lessons learned

1. **`replace_all` on substring-containing target strings is fragile.** `cd ~/<legacy-repo>` is a substring of `cd ~/ai_camera_monitor`. If the file already has some occurrences patched, `replace_all` re-matches them and produces `-refactor-refactor`. Use per-occurrence patches with unique surrounding context for surgical edits.
2. **`tool_call` parameter-name muscle memory is a real failure mode.** When the schema requires `path` and the parameter feels like it should be `patch`, the typing reflex can persist across many attempts. When this happens, route through a different tool (e.g. `execute_code` + `hermes_tools.patch`) to break the loop.
3. **Refs change name, not just location.** `PROJ-<legacy-repo>.md` → `PLAN.md`, `src/heartbeat.py` → `infra/heartbeat.py`, `src/outbound_telegram.py` → `infra/audit_telegram.py`. Always verify the exact path against the current refactor before patching.
4. **Modules get split, not just renamed.** `src/vehicle_state.py` was retired entirely; its functionality was split across `vehicle_matcher/` (the matcher logic) and `vehicle_position/` (the parked-vehicle state). The "PRODUCTION (legacy)" line in match-fleet-audit now points at retirement, not a path.
5. **Document dependency on retirements.** When a skill body says "the production matcher is `_match_known_vehicle`" and that function has been retired, the skill needs ANNOTATION, not just a path update. The legacy reference becomes historical context, not current API.

## Status

- All 19 skills updated ✓
- Footer note added to `local-ai/farm-vision` (skill #20) ✓
- This audit log written ✓
- Outstanding: this change is NOT yet committed or pushed (see AGENTS.md — docs go in the same commit as the change)
