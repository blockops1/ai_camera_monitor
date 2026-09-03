# ai_camera_monitor — Agent Operating Guide

> Auto-injected into the agent system prompt whenever workdir = this repo.
> Follow on EVERY session, including compaction-restore sessions.

## Step 1 — load skills + read the plan (do this FIRST, no exceptions)

skill_view name=refactor-module-header   # Standard docstring format for every module in this repo
skill_view name=jill-workflow-style      # Multi-step task execution style for Note
skill_view name=reolink-camera-config    # RLC-510A / RLC-833A config + canonical operator-script HOW-TO (added 2026-08-30, Phase.166 §11.87)

PLAN=PLAN.md
test -f "$PLAN" || PLAN=$(find . -maxdepth 2 -name 'PLAN.md' | head -1)
test -n "$PLAN" && echo "Plan: $PLAN" || echo "NO PLAN FOUND"

Do not skip. Compaction summaries go stale — Note has flagged this multiple times.

## Step 1.5 — module-header standard (added 2026-08-12)

Note: *"I like what you put for documentation in the header, but if there's something that's more useful or standard that would be helpful, then I would like you to recommend adding that."*

Every Python module in this repo (infra/, listener/, vehicle_position/, vehicle_identifier/, vehicle_matcher/, known_vehicles/, telegram_formatter/, pipeline/) MUST start with a standardized docstring. The authoritative format lives in the `refactor-module-header` skill (load it with `skill_view name=refactor-module-header`).

**Mandatory sections in fixed order:**

1. `[Module name] — [one-line purpose]`
2. `STATUS:` — one of `stable | provisional | legacy | experimental`
3. `THREAD SAFETY:` — one of `thread-safe | single-threaded | uses threading.Lock`
4. `INPUTS:` — files, env vars, function args (note required vs optional)
5. `OUTPUTS:` — return values, files written, network calls, side effects
6. `PUBLIC API:` — every public function signature with short description (skip for test files)
7. `DOES NOT DO:` OR `KNOWN VIOLATIONS (see PLAN.md Part 9):` — explicit non-goals
8. `CALLED BY:` — fan-in (which modules import this one)
9. `CALLS INTO:` — fan-out (what this module depends on)
10. `RELATED:` (optional) — data classes / files / contracts this module reads or writes

**For modules with multiple jobs** (the 9 modules flagged in PLAN.md Part 9): use `KNOWN VIOLATIONS (see PLAN.md Part 9):` instead of `DOES NOT DO:`. Honest about the gap. Intent-only docs become stale the moment someone reads the code and finds the function is right there.

**Workflow rule:** new module → write the full header BEFORE writing any code. Module being modified → update the header in the same commit. Module being split → new sub-modules get fresh headers; the old module's header is replaced with a "split into:" note pointing at the new files.

## Step 2 — source-of-truth docs (read before answering "is X done?")

- `PLAN.md`                    — refactor architecture, cutover plan, module-purity review (Part 9), open questions
- `README.md`                  — repo purpose, file layout, what's copied vs new
- `ARCHITECTURE.md`            — deep doc: data flow, matcher schema, persistence, ops runbook (current state only)
- `listener-architecture.html` — module logic diagram (regenerate after every structural change)

## Step 3 — isolation rule (added 2026-08-11)

This repo is **completely self-contained**. Nothing in `infra/`, `listener/`, `vehicle_position/`, `vehicle_identifier/`, `vehicle_matcher/`, `known_vehicles/`, `telegram_formatter/`, `pipeline/` may reference `<legacy-repo>/` or anything under that path. The old repo stays untouched for rollback.

**Public/private split (added 2026-08-30, v0.1.0 → v0.1.2 public release).** This is the **private internal version** of the surveillance listener. The **public release** lives at https://github.com/blockops1/ai_camera_monitor — scrubbed per `devops/open-source-release-pipeline` SKILL.md. Public repo has 96 tracked files (vs 240 here), 0 tests, 0 archive files, 0 internal docs, 0 operator-path scripts. Single squashed commit (`dab8311`) authored as `name one <rolf@blockoperations.com>`. **Rule:** any change that ships here must also be PII-clean to land in public, or be marked public-private in this AGENTS file. See Step 4.6 for the archaeology-comment scrub rule that surfaced during the v0.1.1 → v0.1.2 round.

