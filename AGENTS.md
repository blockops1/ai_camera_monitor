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

## Runtime (snapshot 2026-10-04, refreshed 21:25 EDT)

- venv: `~/farm-surveillance-v2/.venv/` (Python 3.11)
- Listener: **RUNNING** as of 2026-10-04 18:50 EDT (PID **44470**, `com.farm.surveillance.v2`, `kickstart -k` rotated from 15465). All three processes (listener :8090, MCP :8091, API :8092) live in one PID. Listener bytecode includes: US-069 (`09d93e8`+`0835952`), US-074 HTML parse_mode (`ba2ff5f`+`1d7ac37`), US-074b per-InputMedia parse_mode (`c843a8b`), US-075 bold labels (`637f7fc`), US-076 API/MCP enablement (`722ee2b`+`0f09fc6`), US-079 heartbeat API/MCP lines (`b332500`), US-080 E2E test coverage, US-081 animal embedder live, US-082 E2E animal test, US-084 timestamp cosmetic, US-091 VM1 routing override (`eefe5f03`), US-059a/b/d/e person recognition wiring, US-092a/b/c/d (TG-on-fail + watchdog + scheduler + person gate), US-093a (vision_health reset), US-096a/b/c (frame-geometry auto-subscribe + 4K noise filter), US-097a/b (socket-leak fix in `PersistentRTSPReader.stop()` + demux normal-exit close), **US-098x (`fa32aeea`)** env-tunable RTSP thresholds, **US-099x (`t_9648b2bf` done 2026-10-04)** 43-char API/MCP tokens, **US-100x (`d93d2fb7`)** FRONT-cam socket-leak fix in `_reconnect_loop` swap path + `camera_health.reconnects` counter carry-over (operator-verified at runtime 2026-10-04 19:00 EDT: 4 reader-swap events in 2 min, FRONT socket count 1 not 36). US-073h `HealthReporter` (10-min cadence) emitting `listener_health` + `camera_health` + `api_health` + `mcp_health` to daemon.log.
- Active board: `coder-farm-surveillance` (kanban). 0 ready, 1 review (US-100x `t_e3b36e70` -- FRONT cam socket leak fix, **commit `d93d2fb7` on main, work verified at runtime, but card assignee=qa stuck in chronic `blocker_auth` respawn loop since 2026-10-04 09:56:19** -- see Open work below), 1 blocked (US-094b `t_cc46f691` -- motion-off firmware limitation), 0 running. 188 cards done. **V2-094 SHIPPED 2026-09-30** (`6fb4979d`, forgejo in sync): US-094a (BEFORE snapshot, `t_e72debae` done), US-094b (`t_cc46f691` blocked at Reolink firmware SetMotion rejection), US-094c (encode_set to H.265 + 2fps + native res, `t_13a41934` done -- 3840x2160 on RLC-833A, 2560x1920 on RLC-510A, all 6 cams), US-094d (RTSP URL fix + listener restart, `t_cdb6c624` done), US-094e (`t_83c4ef05` ready -- 5-day observation arc starts 2026-09-30 17:24 EDT, due 2026-10-05 17:24 EDT). Operator pre-deconfigured rule: "no custom code in v2; all changes flow through the open-source Reolink MCP at /Users/jill/reolink-cli/". **V2-099 done 2026-10-04** (43-char API/MCP tokens, listener restarted, API/MCP `bound=True` on Tailscale `100.64.0.10`). **V2-100 done 2026-10-04** (FRONT cam socket leak in `_reconnect_loop` swap path, runtime-verified: 36 -> 1 ESTABLISHED sockets).
- **Three-process deployment (2026-09-25, US-076):** listener :8090 + MCP :8091 + API :8092 all spawned by `listener/daemon.py` daemon-thread hooks (`_start_mcp_server` line 1014, `_start_api_server` line 1073, called before `app.run()` at line 1196). Gated by `FARMSURV_MCP_ENABLED=1` + `FARMSURV_API_ENABLED=1` in `~/.env`. Tokens >=32 chars, bind to Tailscale (100.64.0.10).
- **Local Qwen context (2026-09-24 ~18:25 EDT):** llama-server `-c 196608`, `-np 2`, `--kv-unified`. `model.context_length: 196608` in `~/.hermes/config.yaml`. RSS ~25.7 GB. PID 24330 on :8093. Vision endpoint at `:8080` healthy (HTTP 200 on `/v1/chat/completions`, ~1.8s response).
- **Per-camera night suppress (V2-069):** `FARMSURV_NIGHT_SUPPRESS_CAMERAS=OUTSIDE_BACK_SOLAR` + `FARMSURV_NIGHT_SUPPRESS_CLASSES=car,truck,bicycle,motorcycle,bus` active. Confirmed live in boot log.
- **Telegram caption format (US-074+074b+075):** HTML parse_mode (`<b>`+`<code>` allowed via `escape_html` allowlist). `sendMessage`/`sendPhoto` honor top-level `parse_mode`; `sendMediaGroup` requires parse_mode per InputMedia (US-074b fix, was top-level-silently-ignored bug). Bold-label rule: word-before-colon bold, word-after plain. Bold `<b>...</b>` for section labels, `<code>...</code>` for timestamps/IDs. Em-dash between attributes removed in US-075.
- **Animal recognition (V2-045, COMPLETE 2026-09-25):** US-045a/b/c/d/e/f all done. MegaDescriptor-L-384 wrapper at `infra/animal_embedder.py`. Animal branch wired into pipeline Stage 12+13. `scripts/enroll_animal.py` CLI. Tier-2 cosine + Tier-1 Jaccard matching at `animal_matcher/match.py`. **Finnegan enrolled** 2026-09-25 19:24 EDT — labeled "Finnegan" (species dog, breed goldendoodle, distinctive: black collar, curly coat, white patch). 2 individual crop embeddings + 1 averaged stored in `data/known/animals/embeddings.npz` under keys `finnegan__crop_a.png`, `finnegan__crop_b.png`, `finnegan__avg`.
- **Animal embedder live mode (US-081):** `ANIMAL_EMBEDDER_LIVE=1` set in `~/.env` (variable added to `.env.example` with comment). MegaDescriptor-L-384 weights cached at `~/Library/Caches/huggingface/hub/`. Live embeddings used for Finnegan enrollment.
- **Person recognition (V2-059, IN PROGRESS):** US-059a/b/d/e shipped (person_embedder.py via CLIP-ViT-B/32, person_matcher with CLIP cosine + Tier-1 Jaccard, person branch in pipeline Stage 12+13, TG#3 wiring). US-059c (enrollment CLI + initial enrollment) not yet dispatched. Jeremiah enrollment pending operator action.
- **Forgejo recovery mirror (US-068e):** `.git/hooks/post-merge` wired. **IN SYNC 2026-09-30 17:25 EDT** -- local `6fb4979d` = forgejo `6fb4979d`. Earlier 2026-09-29 20:57 EDT drift (local `85dfaf9` != forgejo `50e7afff`) resolved by `bash scripts/push_to_forgejo.sh` once `deruyter:3000` (and Tailscale 100.64.0.1) became reachable. Push-log exit=128 (75s TLS) on 2026-09-28 16:34 was a separate transient that recovered without intervention.
- **Worktree reap (2026-09-29):** 48 stale worktrees reaped (~9 GB -> 17 MB on `.worktrees/`). All work preserved via `archive/<branch>-<sha>` tags. 6 merged branches with no worktree deleted. 3 exceptions handled: US-067d/US-068b7 original branches tagged-archived (remediation on main); US-060b tagged-archived (officially superseded per US-087 triage).
- Disk: ~224 GiB free / 460 GiB (5% used).
- **Last commits on main:**
  - `d93d2fb7` US-100x: Fix FRONT cam socket leak in _reconnect_loop + camera_health counter
  - `099d0e59` chore(housekeeping): remove 3 stale .env.bak + 2 camera-creds.env.bak files
  - `95b06ffb` docs: refresh AGENTS.md (US-098x SHIPPED fa32aeea, OFP hardware-fault finding, listener PID 11305)
  - `fa32aeea` US-098x: Make RTSP reconnect less aggressive via env-tunable stale + min-uptime thresholds
  - `43a1bc90` US-097b: Acquire _container_lock on demux normal-exit close (approach a)
  - `83dbb4c7` US-097a: Fix PersistentRTSPReader.stop() socket leak

## Phase status (per PRD arcs)

| PRD | Scope | Status |
|---|---|---|
| V2-046 | YOLO night false-alarm suppression | SUPERSEDED by V2-062 |
| V2-059 | Person + body recognition (CLIP-ViT-B/32 + Tier-1 Jaccard) | PARTIAL — US-059a/b/d/e done; US-059c (enrollment CLI + initial enrollment) not yet dispatched |
| V2-060 | VM1 redesign (class + subclass + face_visible + count) | DONE -- all 5 cards done; US-060b officially superseded per US-087 triage, schema-shape guard unnecessary under strict JSON response_format |
| V2-061 | Frame retention policy (20 GB cap, hourly cleanup) | DONE — US-061a/b/c/e done; US-061d shipped then reverted per anti-pattern (no backup cleanup plist) |
| V2-062 | Night-time headlight FP suppression (suntime + quiet hours + baseline override) | DONE — US-062a/b/c/d/e/f/g all done |
| V2-068 | MCP server + farm_api resource layer (10 resources) + per-alert event.json | DONE — US-068a/b1-b9/c/d/e all done; orphan-merge + fix1/fix2 landed via US-068b7 + US-068c-fix1/2 |
| V2-069 | Per-camera + per-class night suppress | DONE — US-069a/b/c/d done; import-order bug fixed at `09d93e8`; fail-loud at `0835952` |
| V2-080 | Unit-test audit + E2E-first convention | DONE — US-080a audit + US-080b delete classified DELETE + US-080c REPLACE-WITH-E2E + US-080e/f E2E tests for farm_api + alert_route (71 tests) |
| **V2-091** | VM1 owns routing decision (gate drops, VM1 classifies) | DONE -- `eefe5f03` on main; 233 runtime override events since 14:03 EDT 2026-09-29 (gate=md -> vm1=vehicle/person) |
| **V2-094** | Reolink AI-only alerting + H.265 main stream + native 4K resolution (via open-source MCP) | SHIPPED 2026-09-30 (`6fb4979d`) -- US-094a/c/d done, US-094b blocked at firmware, US-094e ready (5-day observation arc starts 2026-09-30 17:24 EDT, due 2026-10-05 17:24 EDT). No custom code in v2; all changes via `/Users/jill/.local/bin/reolink-cli` mcp-server. Listener restarted at PID 14330 (~14h uptime, all 6 cams healthy at native 4K/5MP H.265 2fps). RTSP URL paths fixed from `h264Preview_01_main` to `Preview_01_main` for OUTSIDE cams; FRONT/BACK got path appended. `.env` placeholders `__float__` for MOTION_GATE_NIGHT_CONF_FLOOR + MOTION_GATE_NIGHT_BRIGHTNESS_RATIO fixed to module defaults (pre-existing bug masked until listener restart). |
| **V2-092** | Heartbeat TG-on-fail + watchdog restart ladder + in-listener scheduling | SHIPPED -- US-092a (`fabe1f88`), US-092b (`653ce6db`), US-092c (`50e7afff`), US-092d (`bfdec91c` -- added during compaction pause: gate person detection to FRONT/BACK only). Listener bytecode reloaded at 2026-09-30 17:24 EDT via `launchctl kickstart -k` (PID 14330). |
| **V2-093** | vision_health last_call_age_s reset on successful /v1/chat/completions | SHIPPED -- US-093a (`d46c34e4`). Listener bytecode reloaded 2026-09-30 17:24 EDT. `vision_health: up=False` still observed at boot -- pre-existing issue, may need separate diagnostic if it persists >24h after reload. |
| **V2-045** | **Animal body recognition (MegaDescriptor-L-384)** | **DONE** — US-045a-f all done; Finnegan enrolled; live embeddings active |

## Open work

- **OFP (192.168.1.108, `OUTSIDE_FRONT_POWER`) — hardware-fault cam, physical swap required (2026-10-03).** OFP is a RLC-510A on the same firmware (v3.2.0.5180) and the same 5MP H.265 encoder config as the 3 working OUTSIDE cams (OFS/OFG/OBS), with the shortest PoE cable run (3m) and the lowest network latency (0.6ms RTT, best of the 4), yet it is the only one that flaps: TCP554 stays open, RTSP subscription succeeds, frames flow for ~12s, then the encoder stops pushing for 5+ s, listener marks unhealthy and reconnects. Ruled out: H.264 vs H.265, 5MP vs 4MP resolution, power cycle, full system reboot, encoder config (same as 3 working cams), PoE cable (shortest = best), TCP/RTSP handshake (works for ~12s), SD card (none), reset. **The 12s window matches Reolink firmware's internal RTSP session-idle timeout, with no cam-side knob exposed via v20 Baichuan.** Mitigated at the listener level by US-098x (`fa32aeea`) — env-tunable `FARMSV_RTSP_STALE_SECONDS=20.0` + `FARMSV_RTSP_RECONNECT_MIN_UPTIME=30.0` ride through the 12s idle window without firing the reconnect path. Post-US-098x: OFP 0 reconnects/5min, 1 ESTABLISHED socket. **Do not retry resolution/encoder/codec changes on this cam — it is a hardware fault (suspect: SoC degradation, power regulator, or network PHY).** When the cam is physically replaced, register the new cam's alias under `OUTSIDE_FRONT_POWER` and re-validate the 5-min reconnect rate — if still 0/5min with the replacement, the diagnosis is closed.
- **V2-059 US-059c not yet filed** (enrollment CLI for persons + initial Jeremiah enrollment). Mirrors US-045d animal enrollment. Required before person matcher fires live.
- **`vision_health` heartbeat counter does not reset on success** (operator-visible `up=False last_call_age_s=N` while vision route is healthy since 14:05 EDT 2026-09-29). Misleading; the route is fine. Worth a US-XXX fix to reset counter on successful `/v1/chat/completions` response. **Note:** US-093a shipped (`d46c34e4`) for this; reloaded 2026-09-30 17:24 EDT; `up=False` still observed at boot -- check >24h after restart before re-filing.
- **V2-094b blocked at firmware**: Reolink SetMotion disable command ignored on both RLC-833A (v3.1.0.4972) and RLC-510A (v3.2.0.5180). 5 input shapes tried via MCP + CLI, all rejected or silently no-op. Pragmatic alternative: rely on AI gate layer to suppress non-AI motion (current state). Follow-up US-094b2 may investigate SetMdAlarm protocol variant or scene-mode prep. Filed as blocked card `t_cc46f691`.
- **V2-094e observation arc in progress**: 5-day AI-only alerting baseline vs observation comparison. Starts 2026-09-30 17:24 EDT, due 2026-10-05 17:24 EDT. Operator decision recorded at end. Card `t_83c4ef05` (ready, no worker needed).
- **Launchd-vs-shell reolink-gateway networking bug**: launchd-managed gateway (PID via `launchctl bootstrap gui/$UID com.farm.reolink.gateway.plist`) cannot reach 192.168.1.X cams on port 554 (os error 65 "No route to host"), while a shell-managed gateway (same binary, same UID) CAN. WORKAROUND: shell-detached gateway PID 12130 via `subprocess.Popen(start_new_session=True)` since 2026-09-30 17:15 EDT. Real bug at macOS launchd session bootstrap level -- investigation deferred. Camera network reachability still works through the shell-detached gateway.
- **`_extract_ip_from_rtsp` IndexError fix** -- US-088 shipped (`7ab56507`); confirmed live. No longer open.
- **Lint rot (US-085, 2026-09-29):** clean lint pass landed; status post-refresh TBD. (Was: 73 errors pre-existing; 42 fixable with `--fix`. None in production code paths.)
- **Chronic `blocker_auth` respawn loop in kanban dispatcher (2026-09-24 to present, 1,840+ warnings).** Defined in `~/.hermes/hermes-agent/hermes_cli/kanban_db_dispatch.py:1516` as the `_RESPAWN_BLOCKER_RE` regex: if a worker's `last_failure_error` matches an auth/quota error, the dispatcher refuses to respawn the same worker for that card and emits `respawn_guarded reason=blocker_auth` every ~60s. Symptom: card stuck in `review` with `assignee=qa` (or other profile), unable to be claimed. **Not card-specific** -- the dispatcher is in a chronic failure state affecting all `coder-farm-surveillance` cards. Recovery options: (1) clear `last_failure_error` on the stuck card in `task_events` so the regex no longer matches and the next respawn gets through, (2) investigate the upstream auth/quota failure (worker session lost credentials, profile venv issue, hermes-agent version mismatch), (3) force a manual `kanban complete` on a verified card after the operator signs off. **First observed 2026-09-24; no root cause yet identified.** Currently affects 1 card: US-100x `t_e3b36e70` (work shipped `d93d2fb7`, runtime-verified, but card cannot reach `done`).
- **US-100x card `t_e3b36e70` is functionally done but stuck at `review`.** Commit `d93d2fb7` on main, 38 lines added to `infra/frame_capture.py` (force-close under `_container_lock` in `_reconnect_loop` swap path + `camera_health.reconnects` counter carry-over + WARNING log on swap), 273 lines in `tests/test_frame_capture.py::TestReconnectLoopSocketLeak` (3 unit tests passing). Operator restart completed 2026-10-04 18:50 EDT (PID 15465 -> 44470). Runtime verification 2026-10-04 19:00 EDT: 4 reader-swap events in 2 min, FRONT socket count 1 (was 36), all 6 cams at 1 ESTABLISHED socket. **The card is stuck on the chronic `blocker_auth` issue above; the work itself is real and verified.**

---

**File lineage:** modeled on `farm-surveillance-internal/AGENTS.md` (Sep 2, 24,429 bytes, the source-of-truth operating guide). v2 is structurally simpler (no module-purity splits, no parameterization rule, no public/private split) — only the conventions that v2 actually follows are included. Don't add "aspirational" sections; if v2 doesn't follow a rule, leave it out. Better to be honest than to set up a rule we'll break.
