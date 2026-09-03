# CLEANUP-2026-08-20 — Vehicle matcher paused migration

**Author:** Note (decision) + Jill (planning)
**Status:** Approved for execution (Phase.103) — Option 2 (extended scope)
**Scope:** Three-part cleanup

1. **Archive** the `vehicle_matcher/` package (the paused clean-rewrite).
2. **Archive the cascaded dead code** that depends on the new package — `pipeline/` (the parallel orchestrator the listener doesn't call) and `telegram_formatter/match_telegram.py` + `telegram_formatter/no_match_telegram.py` (the two Telegram formatters tied to the pipeline).
3. **Trim** the dead-code block from `infra/vehicle_matcher.py` (kept as "rollback safety" since 6B.29d, never needed).

This is a no-behavior-change cleanup. Every moved item is dead code in production (the live listener uses `infra.vehicle_matcher.match_vehicle_scored`, never the new package or the pipeline). Archive is reversible by `mv` back per the archive-first workflow skill.

## Decision rationale

The cleanup is a follow-on to Phase.102 (which removed the `prompt_mode` block from `listener/listener.py` and the `FARMSURV_COMBINED_PROMPT` env var from the LaunchAgent plist). The earlier `prompt_mode` block was the last remaining reference to the paused migration; with that gone, the paused-migration artifacts are now unambiguously dead code.

The probe `scripts/probe_matcher_comparison.py` (2026-08-20) confirmed that `infra.vehicle_matcher.match_vehicle_scored` (production) is materially better than `vehicle_matcher.matcher.match_signature` (paused) on the 4 hand-crafted fixtures — production has 15 per-dimension scoring functions with color normalization, type-group flex, model aliases, distinctive-keyword matching, and negative-mismatch penalties; the paused migration has 4 dimensions and no spec handling. The paused migration is **not a drop-in replacement** — it's a smaller, less expressive matcher that paused mid-port.

**Scope expansion (the cascade):** the original plan was 2-part (archive the new package + trim dead code). Verification pre-execution revealed that **the new package is not the only dead code** — three other code paths import from it and would break if the package were archived:

- `pipeline/orchestrator.py` (the parallel orchestrator) calls `match_signature` and `score_top_n` from the new package.
- `telegram_formatter/match_telegram.py` and `telegram_formatter/no_match_telegram.py` import `MatchVerdict` and `NoMatch` from the new package (used as type hints).
- Their tests live in `pipeline/tests/` and `telegram_formatter/tests/`.

**Critical finding: the listener never calls `pipeline.run_pipeline`.** The listener's match path is `infra.vehicle_matcher.match_vehicle_scored` (production scorer). The pipeline orchestrator is a parallel implementation that exists but is unwired. Therefore the pipeline + the two Telegram formatters are also dead code in production.

**Three-part cleanup is the consistent scope.** The same logic applies to all 8 dead-code artifacts: wire to a paused migration, no production caller, ship unused. Archive them all together. Note approved this scope expansion explicitly (Option 2): *"I'd like you to plan on option two. The nice thing about moving something to an archived directory is if it turns out there's some unforeseen consequences we can always just move it back."*

**Two forward paths exist** (see "Future work" below):
- **Option A** (deferred): Resume the migration — port the 15 dimensions + 5 spec subsystems (`color_normalization`, `type_groups`, `body_style_aliases`, `model_aliases`, `distinctive_keywords`) into the new `vehicle_matcher/` package's per-dimension functions; re-wire the pipeline orchestrator into the listener. Estimated ~5 days of focused work.
- **Option B** (deferred): Archive and reconsider — leave the paused migration in the archive, revisit when matcher capability is a priority again.

This cleanup executes Option B. The new package, the pipeline, and the two Telegram formatters are archived (not deleted) so Option A is still possible without `--git log` archaeology.

## What moves

| Item | Type | Lines | Active in production? | Action |
|---|---|---|---|---|
| `vehicle_matcher/` (dir) | package | 471 | No | `mv` to archive |
| `vehicle_matcher/__init__.py` | file | 40 | No | inside the dir |
| `vehicle_matcher/matcher.py` | file | 180 | No | inside the dir |
| `vehicle_matcher/scoring.py` | file | 251 | No | inside the dir |
| `vehicle_matcher/tests/` (dir) | tests | 602 | No | `mv` to archive (subdir of package) |
| `vehicle_matcher/tests/__init__.py` | file | 1 | No | inside the dir |
| `vehicle_matcher/tests/test_matcher.py` | file | 283 | No | inside the dir |
| `vehicle_matcher/tests/test_scoring.py` | file | 318 | No | inside the dir |
| `pipeline/` (dir) | parallel orchestrator | 278 | No (listener doesn't call `run_pipeline`) | `mv` to archive |
| `pipeline/__init__.py` | file | n/a | No | inside the dir |
| `pipeline/orchestrator.py` | file | 278 | No | inside the dir |
| `pipeline/tests/` (dir) | tests | 438 | No | `mv` to archive |
| `pipeline/tests/__init__.py` | file | n/a | No | inside the dir |
| `pipeline/tests/test_orchestrator.py` | file | 438 | No | inside the dir |
| `telegram_formatter/match_telegram.py` | file | 139 | No (only pipeline uses it) | `mv` to archive |
| `telegram_formatter/no_match_telegram.py` | file | 126 | No (only pipeline uses it) | `mv` to archive |
| `telegram_formatter/tests/test_match_telegram.py` | file | 255 | No | `mv` to archive |
| `telegram_formatter/tests/test_no_match_telegram.py` | file | 252 | No | `mv` to archive |
| `infra/vehicle_matcher.py` (full file, 594 lines) | orchestrator | 594 | **Yes (lines 1-312)** | `mv` to archive before trim |
| `infra/vehicle_matcher.py` lines 313-594 (dead-code block) | dead code | 282 | **No (kept "rollback safety")** | archive in place (same file mv) |
| `infra/vehicle_matcher.py` lines 1-312 (active orchestrator) | live code | 312 | **Yes — production** | **stays in place** |
| `infra/matcher_scoring.py` | scoring engine | 575 | **Yes — production** | **stays in place** (still imported by `infra/vehicle_matcher.py:112`) |
| `infra/matcher_spec.py` | spec data | 252 | **Yes — production** | **stays in place** |
| `telegram_formatter/motion_telegram.py` | file | n/a | **Yes — live listener uses it** | **stays in place** |
| `telegram_formatter/__init__.py` | file | n/a | **Yes — re-exports live + dead formatters** | **stays in place**; docstring updated to mark `match_telegram` / `no_match_telegram` as archived |
| `listener/listener.py` | listener | 4100+ | **Yes — live** | **stays in place**; no edits needed |
| `~/.hermes/skills/local-ai/<legacy-repo>-workflow/SKILL.md` | doc | n/a | Pitfall ref | update if this pause is referenced |
| `PLAN.md` | doc | n/a | source of truth | §11.31 ship section |

**Net effect after cleanup:**
- 14 files moved to archive (3154 lines), 0 files deleted.
- `infra/vehicle_matcher.py` trimmed from 594 → 312 lines (282 lines removed from the live tree).
- Production listener behaviour unchanged. Production tests still pass (some test files for the archived artifacts become archived with them).

**Archive contents (named so the original namespace is preserved):**

```
~/archive/2026-08-20-vehicle-matcher-paused-migration/
├── MANIFEST.md
├── vehicle_matcher_pkg/
│   ├── __init__.py
│   ├── matcher.py
│   ├── scoring.py
│   └── tests/
│       ├── __init__.py
│       ├── test_matcher.py
│       └── test_scoring.py
├── pipeline_pkg/
│   ├── __init__.py
│   ├── orchestrator.py
│   └── tests/
│       ├── __init__.py
│       └── test_orchestrator.py
├── telegram_formatter_match_telegram.py
├── telegram_formatter_no_match_telegram.py
├── telegram_formatter_tests_test_match_telegram.py
├── telegram_formatter_tests_test_no_match_telegram.py
└── infra_vehicle_matcher_full-2026-08-20.py
```

The dirs are renamed `vehicle_matcher_pkg/` and `pipeline_pkg/` to make the original namespace distinction clear in the archive. The Telegram formatter files keep their original names (with the dir prefix on the filename) because `telegram_formatter/` is a bigger package with files that stay in production.

## What stays in production

After the cleanup, the **production matcher stack** is:

1. `infra/matcher_spec.py` — owns spec data (`DEFAULT_SPEC`, `load_spec()`).
2. `infra/matcher_scoring.py` — owns per-dimension scoring engine (`score_vehicle`, 22 dimension functions, `DEFAULT_DIMENSION_WEIGHTS`).
3. `infra/vehicle_matcher.py` (slimmed) — owns the orchestrator (`match_vehicle_scored`, `score_top_n`, `MatchDetail`, `match_with_details`).
4. `telegram_formatter/motion_telegram.py` — motion-only Telegram body (used by the live listener).
5. `listener/listener.py` — production listener, unchanged.

Listener import sites (4) — `from infra.vehicle_matcher import (...)` — stay unchanged.

## Why archive (not delete)

Per the archive-first workflow skill (Note's hard rule 2026-07-20): **never delete or overwrite; move to `~/archive/<date>-<purpose>/` first, record what moved in `MANIFEST.md`, restore by `mv` back.** The cleanup touches 14 files of code + 4 directories of tests, all of which are reversible-valuable:

- The new package + the parallel pipeline + the two Telegram formatters represent work already done. Future investment in the migration (Option A) can reuse the package skeleton, the absence-evidence fix in `scoring.py`, the dataclass-typed `ScoringSpec`, the `run_pipeline` orchestration, and the `build_match_telegram_body` / `build_no_match_telegram_body` rendering.
- The full `infra/vehicle_matcher.py` (594 lines) preserves the 282-line dead-code block in case of **emergency regime reversal** (the docstring at lines 47-59 explicitly says this). Per AGENTS.md "Document what you have" and the archive-first workflow, rolling back from archive is one `mv` away.

**Note's quote (2026-08-20)**: *"The nice thing about moving something to an archived directory as if it turns out that there's some unforeseen consequences we can always just move it back to where it was before. That should be part of your plan."*

This is exactly the workflow we're using.

## Per-file actions

Each file moves as a single `mv` operation. Files in the same directory travel together (e.g., `mv vehicle_matcher/ archive/.../vehicle_matcher_pkg/` moves the package + its `tests/` subdir in one operation).

### Group A: `vehicle_matcher/` (the new package, paused migration)

**Action:** `mv vehicle_matcher/ ~/archive/2026-08-20-vehicle-matcher-paused-migration/vehicle_matcher_pkg/`

This moves the package (3 files: `__init__.py`, `matcher.py`, `scoring.py`) and its tests dir (3 files: `__init__.py`, `test_matcher.py`, `test_scoring.py`) in one operation. Total: 6 files, 1073 lines.

**Pre-move verification:**
```bash
cd <install-path>/ai_camera_monitor
ls vehicle_matcher/                 # → 3 files
ls vehicle_matcher/tests/           # → 3 files
```

**Post-move verification:**
```bash
ls <install-path>/ai_camera_monitor/vehicle_matcher/  # → "No such file or directory"
ls ~/archive/2026-08-20-vehicle-matcher-paused-migration/vehicle_matcher_pkg/  # → 3 files
ls ~/archive/2026-08-20-vehicle-matcher-paused-migration/vehicle_matcher_pkg/tests/  # → 3 files
```

### Group B: `pipeline/` (parallel orchestrator, not wired to listener)

**Action:** `mv pipeline/ ~/archive/2026-08-20-vehicle-matcher-paused-migration/pipeline_pkg/`

This moves the pipeline package (`__init__.py`, `orchestrator.py`) and its tests dir (`__init__.py`, `test_orchestrator.py`) in one operation. Total: 4 files, ~716 lines.

**Why this is dead code:** `pipeline.run_pipeline` is the only production-side caller of `vehicle_matcher.match_signature` / `score_top_n`. The listener never calls `run_pipeline`. So `pipeline/orchestrator.py` is parallel infrastructure that exists but is unwired.

**Pre-move verification:**
```bash
cd <install-path>/ai_camera_monitor
ls pipeline/                        # → __init__.py orchestrator.py
ls pipeline/tests/                  # → __init__.py test_orchestrator.py
grep -n "run_pipeline" listener/listener.py | head -5
# Expected: zero hits — the listener does not call pipeline.run_pipeline
```

**Post-move verification:**
```bash
ls <install-path>/ai_camera_monitor/pipeline/  # → "No such file or directory"
ls ~/archive/2026-08-20-vehicle-matcher-paused-migration/pipeline_pkg/  # → __init__.py orchestrator.py
ls ~/archive/2026-08-20-vehicle-matcher-paused-migration/pipeline_pkg/tests/  # → __init__.py test_orchestrator.py
```

### Group C: `telegram_formatter/match_telegram.py` + `no_match_telegram.py`

**Action:**
```bash
mv telegram_formatter/match_telegram.py    ~/archive/2026-08-20-vehicle-matcher-paused-migration/telegram_formatter_match_telegram.py
mv telegram_formatter/no_match_telegram.py ~/archive/2026-08-20-vehicle-matcher-paused-migration/telegram_formatter_no_match_telegram.py
mv telegram_formatter/tests/test_match_telegram.py    ~/archive/2026-08-20-vehicle-matcher-paused-migration/telegram_formatter_tests_test_match_telegram.py
mv telegram_formatter/tests/test_no_match_telegram.py ~/archive/2026-08-20-vehicle-matcher-paused-migration/telegram_formatter_tests_test_no_match_telegram.py
```

These four files move individually because `telegram_formatter/` is a bigger package — `motion_telegram.py` stays in production. Total: 4 files, 772 lines.

**Why this is dead code:** the only production caller of `build_match_telegram_body` / `build_no_match_telegram_body` is `pipeline/orchestrator.py`. After Group B archives the pipeline, these formatters have no production caller.

**Pre-move verification:**
```bash
cd <install-path>/ai_camera_monitor
grep -rn "build_match_telegram_body\|build_no_match_telegram_body" --include="*.py" | grep -v __pycache__
# Expected: only pipeline/orchestrator.py and the test files in telegram_formatter/tests/
```

**Post-move verification:**
```bash
ls telegram_formatter/match_telegram.py    # → "No such file or directory"
ls telegram_formatter/no_match_telegram.py # → "No such file or directory"
ls telegram_formatter/motion_telegram.py   # → exists (stays in production)
ls telegram_formatter/tests/test_match_telegram.py    # → "No such file or directory"
ls telegram_formatter/tests/test_no_match_telegram.py # → "No such file or directory"
```

**Docstring update for `telegram_formatter/__init__.py`:** the package's `__init__.py` re-exports the live + dead formatters. After the move, update the docstring to note that `match_telegram` and `no_match_telegram` were archived 2026-08-20. (The `__init__.py` itself stays; only the import lines for the archived formatters need updating.)

**Verification grep after the docstring update:**
```bash
grep -n "match_telegram\|no_match_telegram" telegram_formatter/__init__.py
# Expected: only docstring mentions of "archived 2026-08-20", no active re-exports
```

### Group D: `infra/vehicle_matcher.py` (full file, 594 lines)

**Action:** `mv infra/vehicle_matcher.py ~/archive/2026-08-20-vehicle-matcher-paused-migration/infra_vehicle_matcher_full-2026-08-20.py`

This is the **archive-before-edit** step for the dead-code trim. The full 594-line file is preserved; after the trim, the same-named file in the production tree will be 312 lines.

**Pre-move verification:**
```bash
ls -la <install-path>/ai_camera_monitor/infra/vehicle_matcher.py  # → exists
wc -l <install-path>/ai_camera_monitor/infra/vehicle_matcher.py   # → 594
```

**Post-move verification:**
```bash
ls <install-path>/ai_camera_monitor/infra/vehicle_matcher.py  # → "No such file or directory"
ls ~/archive/2026-08-20-vehicle-matcher-paused-migration/infra_vehicle_matcher_full-2026-08-20.py  # → shows the file
wc -l ~/archive/2026-08-20-vehicle-matcher-paused-migration/infra_vehicle_matcher_full-2026-08-20.py  # → 594
```

### Group E: `infra/vehicle_matcher.py` (NEW file, 312 lines)

**Action:** `write_file` with the lines 1-312 content of the original file.

The new file is the slimmed version: lines 1-312 of the original, with the docstring's `KNOWN VIOLATIONS` section updated to reflect "dead code archived 2026-08-20."

**Pre-write verification:**
```bash
# Confirm content matches the archive's lines 1-312
diff <(head -312 ~/archive/2026-08-20-vehicle-matcher-paused-migration/infra_vehicle_matcher_full-2026-08-20.py) <install-path>/ai_camera_monitor/infra/vehicle_matcher.py
# Expected: zero diff (the production file is now a prefix of the archive)
```

**Updated docstring edit:** the original lines 47-59 say "this module still owns the Phase.26a serial-gate interpreter ... dead code in production but kept here as a rollback target." Replace with "this module owns only the production match orchestration; the pre-6B.29d interpreter path was archived 2026-08-20 to `~/archive/2026-08-20-vehicle-matcher-paused-migration/infra_vehicle_matcher_full-2026-08-20.py` for emergency regime-reversal safety."

### Group F: `infra/tests/` (no change)

**Action:** No change. The active test files (`infra/tests/test_matcher_scoring.py` etc.) do not import from the dead-code block. They will continue to pass.

**Pre-trim verification:**
```bash
grep -rn "_normalize_color\|_match_with_spec\|_pass_should_fire\|_pass_color_type_matches" \
  --include="*.py" | grep -v "infra/vehicle_matcher.py:" | grep -v "vehicle_matcher_pkg/"
# Expected: zero hits (no test or other module imports from the dead-code block)
```

## Execution order

The cleanup must be done in this order to preserve archive-first invariants:

1. **Step 1: Create archive dir + write `MANIFEST.md`.** Per the archive-first workflow skill, the manifest is written *before* anything moves. The manifest records what moved, when, why, and the restore commands.

2. **Step 2: `mv vehicle_matcher/ → ~/archive/.../vehicle_matcher_pkg/` (Group A).** The new package moves first. This severs the import graph at the root — once the package is gone, every downstream file (pipeline, telegram formatters) is provably broken until they're also archived.

3. **Step 3: `mv pipeline/ → ~/archive/.../pipeline_pkg/` (Group B).** The parallel pipeline moves. After this, the `vehicle_matcher.*` import lines in `pipeline/orchestrator.py` are already broken (from Step 2), but the file is now archived too, so the broken-import state is acceptable.

4. **Step 4: `mv telegram_formatter/{match_telegram,no_match_telegram}.py + the two test files` (Group C).** The Telegram formatters + their tests move. After this, all four dead-code-bearing files are in the archive.

5. **Step 5: `mv infra/vehicle_matcher.py → ~/archive/.../infra_vehicle_matcher_full-2026-08-20.py` (Group D).** The full 594-line file is archived before the trim. After this `mv`, the production tree has no `infra/vehicle_matcher.py` until the slimmed version is written in Step 6. (The listener cannot import from `infra.vehicle_matcher` until Step 6 writes it.)

6. **Step 6: `write_file infra/vehicle_matcher.py` with lines 1-312 of the archived file (with the docstring updated) (Group E).** The slimmed version is written to the production tree. After this, the listener import sites resolve.

7. **Step 7: Update `telegram_formatter/__init__.py` docstring.** Mark the archived formatters as archived, remove re-exports.

8. **Step 8: `ruff + pytest` baseline.** Verify everything lints cleanly and all remaining tests pass (expect ~625 tests, was 782; the diff is ~157 tests for the archived artifacts — the new package's tests, the pipeline tests, and the four Telegram formatter tests).

9. **Step 9: Listener restart via `unload`/`load` (per Phase.102 procedure).** PID 75372 → new PID. `/health` + `/status` + first live alert verification.

10. **Step 10: PLAN.md §11.31 ship notes + commit + push to deruyter.**

11. **Step 11: Update `~/.hermes/skills/local-ai/<legacy-repo>-workflow/SKILL.md`** if the skill text references the paused migration or the dead-code block.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Production test imports from a dead-code symbol | Very low | Med | Pre-trim grep (Group F verification) |
| Active 1-312 region has an undocumented reference to a dead-code symbol | Low | Med | AST scan (verified) + ruff + pytest |
| `telegram_formatter/__init__.py` re-exports the archived formatters | Low | Med | Step 7 docstring update removes re-exports |
| Listener fails to import after trim | Very low | High | Step 6 writes the slimmed file before pytest runs; the listener restart is its own gate |
| Archived `infra_vehicle_matcher_full-2026-08-20.py` is needed for a regime reversal | Low | Low | `mv` back to `infra/vehicle_matcher.py` and the orchestrator imports resolve |
| Test count delta gives us a regression we didn't expect | Low | Low | Spot-check a few archived tests against the slimmed file's behavior |
| Pipeline orchestrator / telegram formatters are needed by a cron or another agent | Very low | Med | Per the archive-first workflow, the `mv` back is one command; we can iterate from archive |
| Other agent (Jack's box or another hermes profile) imports from `pipeline` or the new `vehicle_matcher` | Very low | Med | The git remote is the source of truth; the local archive is reversible. Remote is unchanged by `mv`. |

**The cleanup is bounded by the archive-first invariant:** every moved item is recoverable in one `mv` command. The blast radius for any wrong move is "restore from archive, diagnose, retry."

## Future work (deferred, NOT in this cleanup)

- **Option A (preferred when matcher is a priority):** Port the 15 dimensions + 5 spec subsystems into the new `vehicle_matcher/` package's per-dimension functions; re-wire the pipeline orchestrator into the listener. Migration tests verify parity. ~5 days of focused work. The archive preserves the new package + the pipeline + the two Telegram formatters + the dead-code block, so the port begins with the archive contents, not from scratch.
- **Option B (do nothing):** Leave the paused migration in the archive. Revisit when matcher capability is a priority again.
- **Decision point:** The decision to archive vs. continue the migration is **a separate phase** from this cleanup. The right cadence is: archive now (Phase.103), port later (Phase.NNN when Note has time and the matcher is a priority).

## Verification checklist

Before declaring the cleanup complete:

- [ ] `~/archive/2026-08-20-vehicle-matcher-paused-migration/MANIFEST.md` exists with restore commands
- [ ] `vehicle_matcher/` package no longer exists in `~/ai_camera_monitor/`
- [ ] `vehicle_matcher_pkg/` inside the archive contains the package + tests dirs
- [ ] `pipeline/` no longer exists in `~/ai_camera_monitor/`
- [ ] `pipeline_pkg/` inside the archive contains the package + tests dirs
- [ ] `telegram_formatter/match_telegram.py` + `no_match_telegram.py` no longer exist in the production tree
- [ ] `telegram_formatter/tests/test_match_telegram.py` + `test_no_match_telegram.py` no longer exist
- [ ] `telegram_formatter/__init__.py` docstring updated; no re-exports of archived formatters
- [ ] `infra/vehicle_matcher.py` is 312 lines (slimmed) in the production tree
- [ ] `infra_vehicle_matcher_full-2026-08-20.py` exists in the archive with 594 lines
- [ ] `ruff check` is clean
- [ ] `pytest` passes (~625 tests, down from 782)
- [ ] Listener restarted on Phase.103 code, `/health` + `/status` green
- [ ] First live alert behaves as before (no regression in match path)
- [ ] Commit pushed to `deruyter:3000/jill/ai_camera_monitor.git`
- [ ] PLAN.md §11.31 documents the cleanup

## Rollback

If anything goes wrong, restore from archive (one `mv` per item, runnable in any order):

```bash
# Restore the new package
mv ~/archive/2026-08-20-vehicle-matcher-paused-migration/vehicle_matcher_pkg <install-path>/ai_camera_monitor/vehicle_matcher

# Restore the parallel pipeline
mv ~/archive/2026-08-20-vehicle-matcher-paused-migration/pipeline_pkg <install-path>/ai_camera_monitor/pipeline

# Restore the Telegram formatters + their tests
mv ~/archive/2026-08-20-vehicle-matcher-paused-migration/telegram_formatter_match_telegram.py <install-path>/ai_camera_monitor/telegram_formatter/match_telegram.py
mv ~/archive/2026-08-20-vehicle-matcher-paused-migration/telegram_formatter_no_match_telegram.py <install-path>/ai_camera_monitor/telegram_formatter/no_match_telegram.py
mv ~/archive/2026-08-20-vehicle-matcher-paused-migration/telegram_formatter_tests_test_match_telegram.py <install-path>/ai_camera_monitor/telegram_formatter/tests/test_match_telegram.py
mv ~/archive/2026-08-20-vehicle-matcher-paused-migration/telegram_formatter_tests_test_no_match_telegram.py <install-path>/ai_camera_monitor/telegram_formatter/tests/test_no_match_telegram.py

# Restore the full infra/vehicle_matcher.py
mv ~/archive/2026-08-20-vehicle-matcher-paused-migration/infra_vehicle_matcher_full-2026-08-20.py <install-path>/ai_camera_monitor/infra/vehicle_matcher.py

# Listener restart
launchctl unload /Users/jill/Library/LaunchAgents/ai.farm.surveillance-listener-refactor.plist
sleep 2
launchctl load /Users/jill/Library/LaunchAgents/ai.farm.surveillance-listener-refactor.plist
```

Production tree is back to 2026-08-19 state (Phase.102). Archive contents are unchanged.

## See also

- `scripts/probe_matcher_comparison.py` — the probe that established production is better than the paused migration.
- `~/archive/2026-08-20-vehicle-matcher-paused-migration/MANIFEST.md` — the archive manifest (written in Step 1, before any moves).
- `PLAN.md §11.31` — the ship notes for Phase.103 (added in Step 10).
- `scripts/bootstrap-launchctl.sh` — the listener restart script (used in Step 9).
- `references/2026-08-20-launchctl-unload-load-works-from-agent.md` (in the <legacy-repo>-workflow skill) — the listener restart procedure used in Step 9.