**Files copied from old repo** (`known_vehicles.json`, `alert_overrides.json`, infra modules) are exact snapshots at copy time. The refactor reads/writes its own copies in `data/` and `config/`. No symlinks, no shared mounts.

**Verification before any cutover:**

```bash
grep -rn "<legacy-repo>/" <install-path>/ai_camera_monitor/ \
    --include="*.py" --exclude-dir=__pycache__ --exclude-dir=.git
# Expected: zero hits (PLAN.md and this file may mention it for context; that's fine)
```

**Known-store files are not free-form JSON.** (added 2026-08-30, Phase.166 §11.87.7 post-mortem.) Every script that **reads or writes** `data/vehicles/known_vehicles.json`, `data/animals/known_animals.json`, or `data/identities/*.json` MUST go through the canonical store class (`known_vehicles.store.KnownVehicleStore`, `known_animals.store.KnownAnimalStore`, `identities.store.IdentityStore`). The store classes own the on-disk schema (`{"version": N, "vehicles": [...]}`). Writing raw JSON via `json.dump([...], f)` silently corrupts the file — the next consumer (matcher, scorer, listener, frame archive) reads via the store and crashes on `AttributeError: 'list' object has no attribute 'get'`. The pre-§11.87.7 `scripts/enroll_vehicle_from_alert.py` had this bug; commit `0bb3260` rewrote it to use the store. Same family of scripts (`enroll_person.py`, `enroll_animal.py`) already follows this rule.

## Step 4 — design rule for modules (added 2026-08-11)

Note: *"each of the different functions are designed to stand alone modules or stand alone scripts, so that when we test one of the modules or scripts, we don't have to run the full battery of tests. We only run the tests that are done for that module or that script."*

**Each module does one thing, does it well, does not do other things.** When a module needs a new behavior, decide first which module owns it. If no module does, create one. If two modules want the same behavior, the behavior belongs in a third module both call.

**Module-purity review** lives in `PLAN.md Part 9`. It currently flags 9 violations. Each is a follow-on task, not a blocker for cutover — the listener works with the violations in place, the split happens afterward.

## Step 4.5 — module-purpose discipline (added 2026-08-14, 6B.78)

Note: *"each module has to have one purpose and you have to understand the purpose of that module before you work on it and you don't introduce other things into it."*

Step 4 says each module does one thing. Step 4.5 says: **before you edit a module, read its header and confirm your edit fits inside the module's stated purpose.**

### How to apply

1. **Read the module header FIRST.** The `PUBLIC API:` + `DOES NOT DO:` (or `KNOWN VIOLATIONS`) sections define the contract.
2. **If your edit isn't covered by the header, you are introducing a new purpose into the module.** Stop and decide: does this purpose belong here? If unsure, no — find the right module or create one.
3. **"I was already in the file" is not a justification.** Adjacent-module edits belong in a separate commit, against a separate purpose.

### Concrete anti-patterns (Phase.78)

- **`vision_response.py`** parses Qwen's JSON. It does NOT decide what fields mean. If Qwen returns a motion field, the schema should not have asked for it — fix the schema, don't add a warning log in the parser.
- **`vehicle_identifier/identifier.py`** identifies vehicles. It does NOT detect motion. If the matcher needs motion info, it asks `motion_detector.MotionResult`, not vision.
- **The listener** orchestrates. It does NOT re-implement motion logic, blob tracking, or vision parsing. If a cross-module value is needed, it's already on the public API of one of those modules.

### When in doubt

Ask: "which module's purpose does this change serve?" If the answer is "two modules," split the commit. If the answer is "none of them — it's plumbing," it might be a new module.

## Step 4.6 — public-safe archaeology comments (added 2026-08-30, public release v0.1.1 → v0.1.2)

Note's principle (the v0.1.0 push shipped operator-specific test data in three archaeology comments): **debugging-session context is not free of PII.** Comments like `# Note says "give me an OFS snapshot" in chat; Jill curls /snapshot?camera=OFS` survive all sanitization passes because they look like legitimate technical notes — but they leak operator first names, specific vehicle descriptions, and operator pseudonyms.

