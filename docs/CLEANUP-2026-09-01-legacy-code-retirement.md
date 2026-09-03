# Cleanup 2026-09-01: Legacy code retirement (§11.90 + §11.91)

## Execution outcome (2026-09-01)

**Status:** §11.90 DONE. §11.91 NOT STARTED (pending follow-on inventory doc).

**§11.90 execution evidence:**
- **6 module files moved** to `~/archive/2026-09-01-legacy-code/`
  (with MANIFEST.md): `infra/audit.py`, `infra/frame_selector.py`,
  `infra/vehicle_artifacts.py`, `infra/_telegram_origin.py`,
  `vehicle_identifier/focused_pass.py`,
  `infra/tests/test_motion_detector.py`.
- **`infra/motion_detector.py` slimmed** 618 → 52 lines. Becomes a thin
  dataclass shim re-exporting from `infra/motion_types.py`.
- **2 duplicate dataclass blocks removed** from `vehicle_position/`
  (`motion_detector_impl.py` had a full duplicate, `motion_detector.py`
  adapter had a partial duplicate). All path to `infra/motion_types.py`
  now. `PositionResult` retained (intentional refactor-vocab rename).
- **12 docstring/comment sites reworded** across 8 files: `infra/cleanup.py`,
  `infra/persistent_rtsp.py`, `infra/person_prompt_template.py`,
  `infra/vehicle_matcher.py`, `infra/vision_response.py`,
  `infra/prompt_templates.py` (× 7), `telegram_formatter/vehicle_alert.py`,
  `telegram_formatter/composite_telegram.py`.
- **`pytest`:** 1033 passed, 1 skipped, 0 failures (full infra/ +
  telegram_formatter/ + vehicle_position/ sweep).
- **Listener:** PID 62496, `/health` returns `{"cameras_loaded":6,"status":"ok"}`,
  zero change required (no §11.90 module is imported by the live path).
- **2 git commits** (Q2 bulk-approve): code-moves + docs/PLAN update.
- **Push:** to private `origin/main` only. Public `origin/main` stays
  at `bd5622a` v0.2.1.

**§11.91 follow-on work:** inventory doc TBD after §11.90 ship.
Scope per the stub below: 8 in-tree `_archive_*.py` files + 1 top-level
archive file, 9 untracked `.bak` files, ~7 pre-§11.88 probe scripts.
Pending separate greenlight.

---

## Why (pre-execution, kept intact)

Note directive (2026-09-01): *"We should probably do a round to identify
legacy code that is no longer in use and come up with a plan to remove it
and it's unit tests."*

Surface trigger was the §11.89 pairwise-diff resize refactor, which surfaced
that Phase.115 (2026-08-25) had already removed `infra.motion_detector.detect_motion()`
from the live path — but the module, its tests, and several sibling dead
modules were never retired. This cleanup retires them.

**Two-doc structure** (per agentic-escalation-workflow recommendation):

- **§11.90 (this doc, Phases 1-4)** — module-level legacy retirement: dead
  prod modules + legacy-by-design + their tests. **Low-risk, grep-verified.**
- **§11.91 (separate doc, written after §11.90 ships)** — archive consolidation:
  move existing `_archive_*` files to `~/archive/...` MANIFEST, retire untracked
  `.bak` files, retire pre-§11.88 probe scripts. **Medium-risk, needs verification
  of operator muscle memory.**

## Cleanup contract

Per multi-file-entity-retirement reference, two treatments per occurrence:

| Treatment | When to use |
|-----------|-------------|
| **REMOVE** | The reference is to active runtime config (env var, dict entry, JSON key) or the file itself is dead. Archive via `mv` to `~/archive/2026-09-01-legacy-code/`. |
| **REWORD** | The reference is to a docstring, comment, system prompt, or test fixture string. Keep the explanation but update the example to remove the legacy name. |
| **LEAVE (mark `STATUS: legacy`)** | Module is referenced by docs but not by code. Update header only — do not move the file yet. Move in §11.91 once the archive consolidation sweeps through. |
| **DELETE (untracked only)** | File is untracked in git AND is a transient `.bak` snapshot. Move to `~/archive/2026-09-01-bak-cleanup/` since git has no record. |

