# Cleanup 2026-09-01: Probe retirement + archive consolidation (§11.91)

## Execution outcome (2026-09-01)

**DONE.** Approved by Note 2026-09-01 ("Oh yes go ahead with that"). Execution followed the phases below; verification clean. Total: **30 files moved to archive, 2 docs updated, 1 new MANIFEST written. Listener (PID 62496) untouched, NO restart needed.**

### What shipped

| Category | Count | Bytes | Destination |
|----------|-------|-------|-------------|
| `_archive_*.py` (rollback-safety files from past refactors) | 8 | ~232 KB | `~/archive/2026-09-01-legacy-code/_archive_consolidation/` |
| `.bak*` (Phase.116 timestamp-fix cutover, 2026-08-26) | 8 | ~245 KB | same |
| `probe_*.py` (per-phase one-shot diagnostics) | 22 | ~147 KB | `~/archive/2026-09-01-legacy-code/_archive_consolidation/probes/` |
| Reworded references | 8 sites in 2 scripts + AGENTS.md Runtime/Phase status | — | `scripts/generate_architecture_diagram.py`, `scripts/enroll_animal.py`, `AGENTS.md` |
| New files | 2 MANIFEST.md | ~10 KB | `~/archive/.../MANIFEST.md`, `~/archive/.../probes/MANIFEST.md` |

### What was NOT retired

- `scripts/probe_quick_classifier.py` — referenced by §11.110 (Phase.110 quick-classifier top-class priority, open work). Will retire when §11.110 ships.
- `data/vehicles/known_vehicles.json.bak-20260829_143907` — data-file safety net from §11.87.7 cutover. Different category: data backup, not code. AGENTS.md Step 3 isolation rule cares about this.

### Verification

- ✅ Pytest sweep: 1354 passed, 2 skipped, 0 failures (`infra/tests/` + `telegram_formatter/tests/` + `vehicle_position/tests/`).
- ✅ Listener `/health`: `{"cameras_loaded":6,"status":"ok"}` (PID 62496, no restart).
- ✅ Python syntax: `generate_architecture_diagram.py`, `enroll_animal.py` parse clean.
- ✅ Module imports: `infra.motion_detector` (shim), `infra.motion_types`, `vehicle_position.motion_detector_impl` all importable.

### Commit plan

2 commits expected:
1. Code + AGENTS.md updates (~32 file changes)
2. PLAN.md status update (this file → status DONE)

---

## Original plan (kept for audit trail)

### Why §11.91

§11.91 is the second half of the legacy-code sweep. §11.90 (DONE, commit
`476e7b9` on private main) retired 6 prod modules + extracted dataclasses.
Remaining cleanup: (a) **`scripts/`** has 23 probe scripts that were
per-phase one-shot diagnostics, plus 8 `_archive_*.py` files (rollback
safety) lingering in the live tree. Per Note's "archive, not delete"
rule, consolidate everything into one archive bucket under
`~/archive/2026-09-01-legacy-code/_archive_consolidation/`.

### Phases (executed in this order)

#### Phase 1 — Reword references (no file moves)

- `scripts/generate_architecture_diagram.py:129,130,136,148,151,191,202` — remove 7 stale references to retired modules from the static diagram walker. Add `infra.motion_types` (new canonical home from §11.90).
- `scripts/enroll_animal.py:64` — docstring reword (point at motion gate + canonical dataclass home).

**Risk:** zero. No import changes.

#### Phase 2 — Archive consolidation (move only, no deletion)

- 8 `_archive_*.py` files from live tree → `~/archive/2026-09-01-legacy-code/_archive_consolidation/`
- 8 `.bak*` files (extension `.bak`, `.bak.v2`) from live tree → same
- 1 `known_vehicles.json.bak-20260829_143907` LEFT IN PLACE (data safety net)
- New MANIFEST section in `~/archive/.../MANIFEST.md`

**Risk:** all 16 files have zero importers (verified). Listener PID 62496 untouched throughout.

#### Phase 3 — Probe retirement (delete 22, keep 1)

- Delete all 23 `scripts/probe_*.py` files. Findings already in PLAN + commits.
- **EXCEPTION**: keep `scripts/probe_quick_classifier.py` — referenced by §11.110.

**Risk:** none. Each probe is standalone (no inter-probe imports).

#### Phase 4 — Doc hygiene

- Update AGENTS.md Runtime section (PID 62496, commit `476e7b9`, last-verified date).
- Update AGENTS.md Phase status table (add §11.90 + §11.91 rows; bump test count 1057 → 1354).
- Add "Execution outcome" section at top of this CLEANUP doc.

**Risk:** none.

#### Phase 5 — Verification

- `git status` clean except for staged changes
- Pytest sweep: 1354+ should pass (no production code touched)
- Listener /health: no restart needed

### Out of scope (deliberately)

- `scripts/generate_architecture_diagram.py` regeneration — only the list of modules is touched; diagram regen is its own step.
- Module-purity splits still flagged in PLAN.md Part 9 — independent work.