**Rule for any code destined for public release:**

1. **Operator name in comments → "Operator".** Never `Jill`, `name one`, `Note`, `Note`, `carson`, etc. Use `"Operator"` (no possessive form leaks either — never `"Note's X"`, use `"Owner's X"`).
2. **Specific vehicles in comments → generic placeholder.** Never `"Tesla Model Y blue"`, `"Ford F-150 black"`. Use `"make+model+color"` literal phrasing (`"make+model+color with high confidence"`) or `"Owner's <type> <color>"` (`"Owner's blue sedan"`).
3. **Operator pseudonym + vehicle combinations → generic pairing.** Never `"Note's blue Tesla"`, `"Note's F-350"`. Use `"Owner's blue sedan"` etc.
4. **Keep alert IDs and timestamps.** They don't identify the operator and are useful debugging context.

**Why this matters:** filter-repo's `--replace-text` only catches strings in the sanitization-map. Archaeology comments with operator names or specific vehicles will ship to public unless scrubbed at the source. The v0.1.2 round caught three leaks: `listener/listener.py:290` ("Jill curls"), `infra/vision_schema_lift.py:48` ("Tesla Model Y blue conf=0.98"), `scripts/enroll_vehicle_from_alert.py:150` ("Note's blue Tesla"). Each looked like a debugging note but each was operator-PII.

**Pre-commit scrub probe (run before any commit that may go to public):**

```bash
grep -rEn '# (Note|Mr\. V|Ms\. V|Mister|Owner|operator|name one|Jill|name two|name three|name four)\b' \
    --include='*.py' --include='*.md' .
grep -rEn '\b(Tesla|Ford|Chevy|Honda|Toyota|Ram)\b.*(Model|F-?150|F-?350|Silverado|Tacoma|Tundra|Rav4|Civic|Corolla).*(blue|red|white|black|green|silver|gray|grey)' \
    --include='*.py' .
grep -rEn '(Note|Owner|Mr|Ms|Mrs)\s*['']s?\s*(blue|red|white|black|silver|gray)' \
    --include='*.py' --include='*.md' .
```

Zero hits before any public-bound commit.

## Step 5 — verify state before claiming "still working"

```bash
lsof -iTCP:8090 -sTCP:LISTEN                          # listener alive?
ls -la <install-path>/ai_camera_monitor/.venv  # refactor-local venv exists?
<install-path>/ai_camera_monitor/.venv/bin/python -c "import infra.paths; print(infra.paths.PROJECT_ROOT)"
# Expected: <install-path>/ai_camera_monitor
```

If any check fails, STOP and diagnose. Do not assume past success = current success.

## Step 5.5 — read the consolidated log, not the launchd pipe (added 2026-08-27)

Note: *"Doesn't all the scripts logged to a common log in a known good location, and as part of the log entry don't they start with what script is producing the log and the local date and time? I thought this project had a consolidated log in a known good location, and had log rotation set up."*

**Canonical log**: `logs/listener.log` — produced by `infra/logging_setup.py::configure_logging()`. Format is `[<full.dotted.module.name>] [LEVEL] [YYYY-MM-DD HH:MM:SS]` and the file is rotated **25 MB × 5 backups** by `RotatingFileHandler` attached to **ROOT**.

**Not the canonical log**: `logs/launchctl-stderr.log` — that's the raw launchd stderr pipe. It has no timestamps, no module names, no level, no rotation. Use it ONLY for bootstrap crash debugging (Tracebacks from import errors, before the logger was wired).

**Sibling modules** (40+ files in `infra/`, `listener/`, `telegram_formatter/`, `vehicle_*/`) use raw `logging.getLogger(__name__)`. They propagate to root, hit the file. No per-module wiring needed. 28 distinct module names appear in production logs, confirming the design works.

**For any "what happened?" investigation**, default to:

```bash
tail -n 200 logs/listener.log                          # last 200 entries
grep -E '<pattern>' logs/listener.log                  # search current
grep -E '<pattern>' logs/listener.log.1                # search prior rotation cycle
grep -oE '<pattern>' logs/listener.log | sort | uniq -c | sort -rn  # count by reason
```