## Verification method (per archive-first-workflow Step 0)

For each candidate module, this grep pattern proves zero live importers:

```bash
grep -rn --include="*.py" \
  -E "from <MODULE>\b|import <MODULE>\b|<MODULE>\.[a-zA-Z_]+(\(| |\b)" \
  --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=.git .
```

A candidate is RETIRED only if this grep returns zero hits in `infra/`,
`listener/`, `vehicle_*/`, `telegram_formatter/`, `pipeline/`, or `scripts/`
after filtering out archive files and the architecture-diagram walker.

---

## §11.90 — Module-level legacy retirement

### Phase 1 — Dead prod modules (zero importers, stale `STATUS: stable` headers)

These five modules have **zero live importers** and their headers still claim
`STATUS: stable` — the headers were written before the system evolved past them.

| File | LOC | Header claim | Reality |
|------|-----|--------------|---------|
| `infra/audit.py` | ~180 | `STATUS: stable` | 0 importers anywhere; only mentioned in 4 docstrings. Last touched: unknown. |
| `infra/frame_selector.py` | ~120 | `STATUS: stable` | 0 importers; only `scripts/generate_architecture_diagram.py` (static walker) references it. |
| `infra/vehicle_artifacts.py` | ~250 | `STATUS: stable` | 0 importers; only motion_detector docstring references it. |
| `vehicle_identifier/focused_pass.py` | ~200 | `STATUS: legacy — DEAD CODE in the slim listener.py post-6B.105c.` | 0 importers; only 2 archive files reference it. |
| `infra/_telegram_origin.py` | ~150 | No STATUS header | 0 importers except `listener/_notifier_archive_6B150.py` (already archived). |

**Action per file:**

| File | Action |
|------|--------|
| `infra/audit.py` | Move to `~/archive/2026-09-01-legacy-code/infra_audit.py`. Pre-move: rewrite header to `STATUS: legacy — removed 2026-09-01, no live importers`. |
| `infra/frame_selector.py` | Move to `~/archive/2026-09-01-legacy-code/infra_frame_selector.py`. Header rewrite same. |
| `infra/vehicle_artifacts.py` | Move to `~/archive/2026-09-01-legacy-code/infra_vehicle_artifacts.py`. Header rewrite same. |
| `vehicle_identifier/focused_pass.py` | Already correctly marked `STATUS: legacy`. Move to `~/archive/2026-09-01-legacy-code/vehicle_identifier_focused_pass.py`. |
| `infra/_telegram_origin.py` | Add `STATUS: legacy` to header. Move to `~/archive/2026-09-01-legacy-code/infra__telegram_origin.py`. |

**Why this is safe:** No file imports them. The grep verification at the top
of this doc is the proof. Reverting is `mv ~/archive/2026-09-01-legacy-code/<file> <original-path>`.

**Verification:**
```bash
# Before any move: prove zero importers (re-run from repo root)
for m in audit frame_selector vehicle_artifacts; do
  count=$(grep -rn --include="*.py" \
    -E "from infra\.$m\b|import infra\.$m\b|infra\.$m\.[a-zA-Z_]+" \
    --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=.git \
    . | wc -l)
  echo "infra.$m: $count hits"
done

# After moves: listener still healthy
curl -s http://localhost:8090/health
# Expected: {"cameras_loaded":6,"status":"ok"}
```

### Phase 2 — Legacy-by-design modules (motion_detector + test)

| File | LOC | Header claim | Reality |
|------|-----|--------------|---------|
| `infra/motion_detector.py` | ~620 | `STATUS: legacy (multi-job: motion detect + blob tracking + bbox crop generation — see KNOWN VIOLATIONS)` | 0 live importers; called only by 2 archive files + 1 probe script. Phase.115 removed it from live path. |
| `infra/tests/test_motion_detector.py` | ~17 tests | — | Tests the legacy module. |

**Action per file:**

