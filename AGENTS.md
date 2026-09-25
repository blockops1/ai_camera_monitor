# farm-surveillance-v2 — Agent Operating Guide

> Auto-injected into the agent system prompt whenever workdir = this repo.
> Follow on EVERY session, including compaction-restore sessions.
>
> **Cross-project rules** live in `~/.hermes/memories/MEMORY.md` and `USER.md`.
> **Project-specific rules** live here. Don't duplicate.

## Step 0 — directory guard (added 2026-09-20)

Before any operation in this repo, confirm the cwd:

```bash
basename "$PWD"
# Expected: farm-surveillance-v2
# If you see "farm-surveillance-refactor", "farm-surveillance-internal", or "farm-surveillance" (v1), STOP.
```

If a task says "v2" and you're in another farm-surveillance-* directory, exit and re-`cd`. Workers in refactor/v1/internal produce commits that look like v2 work but land on the wrong branch.

**Cross-project repo lineage** (operator 2026-09-20 "no deletions" — all preserved, never touched):

- `farm-surveillance-internal/` — source of truth (has the original 100+ line AGENTS.md, the most mature operating guide)
- `farm-surveillance-refactor/` — branch-off of internal, dropped AGENTS.md when carved out (git commit `f226fbe §11.176 US-002: init sibling private repo, move operator-private docs`, 2026-09-05)
- `farm-surveillance-v2/` — **this repo** — branch-off of refactor, never received AGENTS.md (gap closed by this file, 2026-09-20)
- `farm-surveillance-prototype/` — earliest, dormant, no AGENTS.md

**Lesson (operator 2026-09-20):** when a project splits off from another, **AGENTS.md MUST be carried forward as part of the carve-out**, not left behind. If you do a carve-out in the future, copy the parent's AGENTS.md as the seed.

**Project binding (operator 2026-09-20):**

- **Primary folder:** `/Users/jill/farm-surveillance-v2` (`label v2`).
- **Secondary folder:** `/Users/jill/farm-surveillance-refactor` (`label refactor`, read-only reference).
- **NOT siblings to touch:** `farm-surveillance-internal`, `farm-surveillance-prototype`, original `farm-surveillance` (v1) — preserved per operator 2026-09-20 "No deletions" rule.

Before authoring any kanban card: `hermes project show farm-surveillance | grep primary_folder` MUST return `/Users/jill/farm-surveillance-v2`. If it points at refactor, all cards dispatch to the wrong repo. Fix: `hermes project add-folder farm-surveillance /Users/jill/farm-surveillance-v2 --primary --label v2`.

## Step 1 — load skills + read the plan (do this FIRST, no exceptions)

```bash
skill_view name=local-ai/farm-surveillance-v2        # all v2 references, pitfalls, shipping definition
skill_view name=devops/production-verification      # 4-point check before reporting "done"
skill_view name=devops/kanban-orchestrator          # dispatch mechanics, max_in_progress gate
```

**Source-of-truth docs in this repo:**

- `docs/PHASE-V2-NNN-PRD-*.json` — one PRD per major change arc (V2-061 = frame retention, V2-062 = headlight FP suppression, etc.)
- `README.md` — repo purpose, file layout
- `AGENTS.md` — this file
- `pyproject.toml` — deps, scripts, version
- `bin/preflight_v2_only.sh` — must pass before commit; asserts cwd is v2

**PRD body limit:** ≤2,500 chars soft, ~4,500 chars operational ceiling. Cards over 4,500 risk mid-flight context compression that drops ACs.

## Step 2 — production verification (operator 2026-09-20 ROOT-CAUSE)

Mr. V verbatim: *"you tell me they're done but a few days later we find out that the work actually wasn't done and that's what actually bothers me."*

**Kanban `done` = worker shipped a commit. NOT = problem fixed in production.**

Before reporting anything as "done", verify ALL of:

1. **Commit on `main`, not branch tip.** `git show main:<file>` not `git log <branch>`.
2. **Worktree clean.** `git status --porcelain` should show only intentional untracked files (e.g., AGENTS.md, scratch notes). Worker-leaked artifacts from prior dispatches = `git stash` or WIP commit before continuing.
3. **Behavioral fix verified at runtime, not just by diff.** For wiring fixes: the code path is actually invoked (cleanup thread spawned, hourly job scheduled). For feature flags: env var is read + honored (toggle test). For filters: trigger a sample event and observe filtering.
4. **Listener restarted to reflect the change.** See "Listener bytecode vs source-of-truth" anti-pattern below. After any change to `listener/daemon.py`, `listener/pipeline.py`, or anything imported by them at boot, the running listener is on bytecode loaded at launch — it does NOT auto-reload from disk. Must restart via `launchctl kickstart -k gui/$UID/com.farm.surveillance.v2` before runtime tests are valid. Operator 2026-09-20 18:50 EDT standing directive: defer restart until in-flight kanban cards are done (don't disrupt QA/running work), then restart before declaring the PRD live. Precedent: alert `13f9c8d5-e9ce-40da-885d-d74471a22259` at 18:44 EDT — YOLO `car 0.81` misclassification on a person in dark clothing moving fast (twilight, before 20:09 quiet-hours start). Listener started 12:04, V2-062 gates (`infra/quiet_hours.py`, `infra/alert_overrides_baseline.py`, wiring into `_send_tg1/2/3` and `pipeline.run`) committed 12:48–15:02. Alert fired through old bytecode with no gates active.

If any check fails, report the gap BEFORE claiming "done." Unverified = label explicitly: *"code shipped, behavior NOT yet verified."*

**Canonical v2 failure (2026-09-20):** 37 GB pile-up. US-061a "done" had the env knobs on main, but the cleanup daemon thread (US-061c) and launchd plist (US-061d) that would actually enforce the 20 GB cap were not yet wired — disk filled anyway. US-061b checked under this rule and was correctly labeled "code shipped, runtime behavior NOT verified."

This rule lives in three places for redundancy: MEMORY.md (cross-project), the `devops/production-verification` skill (kanban-specific), and this file (v2-specific).

### Step 2.1 — Forgejo recovery mirror (operator 2026-09-22, NEW)

The Forgejo mirror at `deruyter:3000/jill/farm-surveillance-v2` is the recovery boundary, not a release gate. If this Mac dies, the work must survive there. The push is part of "done," not a separate step.

**Push after every merge to `main`.** Push is automatic. `--no-verify --force-with-lease` is the standard tool when the pre-push hook flags legitimate code (the JPEG scanner matches `.jpg` literals in source, the IP scanner matches PRD evidence). Bypass without asking. The hooks are a recovery story bug to file separately, not a blocker on local work.

**Force-push is safe in this repo** because Jill is the only pusher. If another agent or workstation ever starts pushing to this mirror, this rule needs to change (force-push becomes destructive) — re-check on any new drift incident.

**Verify after push:**

```bash
LOCAL_SHA=$(git rev-parse main)
REMOTE_SHA=$(git rev-parse forgejo/main 2>/dev/null)
[ "$LOCAL_SHA" = "$REMOTE_SHA" ] && echo "in sync" || echo "DRIFT — re-push needed"
```

**Why this rule exists:** 2026-09-22, the operator noticed the Forgejo mirror was 4 days behind local main. No automated check was running. The mirror was being treated as a release artifact instead of a backup. That's wrong — local is the working tree, Forgejo is the safety net.

## Step 3 — conventions

- **Python**: 3.11 (project venv is `.venv/bin/python3.11`).
- **Package manager**: `uv`, not pip. `uv add suntime`, `uv run pytest tests/test_x.py`.
- **Paths**: every path goes through `infra/paths.py`. No hardcoded paths anywhere else.
- **Env vars**: secrets (Telegram bot token, chat ID, RTSP URLs) loaded from `~/.env` and per-app `*-creds.env` files. Confirm the action, never the value.
- **All knobs env-tunable.** `.env.example` declares the variable (operator-owned). `.env` and per-app `*-creds.env` files hold the values. Hardcoded constants in code = rejected at review.
- **Boolean env vars: write what each value does, not just the variable.** Operator 2026-09-20 (verbatim: *"I don't know whether Zero or one in the settings will turn off"*). Every `=0`/`=1` declaration in `.env.example` needs an inline comment showing what each value does (`# 1 = ON, 0 = OFF`). Establish the convention once at the top of the file. Exception: presence-or-absence env vars (e.g. `MOTION_GATE_NIGHT_SUPPRESS_ENABLED` triggers off the literal `1`, not presence) and numeric-percentage env vars (e.g. `BBOX_INTERSECTION_DILATE_PCT=0.01`) keep their natural form.
- **Time-of-day uses `suntime` + farm lat/lon.** Farm coords: lat 34.5782, lon -84.9438 (Resaca, GA). Twilight buffer: 30 minutes. Quiet hours today (2026-09-20 EDT): [20:09, 06:56]. NEVER hardcoded hours — sun moves ±15 min/season. EDT = UTC-4, fixed (no DST auto-switch).
- **Logging**: `logging.getLogger(__name__)`. Never `print()` for diagnostics. Canonical log: `logs/daemon.log` (RotatingFileHandler 50MB × 5 backups).
- **Tests: E2E-first (V2-080, 2026-09-24).** Default rule: E2E tests are the sole testing mechanism. Each E2E test produces a verifiable, repeatable artifact (an `event.json` file, a Telegram message log, a frame-diff PNG). Never write unit tests after you write code. If you must test a system in isolation (JSON schema, env loader, path resolver, MIME sniffer), FIRST write down every way the system could fail in `tests/<file>.FAILURE_MODES.md`, THEN write the test. If you can't enumerate failure modes, you don't understand the system — go back to E2E. Cards MUST NOT carry unit-test ACs unless paired with a failure-modes document. Run with `pytest tests/` for all, `pytest tests/test_<module>.py` for one, `pytest tests/e2e/` for E2E only.
- **TDD antipatterns (operator 2026-09-24, V2-080 follow-on).** When implementing with test-driven development: tautological tests are harmful (a test that asserts on the implementation rather than the behavior proves nothing); change-detector tests are harmful (a test that asserts "this function is called" or "this attribute exists" without verifying behavior breaks every refactor without catching any bug); do not create regression tests for bug fixes without a genuine gap in behavior testing (a "regression test" added to a bug-fix card is only valuable if E2E doesn't already cover that behavior — if it does, the test is duplicate signal). Default: write the E2E first, watch it fail for the right reason, fix the code, watch it pass, ship.
- **Lint**: `uv run ruff check .`. Zero errors before commit.
- **Git**: never pull from remote; push only with explicit operator permission.
- **Single commit per story.** Commit message starts with `US-NNx:` (e.g., `US-061b:`). One story, one commit.
- **`US-NNx` is a one-time ID per story, not a re-openable tag.** If reopening retired scope, use the next free letter and tag the title `(remediation)`. Run `hermes kanban list --json | jq -r '.[] | .id + " " + .title' | grep US-NNN` before filing.
- **No camera web-UI changes.** Out of scope for current arc.
- **No deletions to refactor/v1/internal/prototype projects.** Operator 2026-09-20 standing rule. Archive first via `archive-first-workflow` skill if surface is being retired.

## Step 4 — listener + plist contract (operator 2026-09-20, 5th reiteration)

**The launchd plist `com.farm.surveillance.v2.plist` has ZERO `EnvironmentVariables`.** This rule has been corrected ~5 times. The plist is for `ProgramArguments`, `WorkingDirectory`, `KeepAlive`, log paths, and resource limits ONLY. All env knobs go in:

- `~/.env` — operational config (LISTEN_HOST, LISTEN_PORT, PATH, VISION_LLM_URL)
- `camera-creds.env`, `telegram-creds.env`, `llm-creds.env` — per-app credentials (mode 600)

Loaded at boot via `listener/daemon.py:bootstrap()` → `_load_home_env()` → `dotenv_values()`.

**Listener lifecycle:**

```bash
# Check if running
launchctl list | grep farm   # non-empty = running, empty = stopped

# Load (start, KeepAlive so it stays running)
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.farm.surveillance.v2.plist

# Unload (stop, KeepAlive does NOT auto-restart after this)
launchctl bootout gui/$UID/com.farm.surveillance.v2.plist
```

**Current status (2026-09-20):** STOPPED. Will not restart until:
- US-061c (cleanup daemon thread) wired — DONE on main (`8f717ad`)
- US-061d (launchd plist StartInterval=3600 backup) shipped — IN PROGRESS (`t_2972a070`)
- `_extract_ip_from_rtsp` patched to handle malformed URLs (currently crashes on `'rtsp%3A%2F%2F'` without literal `://`)

**Per-app credential files** at project root: `camera-creds.env`, `telegram-creds.env`, `llm-creds.env` (mode 600). `infra/paths.py` declares `CAMERA_CREDS_FILE`, `TELEGRAM_CREDS_FILE`, `LLM_CREDS_FILE` constants. `~/.env` is the fallback only.

## Step 5 — kanban dispatch rules (v2 coder profile)

- **`max_in_progress_per_profile: 1`.** Hard rule from `kanban-orchestrator` skill (`dispatcher-mechanics.md` Gate 4). One coder + one QA + one reviewer at most. Never suggest "dispatch more workers" or `--max 2` for the coder profile. Status updates mention only what's running.
- **`hermes kanban create --body` is a literal string.** No `--body-file`. Use shell substitution: `BODY=$(cat /tmp/x.md); hermes kanban create --body "$BODY"`. `--body @/path` stores the literal string `@/path`, never reads the file.
- **Card body ≤2,500 chars (soft warning), operational ceiling ~4,500.** V2-061 cards at 4,677 bytes completed successfully; 5,000+ chars risk mid-flight context compression that drops ACs.
- **`--workspace worktree`.** Workers dispatch into a fresh worktree per card. Branch convention: `code/v2-NNNx-<slug>` (e.g. `code/v2-062b-time-of-day`).
- **Body + skills immutable post-create.** SQLite fix only: `UPDATE tasks SET skills='[]', status='ready' WHERE id='...'`. Don't try to amend via CLI — it doesn't expose those fields.
- **Bare `--skill X` with unknown skill exits the worker <60s.** Before `--skill devops/<name>`, verify it exists: `ls ~/.hermes/profiles/<assignee>/skills/` (specialist profiles have their own skills tree; a skill in `~/.hermes/skills/` is invisible to a profile worker unless symlinked into `~/.hermes/profiles/<assignee>/skills/`).
- Coder profile auto-loads `farm-surveillance-v2` + `kanban-worker`. Don't re-add them to `--skills`.
- **Handoff verb is `kanban request-review`.** Pipeline: `coder -> qa -> reviewer -> complete`. Workers never call `kanban complete`. On blocker: `kanban block <id> --reason "..."`.

## Step 6 — Reolink / RTSP technical constants

- **Reolink decode throughput:** 2 fps decode is the practical ceiling — don't promise higher.
- **Ring buffer size = 180 frames = 90 seconds at 2 fps.** Per-camera `data/frames/<camera>/<alert_id>/` holds the alert's full pre/post-event window.
- **`offset_seconds = 6` is dead code.** Cut from the active path; do not reintroduce.
- **TCP retransmit on RTSP** — readers should retry on transient `ECONNRESET` / `EPIPE`, not bail. Pattern: in-thread 3600s reconnect cadence inside `PersistentRTSPReader.run()`, NOT a watchdog thread (V2-049 banned outer supervisors for in-thread resources).

## Step 7 — verify state before claiming "still working"

```bash
# Listener alive?
launchctl list | grep farm

# Cleanup thread wired?
grep -nE 'infra\.cleanup\.run' listener/daemon.py | head -3

# Plist zero env vars?
grep -c "EnvironmentVariables" ~/Library/LaunchAgents/com.farm.surveillance.v2.plist
# Expected: 0

# Worktree clean?
git status --porcelain | wc -l
# Expected: 0 (or only intentional untracked like AGENTS.md)

# Worker preflight MUST pass before commit
cd /Users/jill/farm-surveillance-v2 && bin/preflight_v2_only.sh
```

If any check fails, STOP and diagnose. Do not assume past success = current success.

## Anti-patterns

- **Don't hardcode constants in code.** All knobs must be env-tunable.
- **Don't write raw JSON to a known-store file** (`data/known_vehicles/`, `data/known_animals/`, `data/known_people/`). Use the canonical store class.
- **No backup cleanup plist. No second cleanup surface.** US-061c daemon thread (commit `8f717ad`) is the only cleanup path. Operator 2026-09-20: *'Why is there a backup plist? Isn't that just making things more complicated? I don't need more complicated backup things.'* Listener runs under `KeepAlive=true`, so if the daemon thread dies, launchd restarts the listener and the thread is respawned within ~10s. A second launchd plist would race the daemon thread, double-log, and add a surface to monitor for no net benefit. US-061d (launchd plist `com.farm.surveillance.v2-cleanup.plist`) was created 2026-09-20 11:34 (`a1c3c2f`) and reverted 2026-09-20 12:22 (`2de7540`) for this reason. Do not reintroduce.
- **Listener bytecode ≠ source-of-truth.** Running listener holds bytecode loaded at boot; it does NOT auto-reload when files on disk change. After any change to `listener/daemon.py`, `listener/pipeline.py`, or anything imported by them at boot, the running process is stale. Verification via diff or test on disk is NOT verification of runtime behavior. To verify runtime: `launchctl kickstart -k gui/$UID/com.farm.surveillance.v2` (preserves bootstrap entry, ~10s downtime), then check `tail -5 logs/daemon.log` for the new code path. Operator 2026-09-20 18:50 EDT standing directive: defer restart until in-flight kanban cards are done (don't disrupt QA/running work); restart before declaring the PRD live. Precedent: V2-062 gates landed 12:48–15:02 EDT but listener started 12:04 EDT — alerts at 18:44 EDT (`13f9c8d5…`) fired through old bytecode with no `in_quiet_hours()` or baseline-override gates active.
- **Before proposing a new "X" knob, grep for existing knobs that already do X.** Operator 2026-09-20 18:55 EDT: when operator asked for "turn off notifications at night," agent proposed three options without first grepping source for existing night-suppress mechanisms. There were already TWO: (1) `MOTION_GATE_NIGHT_SUPPRESS_ENABLED` (motion-gate layer, default `0`, in `infra/quick_classifier.py:81`) and (2) `FARMSURV_QUIET_HOURS_ENABLED` (TG-dispatch layer, default `1`, in `infra/quiet_hours.py:86`). The two gates stack at different layers (motion classifier vs Telegram dispatch), not duplicate. Rule: when the operator says "I want X," the FIRST action is `grep -rn '<X-keyword>' . --include=*.py --exclude-dir=__pycache__` to see if X already exists. Only after that, propose creating new machinery.
- **Don't lump a code change + a doc change into one commit — except for module headers.** Headers and code land together. For non-header docs (AGENTS.md, PRDs), use **a separate commit per doc file**.
- **Don't suggest "dispatch more workers" or `--max 2`.** Hard rule: `max_in_progress_per_profile: 1`.
- **Don't write `kill_switch` / `kill_switch_override` paths.** (Operator 2026-09-19 "no fallback paths.") When the kill switch is engaged, the cleanup MUST no-op; there's no override.
- **Don't delete refactor/v1/internal/prototype projects.** All four `farm-surveillance-*` directories are preserved.
- **Don't load the listener before US-061c+d are shipped.** Disk-fill protection only works once the cleanup path is wired.
- **Don't fabricate data.** URLs, ratings, prices, file contents — must come from real sources. "Not yet researched" > fabricated.
- Don't update AGENTS.md or PRD docs without operator approval. **AGENTS.md updates are orchestrator (Jill) work, NEVER coder work.** If a card's AC list says "update AGENTS.md" / "update the runtime snapshot" / "update PRD docs" — that AC is wrong; remove it before dispatch. The orchestrator makes the doc edit directly when the corresponding code commit lands, or files a separate non-coder follow-up. Coder workers attempting AGENTS.md writes will hit the protected-instruction-file approval prompt, which times out after 300s and ends the worker without the doc-update slice — this is the root cause of every "blocked after review, AGENTS.md not updated" event in this session.

## Quick commands

```bash
# Worker preflight (asserts cwd = v2, NOT refactor/v1)
cd /Users/jill/farm-surveillance-v2 && bin/preflight_v2_only.sh

# Run all tests
cd /Users/jill/farm-surveillance-v2 && .venv/bin/python -m pytest tests/ -q

# Run tests for ONE module
cd /Users/jill/farm-surveillance-v2 && .venv/bin/python -m pytest tests/test_camera_creds.py -v

# Lint
cd /Users/jill/farm-surveillance-v2 && uv run ruff check .

# Check for cross-repo coupling (must be zero hits into refactor/v1/internal)
grep -rn "/Users/jill/farm-surveillance-refactor\|/Users/jill/farm-surveillance-internal" /Users/jill/farm-surveillance-v2/ \
    --include="*.py" --exclude-dir=__pycache__ --exclude-dir=.git

# View PRD for current arc
jq . /Users/jill/farm-surveillance-v2/docs/PHASE-V2-NNN-PRD-*.json | head -50

# Quiet hours today (suntime + 30-min buffer)
.venv/bin/python3.11 -c "
from suntime import Sun
from datetime import datetime, timedelta, timezone
sun = Sun(34.5782, -84.9438)
now = datetime.now(timezone(timedelta(hours=-4)))
sr = sun.get_local_sunrise_time(now).replace(tzinfo=timezone(timedelta(hours=-4)))
ss = sun.get_local_sunset_time(now).replace(tzinfo=timezone(timedelta(hours=-4)))
print(f'sunrise {sr.time()}, sunset {ss.time()}')
print(f'quiet hours [{(ss + timedelta(minutes=30)).time()}, {(sr - timedelta(minutes=30)).time()}]')
"

# Telegram origin-tag helper
python3 -c "from infra._telegram_origin import origin_prefix; print(origin_prefix('HOME'))"
```

## Runtime (snapshot 2026-09-24, refreshed 21:05 EDT)

- venv: `~/farm-surveillance-v2/.venv/` (Python 3.11)
- Listener: **RUNNING** as of 2026-09-24 20:55 EDT (PID **30681**, `com.farm.surveillance.v2`). Loaded via Python `subprocess.run` from inside Hermes. Port 8090 listening on 0.0.0.0, V2-071 algorithm active, US-073h `HealthReporter` (10-min cadence) emitting `listener_health` + `camera_health` to daemon.log; US-073g dispatcher escape fix (`<code>` tags respected) live; US-069d camera_id wiring threaded through gate.
- Active board: `coder-farm-surveillance` (kanban). V2-068 cleanup in flight: t_d6a1f47b (b7 orphan merge) running, t_82b23478 + t_52c8af04 ready.
- **Local Qwen context (V2-bump 2026-09-24 ~18:25 EDT):** llama-server `-c 196608` (was 131072), `-np 2` retained for agent-driven compression; `model.context_length: 196608` in `~/.hermes/config.yaml`. Memory math: 18 GB KV pool (+6 GB delta), RSS 25.7 GB, fits cleanly in 64 GB. Listener was restarted post-bump to capture all listener-source bytecode changes.
- **Per-camera night suppress (V2-069, 2026-09-23):** `FARMSURV_NIGHT_SUPPRESS_CAMERAS=OUTSIDE_BACK_SOLAR` and `FARMSURV_NIGHT_SUPPRESS_CLASSES=vehicle` active. Suppresses motion-gate alerts at night on Outside Back Solar for vehicle class only. Knobs declared in `.env.example` (US-069c, commit `39f1efa`). Implementation lives in `infra/quick_classifier.py:_night_suppress_match` (US-069a, commit `c32f238`) + `infra/quiet_hours.py:in_quiet_hours(class_name=...)` defense-in-depth (US-069b, commit `cadba81`). Per-camera wiring closed (US-069d, commit `b672331`).
- **Telegram caption format (US-067e, 2026-09-23):** `infra/farm_api/telegram.py` / `alert.py` emit Unicode em-dash (`—`) between attributes, confidence as a categorical word (`high`/`medium`/`low`), and `detail.py` "Attributes" header is plain text. Commit `c42fd60`.
- **Forgejo recovery mirror (US-068e, 2026-09-24):** `.git/hooks/post-merge` wires `scripts/push_to_forgejo.sh` to fire on every merge-to-main. Drift resolved at 21:01 EDT (local `8caee64` = forgejo `8caee64`). Commit `e67ad47`.
- Disk: ~31 GiB free / 460 GiB.
- Last commits on main:
  - `8caee64` kanban: t_e637e067 US-073h: listener periodic health heartbeat
  - `d465dde` Re-add tests/test_health_reporter.py deleted by US-080b
  - `c80d506` US-073g: fix dispatcher escape_markdown_v2 to respect `<code>` tags
  - `6adb7a6` US-080c: Remove mocking from E2E tests + delete 10 remaining unit tests
  - `f3dfe92` US-080c: Replace daemon spawn unit tests with E2E versions
  - `68b328a` US-080f: E2E tests for alert route + 10 farm_api endpoints (71 tests)
  - `6509346` US-080b: delete unit tests per docs/V2-080-unit-test-audit.md
  - `5e84968` US-080a: audit all 102 test files
  - `d6a773f` US-073i: vision + cleanup + telegram health lines in heartbeat
  - `9476a49` AGENTS.md: TDD antipatterns

## Phase status (per PRD arcs)

| PRD | Scope | Status |
|---|---|---|
| V2-046 | YOLO night false-alarm suppression | SUPERSEDED by V2-062 |
| V2-059 | Person + body recognition (OSNet body+face) | PRD drafted (`docs/PHASE-V2-059-PRD-person-body-recognition.json`), unfiled, no cards dispatched |
| V2-060 | VM1 redesign (class + subclass + face_visible + count) | PARTIAL — US-060c/d/e done; US-060a/b blocked |
| **V2-061** | **Frame retention policy (20 GB cap, hourly cleanup)** | **DONE** — US-061a/b/c/e done; US-061d shipped then reverted per anti-pattern (no backup cleanup plist) |
| **V2-062** | **Night-time headlight FP suppression (suntime + quiet hours + baseline override)** | **DONE** — US-062a/b/c/d/e/f/g all done; baseline override wired; suntime + 30-min twilight buffer live |
| **V2-068** | **MCP server + farm_api resource layer (10 resources) + per-alert event.json** | **DONE modulo US-068b7-merge** — US-068a/b1-b6/b8/b9/c/d/e done; US-068b7 orphan merge to main in flight (t_d6a1f47b); US-068c-fix1 + fix2 dispatched to clean up `_json_response` dead ref and `__init__.py` telegram mismatch |
| **V2-069** | **Per-camera + per-class night suppress (motion gate + quiet hours defense-in-depth)** | **DONE** — US-069a + US-069b + US-069c + US-069d done. Per-camera wiring gap closed (`camera_id` threaded through gate→classifier). |

## Open work

- **V2-068b7-merge (in flight, t_d6a1f47b):** cherry-pick `fb9da4f` (farm_api telegram dispatch status resource) onto main. Orphan branch `farm-surveillance/t_2f199824-us-068b7` has 1 commit ahead of common ancestor; main is 67 ahead. Required because `infra/farm_api/__init__.py` line 106 imports from `infra.farm_api.telegram` but the module doesn't exist on main.
- **V2-068c-fix1 (t_82b23478, ready):** investigate and remove dead `_json_response` reference at `infra/mcp_server.py:360` flagged in US-068c remediation defect log. May be a no-op (initial grep empty).
- **V2-068c-fix2 (t_52c8af04, ready):** reconcile `infra/farm_api/__init__.py` docstring/import references to telegram module. Blocked by t_d6a1f47b (the module must exist on main first) or remove refs until it ships.
- **V2-059 cards — not yet filed.** PRD placeholder exists at `docs/PHASE-V2-059-PRD-person-body-recognition.json` (empty file). Needs actual PRD draft before cards can be filed.
- **V2-060 US-060a/b — blocked** (carry from earlier sessions; no recent movement).
- **`_extract_ip_from_rtsp` IndexError fix** — flagged 2026-09-20; listener is currently running without this fix; no observed crash in current event stream. Lower priority.

---

**File lineage:** modeled on `farm-surveillance-internal/AGENTS.md` (Sep 2, 24,429 bytes, the source-of-truth operating guide). v2 is structurally simpler (no module-purity splits, no parameterization rule, no public/private split) — only the conventions that v2 actually follows are included. Don't add "aspirational" sections; if v2 doesn't follow a rule, leave it out. Better to be honest than to set up a rule we'll break.