If you find yourself reading timestamps from `launchctl-stderr.log`, STOP — you're looking at the wrong file. Full skill: `<legacy-repo>-canonical-log`.

## Anti-patterns (added 2026-08-12)

- **Don't import from `<legacy-repo>/`.** That defeats the isolation. If you find yourself wanting to, copy the module over instead.
- **Don't run both listeners simultaneously.** Old and refactor both bind :8090. Second start fails with `OSError: [Errno 48] Address already in use`.
- **Don't write tests that span multiple modules.** Per design rule, each module has its own tests. Integration tests live in `tests/integration/` and are explicitly labeled as such.
- **Don't write raw JSON to a known-store file.** (added 2026-08-30, §11.87.7.) Use the canonical store class. See the §11.87.7 post-mortem note in §3 above.
- **Don't lump a code change + a doc change into one commit — except for module headers.** Headers and code MUST land in the same commit (header drift is the documented failure mode that led to the 2026-07-26 docs-after-push rule). For non-header docs (PLAN.md, AGENTS.md, README, ARCHITECTURE), use **a separate commit per doc file** so a stray doc edit can't pollute a behavior change. PLAN.md commit for §11.86.4 → `b2c8e91`, §11.87.8 → `e8f477d`. AGENTS.md update commits: post-cutover review (`a1b2c3d` style) are themselves doc-only commits.
- **Don't update AGENTS.md or PLAN.md without Note's approval.** (Note 2026-08-10.) Both files are part of the change but require explicit greenlight before write. The current edit was greenlit in chat on 2026-08-30 ("review the AGENTS file to make sure it is current and correct, then update it").

## Quick commands

```bash
# Verify / stop / start the refactor listener (managed by launchd since cutover 6A.97+).
# LaunchAgent has KeepAlive:True + SuccessfulExit:False, so SIGTERM auto-restarts
# within ~5s. To FORCE a stop (no auto-restart) use launchctl bootout:
#   launchctl bootout gui/$UID/ai.farm.surveillance-listener-refactor
# To start again:
#   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.farm.surveillance-listener-refactor.plist
# For a quick restart on a new commit (preserves auto-restart):
#   PID=$(lsof -tiTCP:8090 -sTCP:LISTEN) && kill -TERM $PID   # launchd restarts on new code
# For manual boot (NOT via launchd, used for one-off probes):
cd <install-path>/ai_camera_monitor && ./.venv/bin/python listener/listener.py

# Run all tests
cd <install-path>/ai_camera_monitor && ./.venv/bin/python -m pytest

# Run tests for ONE module (the design rule)
cd <install-path>/ai_camera_monitor && ./.venv/bin/python -m pytest infra/tests/test_paths.py

# Run tests for one listener route
cd <install-path>/ai_camera_monitor && ./.venv/bin/python -m pytest listener/tests/test_listener.py -k health

# Check for cross-repo coupling (must be zero hits)
grep -rn "<legacy-repo>/" <install-path>/ai_camera_monitor/ \
    --include="*.py" --exclude-dir=__pycache__ --exclude-dir=.git

# Lint gate before commit
cd <install-path>/ai_camera_monitor && ./.venv/bin/python -m ruff check infra/ listener/

# Verify all infra modules import cleanly
cd <install-path>/ai_camera_monitor && PYTHONPATH=. ./.venv/bin/python -c "import infra.paths, infra.notifier, infra.vehicle_state, infra.vehicle_matcher, infra.vision_analyzer; print('OK')"

# Regenerate the module logic diagram (after structural changes)
cd <install-path>/ai_camera_monitor && ./.venv/bin/python scripts/generate_architecture_diagram.py

# Verify enroll_vehicle_from_alert.py writes the canonical dict-wrapped schema
# (Phase.166 §11.87.7 — never write raw JSON to known-store files).
# Smoke recipe: see §11.87.7 in PLAN.md.
cd <install-path>/ai_camera_monitor && PYTHONPATH=. ./.venv/bin/python - <<'PY'
import json, os, tempfile
from scripts.enroll_vehicle_from_alert import find_alert, enroll
tmp_alerts = tempfile.mkdtemp()
date = "2026-08-30"
with open(os.path.join(tmp_alerts, f"{date}.jsonl"), "w") as f:
    f.write(json.dumps({
        "alert_id": "TEST-001",
        "camera": "OFS",
        "vehicle_attributes": {"make": "Ford", "model": "F-150", "color": "black", "type": "pickup"},
        "frame_path": "/tmp/synthetic.jpg",
    }) + "\n")
tmp_known = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
# Pre-write a valid (empty) known-store file so from_file() can read it
with open(tmp_known, "w") as f:
    json.dump({"version": 1, "vehicles": []}, f)
alert = find_alert("TEST-001", alerts_dir=tmp_alerts)
enroll(alert, "Smoke Truck", overrides={}, known_file=tmp_known)
with open(tmp_known) as f:
    data = json.load(f)
assert isinstance(data, dict) and data.get("version") == 1 and "vehicles" in data
assert any(v.get("id") == "v_smoke_truck" for v in data["vehicles"])
print("OK — canonical schema verified")
PY
```