| File | Action |
|------|--------|
| `infra/motion_detector.py` | Move to `~/archive/2026-09-01-legacy-code/infra_motion_detector.py`. Header already says `STATUS: legacy`. |
| `infra/tests/test_motion_detector.py` | Move to `~/archive/2026-09-01-legacy-code/infra_tests_test_motion_detector.py`. Add header `STATUS: tests for retired motion_detector module`. |

**Why this is safe:** The live path uses `vehicle_position/motion_detector_impl.py`
(NOT `infra/motion_detector.py`). `frame_diff.py` (live pairwise diff) is also
independent — verified by grep: zero `infra.motion_detector` imports in `listener/`.
The legacy module is preserved for the audit trail (Phase.115 context, the
resize discussion that motivated this cleanup).

**Verification:**
```bash
# 1. Re-run live-pairwise-diff tests (should still pass — they use frame_diff, not motion_detector)
PYTHONPATH=. ./.venv/bin/python -m pytest \
  infra/tests/test_frame_diff.py \
  infra/tests/test_frame_diff_6B171.py \
  listener/tests/test_motion_gate_pipeline_6B144.py \
  -v
# Expected: 37+ tests pass

# 2. Confirm no live caller of infra.motion_detector (re-grep after move)
grep -rn --include="*.py" \
  -E "from infra\.motion_detector\b|import infra\.motion_detector\b|infra\.motion_detector\.[a-zA-Z_]+" \
  --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=.git \
  . | grep -v archive
# Expected: zero hits (only references in docstrings of motion_visualization.py,
#           prompt_templates.py, vision_response.py — all docstrings, not imports)

# 3. Listener still healthy
curl -s http://localhost:8090/health
```

### Phase 3 — Docstring/comment REWORD sweep