## Conventions

- **Python**: 3.14+ (matches old repo's venv). Refactor has its own `.venv/` — never import from `<legacy-repo>/.venv/`.
- **Paths**: every path goes through `infra/paths.py`. No hardcoded paths anywhere else.
- **Env vars**: secrets (Telegram bot token, chat ID, RTSP URLs, <nas> creds) loaded from `~/.env`. Confirm the action, never the value.
- **Logging**: `logging.getLogger(__name__)`. Never `print()` for diagnostics.
- **Tests**: each module gets its own `tests/test_<module>.py`. Run with `pytest <module>/tests/` to test in isolation.
- **Lint**: `ruff check infra/ listener/`. Zero errors before commit.
- **Git**: never pull from remote; push only with explicit permission.
- **Telegram origin tagging**: every Telegram message MUST carry `[<script_filename>] [<CHANNEL_LABEL>]` prefix. Helper: `infra/_telegram_origin.py::origin_prefix(channel_label)`.
- **Docs**: AGENTS.md + PLAN.md + module headers all updated BEFORE any push, not "remember to update later."
- **Parameterization rule** (added 2026-08-30, Phase.166 §11.87, Note): every operator script in `scripts/` exposes its config in three layers, with this precedence (highest → lowest):
    1. **CLI flags** — per-invocation overrides (`--webhook-url`, `--recipe-path`, `--alerts-dir`, ...). Default to `None` so the layer below resolves.
    2. **JSON recipe** — documented fleet/per-target defaults in `config/motion_recipe.json` (and similar). Read at call time, not import time, so an edit + re-run picks it up without a redeploy.
    3. **Env vars** — only for **stable infra/secrets** (camera IPs, webhook URLs, RTSP creds). Never for fleet-wide motion tuning.
  The reasoning (Note): "I want to look at ONE file and see per-camera/per-target settings without reading script source. JSON in git for stable defaults; CLI flag overrides don't pollute the file."

## Runtime

- venv: `~/ai_camera_monitor/.venv/` (Python 3.14) — always absolute path
- Listener: ACTIVE on commit `476e7b9` (Phase §11.90, 2026-09-01, PID 62496 — last verified at 2026-09-01 15:49 EDT), managed by launchd LaunchAgent `ai.farm.surveillance-listener-refactor` with auto-restart on SIGTERM. Old listener (`<legacy-repo>/`) is dormant; old plist is on disk but NOT loaded (`launchctl print` returns "Could not find service"). Rollback: load the old plist, bootout the refactor plist (see Quick commands above).
- Cutover: refactor plist has been the active one since Phase 6A.97+ (early Aug 2026). The "stop old → start refactor → verify → stop refactor → start old → verify" sequence in PLAN §4 is HISTORICAL — use it only for true rollback, not routine restart.
- Two listeners NEVER run simultaneously.
- Endpoint: :8090, POST `/alert`, GET `/health`, GET `/status` (refactor ports match old for rollback simplicity)
- Phase.166 §11.87 (parameterization of 8 operator scripts) is CLOSED. All scripts in `scripts/` route through `infra.camera_creds`, `infra.recipe`, `infra.paths`, and their canonical store classes. See PLAN.md §11.87 for per-commit detail.

## Phase status

See `PLAN.md` for full detail. As of 2026-09-01:

| Phase | Scope | Status |
|---|---|---|
| A | Module copy from `<legacy-repo>/` | DONE |
| B | Module-purity split (9 modules flagged in Part 9) | PARTIAL — flagged but not blocking; cutover succeeded with violations in place |
| C | Module headers (`refactor-module-header` standard) | DONE for all new + modified modules |
| D | Per-module tests | DONE — 1033 passing in `infra/tests/` + `telegram_formatter/tests/` + `vehicle_position/tests/`, 1 skipped |
| E | Listener tests | DONE |
| F | Parity test (refactor matches old behavior) | DONE (cutover commit was Phase 6A.97) |
| G | Cutover (refactor listener takes traffic) | DONE — early Aug 2026 |
| **6B.166** | **Operator-script parameterization (§11.86 + §11.87)** | **DONE** — CLI flags + JSON recipe + env vars. Commits `f47554d` … `e8f477d`. Live smoke verified (canonical schema, dry-run, dup detection, all 8 scripts parameterized). |
| **§11.90** | **Legacy-code sweep Pt 1: prod-module retirement** | **DONE** — Retired 4 prod modules (`infra/audit`, `infra/frame_selector`, `infra/vehicle_artifacts`, `infra/_telegram_origin`) + `vehicle_identifier/focused_pass` + `infra/tests/test_motion_detector.py`. Extracted `MovingObject` + `MotionResult` dataclasses to new `infra/motion_types.py` (single source of truth; `infra/motion_detector.py` reduced to 53-line shim). 19 files changed, +507 / −2505 lines. Pushed to private `origin main` at commit `476e7b9` (2026-09-01). See `docs/CLEANUP-2026-09-01-legacy-code-retirement.md`. |
| **§11.91** | **Legacy-code sweep Pt 2: probe retirement + archive consolidation** | **DONE** — Retired 22 probe scripts (kept `probe_quick_classifier.py` for §11.110). Consolidated 8 `_archive_*.py` files + 8 `.bak*` files into `~/archive/2026-09-01-legacy-code/_archive_consolidation/`. Reworded 7 references in `scripts/generate_architecture_diagram.py` + 1 in `scripts/enroll_animal.py`. See `docs/CLEANUP-2026-09-01-probe-archive-consolidation.md`. |
| **§11.111** | **Vehicle pipeline extraction** | **DONE** — `listener/vehicle_event_pipeline.py` (1554-line monolith) → `listener/vehicle_pipeline/` package (9 submodules: `motion`, `vision`, `crop`, `identity`, `match`, `telegram`, `alert`, `enrichment`, `__init__`). Pure refactor, zero behavior change. 18 symbols remapped. listener.py L1828-1832 + L1914-1918 import updates. Pushed `965800e`, PLAN.md update `9f61120`, README `467afd0`. |
| **§11.113** | **Prompt-leakage test (Variant A vs B)** | **DONE** — Variant A unified single-call prompt (`infra/unified_vision.py`, experimental) leaked 67% (2/3 person-scene alerts confabulated non-primary *_features values from scene BG). Variant B = current per-class pipelines selected. Probe `scripts/probe_variant_leakage.py`. 27 unit tests for Variant A module kept as empirical record. |
| **§11.114** | **Implementation based on §11.113 result** | **DONE — no production change** — Variant B = current per-class pipeline confirmed correct. §11.114.1-6 sub-phases cancelled. Phase.129a event-promotion workaround stays. YOLO stays suppress-only. Experimental code (`infra/unified_vision.py`, `scripts/probe_variant_leakage.py`, `infra/tests/test_unified_vision_6B113.py`) retained with `STATUS: experimental` per `refactor-module-header` skill v1.1.0. |

Open work for the next phase (per active-tasks.md): §11.110 quick-classifier top-class priority; person-matching tuning (3 deferred bugs from 2026-08-31); camera-1 person cooldown re-tune; module-purity splits flagged in Part 9; new operator scripts must follow the parameterization rule above.