Several live files reference the now-removed modules by name in docstrings.
These are not imports but they will mislead future readers ("Why does
`motion_visualization.py` mention `infra.motion_detector` if that module
doesn't exist anymore?").

| File | Line | Current | New |
|------|------|---------|-----|
| `infra/motion_visualization.py` | docstring | references `infra.motion_detector` | Update to reference `infra.frame_diff` (the live pairwise diff) |
| `infra/prompt_templates.py` | docstring | references `infra.motion_detector` | Update to reference `vehicle_position/motion_detector_impl` |
| `infra/vision_response.py` | docstring | references `infra.motion_detector` | Same |
| `telegram_formatter/vehicle_alert.py` | docstring | references `infra.motion_detector` | Same |
| `telegram_formatter/composite_telegram.py` | docstring | references `infra.motion_detector` | Same |

**Action:** REWORD only. No code changes. Update each docstring to point at
the live module so future readers don't chase a deleted file.

**Verification:**
```bash
# Confirm all docstring references are updated
grep -rn --include="*.py" "infra\.motion_detector\|infra\.audit\|infra\.frame_selector\|infra\.vehicle_artifacts" \
  --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=.git . | grep -v "archive/"
# Expected: only comments that are intentional historical references (none expected after this phase)
```

### Phase 4 — Test isolation check

After Phases 1-3, run the **listener** tests + every test file that imports
or transitively touches the removed modules. Verify pass count baseline holds.

**Verification:**
```bash
# Listener + live pipeline tests (the ones that must continue to pass)
PYTHONPATH=. ./.venv/bin/python -m pytest \
  listener/tests/test_motion_gate_pipeline_6B144.py \
  listener/tests/test_person_event_pipeline_6B106.py \
  listener/tests/test_gate_aware_capture.py \
  infra/tests/test_frame_diff.py \
  infra/tests/test_frame_diff_6B171.py \
  infra/tests/test_persistent_rtsp.py \
  infra/tests/test_image_prep.py \
  infra/tests/test_motion_visualization.py \
  -v
# Expected: all pass; baseline was 130+ in this set

# Listener smoke
curl -s http://localhost:8090/health
# Expected: ok
```

---

## §11.91 — Archive consolidation (separate doc, written after §11.90)

Defers to a follow-on inventory doc. Will cover:

- **Move existing `_archive_*` files** (8 files in `listener/` + 1 top-level)
  to a single `~/archive/2026-09-01-archive-consolidation/` with MANIFEST.md
- **Retire untracked `.bak` files** (9 files, never committed)
- **Retire pre-§11.88 probe scripts** (`probe_phase_6B1XX_*.py` for phases
  that have shipped)

Why this is separate: probe scripts may still be in operator muscle memory
even if no code path imports them. Need per-script verification with Note
before retirement.

---

## Risks and guardrails

| Risk | Mitigation |
|------|------------|
| Live listener breaks | Phases 1-3 only move files with verified-zero importers. Phase 4 runs the listener test suite + `/health` curl after each phase. Listener can be hot-restarted if needed (`launchctl kickstart -k gui/$(id -u)/ai.farm.surveillance-listener-refactor`). |
| Architecture diagram goes stale | `scripts/generate_architecture_diagram.py` walks modules statically — after Phase 1 moves, regenerate the HTML. Pre-emptively listed `infra.audit` and `infra.vehicle_artifacts` as removed so the diagram drops them. |
| AGENTS.md mentions removed modules | `AGENTS.md` line 169 mentions `infra.audit`'s retention logic — update that reference to "removed in §11.90." |
| Note's "investigator muscle memory" looks for `infra.audit` after cleanup | The MANIFEST.md at `~/archive/2026-09-01-legacy-code/MANIFEST.md` lists the original paths + restore commands. Searching the archive dir is one `ls` away. |

## Commit strategy (bulk, 2 commits)

Per archive-first-workflow default — bulk, test-coupled phases together:

- **Commit 1 (Phases 1-3):** "§11.90 — retire dead prod modules + motion_detector." All file moves + docstring reword in one commit. Test isolation check (Phase 4 verification) runs before commit.
- **Commit 2 (Phase 4 verification artifact):** "§11.90 — verification: N tests pass, listener healthy on PID X." Curl + pytest output captured in commit message body.

Per-phase commits NOT chosen because: (a) Phases 1-3 are all "remove legacy" with the same risk profile; (b) test-coupled changes (Phase 1+4) would split unnecessarily.

## Final smoke test

1. `cd <install-path>/ai_camera_monitor && pytest listener/tests/ infra/tests/test_frame_diff.py infra/tests/test_frame_diff_6B171.py infra/tests/test_motion_visualization.py infra/tests/test_persistent_rtsp.py infra/tests/test_image_prep.py` → all green
2. `curl -s http://localhost:8090/health` → `{"cameras_loaded":6,"status":"ok"}`
3. `ls ~/archive/2026-09-01-legacy-code/` → MANIFEST.md + 7 files
4. `git log --oneline -3` → §11.90 commit + verification commit + §11.88 baseline

## Rollback

Single-command per file:

```bash
# Example: restore infra/audit.py
mv ~/archive/2026-09-01-legacy-code/infra_audit.py infra/audit.py

# Full restoration (if Note wants the modules back wholesale)
mv ~/archive/2026-09-01-legacy-code/* <original-paths-per-MANIFEST.md>
```

Each restored file keeps its pre-archive header (we wrote `STATUS: legacy`
without deleting the original `STATUS: stable` line — wait, actually we did
overwrite the header. If Note wants the original headers back, that's in git
history at commit `10c3896` — `git checkout 10c3896 -- <path>`).

## Status

- [ ] Phase 1 — 5 dead prod modules moved
- [ ] Phase 2 — motion_detector + tests moved
- [ ] Phase 3 — docstring rewords (5 files)
- [ ] Phase 4 — test isolation verified
- [ ] §11.91 inventory doc written (separate)

## See also

- `docs/CLEANUP-2026-08-23-listener-deadcode.md` — prior cleanup pattern (template)
- `docs/CLEANUP-2026-08-30-architecture-diagram-staleness.md` — prior cleanup pattern
- `PLAN.md §11.89` — original pairwise-diff plan, REDIRECTED to §11.90+§11.91
