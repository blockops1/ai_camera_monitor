# v2 Farm Surveillance — System Recreation Guide

**Project:** farm-surveillance-v2
**Purpose:** End-to-end webhook pipeline: Reolink motion → YOLO gate → VM1 (classify) → TG#1 → VM2 (detail) → TG#2 → vehicle match → TG#3
**Author:** Mr. V (Rolf Versluis) | Operator: Jill (Hermes agent)
**Status:** active (PRD: PHASE-V2-CORE-PRD-pipeline-buildout)
**Last updated:** 2026-09-06

This document is the **single source of truth for rebuilding v2 from scratch**. If everything else is lost, this file plus the PRD plus the code in `~/farm-surveillance-v2/` is enough to recreate the system. It captures *what*, *where*, *why*, and *how it runs* — not implementation details.

---

## 1. What v2 is

A from-nothing build (started 2026-09-06) of a clean, lean surveillance alert pipeline. Replaces a 14k-line refactor (frozen at `~/farm-surveillance-refactor/`) that grew too complex to maintain.

**Goals (verbatim from PRD `goal` field):**
> Land all 11 stages of v2 pipeline so an end-to-end webhook on CAM1 (or any test camera) routes through cooldown → gate → VM1 → TG#1 → VM2 → TG#2 → match → TG#3 and reaches Telegram with no operator intervention.

**Non-goals:**
- Do NOT touch `~/farm-surveillance-refactor/` or `~/farm-surveillance-internal/`. They are frozen archives.
- Do NOT touch `/tmp/`. Build lives at `~/farm-surveillance-v2/`.
- Do NOT modify the 4 vision prompts (`vm1_prompt.py`, `vehicle_prompt.py`, `person_prompt.py`, `animal_prompt.py`) or the 8 lifted infra modules.
- Do NOT introduce `threat` or `threat_level` anywhere in the pipeline.
- Do NOT create per-class pipeline submodules (`vehicle_pipeline/`, etc.). One `listener/pipeline.py` is the spine.

---

## 2. Repository layout

```
~/farm-surveillance-v2/                # v2 build root (PUBLIC FORGEJO: deruyter:3000/jill/farm-surveillance-v2)
├── .git/                              # main branch, local + remote mirror
├── .gitignore                         # excludes .env, __pycache__, *.pyc, .venv/, data/frames/
├── .env.example                       # copy → .env, fill in Telegram + LLM credentials
├── README.md                          # public-facing overview
├── docs/
│   ├── PHASE-V2-CORE-PRD-pipeline-buildout.json   # MASTER PRD (status: active, 18 stories)
│   ├── PLAN.md                        # v2 11-stage linear pipeline spec (frozen 2026-09-06)
│   ├── V2-LIFT-PLAN.md                # what was lifted byte-identical from refactor
│   └── RECREATION.md                  # THIS FILE
├── infra/                             # 8 LIFTED modules + 4 fresh vision prompts + new code
│   ├── pipeline_cooldown.py           # US-001 — PipelineCooldown(keys on (camera_id, classification))
│   ├── vm1_prompt.py                  # LIFTED — VM1 (classify) prompt + schema
│   ├── vehicle_prompt.py              # LIFTED — VM2 vehicle detail prompt
│   ├── person_prompt.py               # LIFTED — VM2 person detail prompt
│   ├── animal_prompt.py               # LIFTED — VM2 animal detail prompt
│   ├── vision_analyzer.py             # US-002 + US-003 — verify_class + detail_class
│   ├── frame_diff.py                  # LIFTED
│   ├── gate.py                        # LIFTED — YOLO gate
│   ├── gate_cooldown.py               # LIFTED
│   ├── motion_types.py                # LIFTED
│   ├── classify_schema.py             # LIFTED
│   ├── quick_classifier.py            # LIFTED
│   ├── vision_cache.py                # LIFTED
│   └── llm_config.py                  # US-008 (PRD §11.176) — strip LLM endpoint defaults
├── telegram_formatter/
│   ├── alert.py                       # US-004 — TG#1 message after VM1
│   ├── detail.py                      # US-005 — TG#2 message after VM2
│   └── match_alert.py                 # US-007 — TG#3 message after match
├── vehicle_matcher/
│   └── match.py                       # US-006 — pure vehicle matcher
├── vehicle_position/
│   ├── __init__.py
│   ├── motion_detector.py             # LIFTED
│   ├── motion_detector_impl.py        # LIFTED
│   └── crop_extractor.py              # LIFTED
├── listener/
│   ├── __init__.py                    # US-008a — empty (or just creates)
│   ├── pipeline.py                    # US-008a/b/c — the spine, single run() function
│   └── listener.py                    # US-009 — handle_webhook() entrypoint
└── tests/
    ├── __init__.py
    ├── test_pipeline_cooldown.py      # US-001 — 3 tests
    ├── test_vision_analyzer.py        # US-012 — 4 tests
    └── test_pipeline_synthetic.py     # US-013a/b — fixtures + 1 E2E test
```

---

## 3. Pipeline architecture (11 stages, single `run()` function)

`listener/pipeline.py::run(alert: dict) -> dict` is the **only** orchestration function. NO per-class submodules. NO ThreatLevel. Linear flow:

| Stage | What | Module |
|-------|------|--------|
| 1 | Extract `camera_id`, `classification` from webhook payload | `listener/pipeline.py` (inline) |
| 2 | Cooldown check: `PipelineCooldown.should_suppress(camera_id, classification)` → suppress or proceed | `infra/pipeline_cooldown.py` (US-001) |
| 3 | Load 4 frames from `vehicle_position/crop_extractor.py` | `vehicle_position/` |
| 4 | Run YOLO gate on frames: drop if `no_surveillance` | `infra/gate.py` |
| 5 | (skip if gate dropped) | — |
| 6 | `record_hit(camera_id, classification)` on cooldown | `infra/pipeline_cooldown.py` |
| 7 | `verify_class(frames)` → VM1 call with `vm1_prompt.py` → TG#1 via `telegram_formatter/alert.py` | `infra/vision_analyzer.py` (US-002) |
| 8 | `detail_class(frames, mode)` → VM2 call with mode-specific prompt → TG#2 | `infra/vision_analyzer.py` (US-003), `telegram_formatter/detail.py` (US-005) |
| 9 | TG#2 send (vehicle/person/animal) | `telegram_formatter/detail.py` |
| 10 | `vehicle_matcher.match(vehicle_features)` → TG#3 | `vehicle_matcher/match.py` (US-006) |
| 11 | TG#3 send | `telegram_formatter/match_alert.py` (US-007) |

**Mode dispatch (vehicle/person/animal) lives in `detail_class()`'s internal dict, NOT in `pipeline.py`.** The pipeline stays spine-thin.

---

## 4. Test strategy

- **Baseline:** 0 tests on `main` at v2 fork (2026-09-06).
- **Target end-state:** 8 tests pass.
  - US-001: 3 tests in `tests/test_pipeline_cooldown.py`
  - US-012: 4 tests in `tests/test_vision_analyzer.py`
  - US-013b: 1 E2E test in `tests/test_pipeline_synthetic.py`
- **Per-story verification:** `python3 -m pytest tests/ -x --tb=short` exits 0.
- **Net-negative impossible from baseline** — but tests MUST go DOWN on refactors (operator policy 2026-09-06).

---

## 5. Profiles (Hermes kanban specialist queue)

Three profiles, all using local `qwen-local` model on port 8093 (35B-A3B MTP llama-server):

```
~/.hermes/profiles/coder/SOUL.md      # implements code changes, hands off to qa via request-review --reviewer qa
~/.hermes/profiles/qa/SOUL.md         # lints/types/scores, hands off to reviewer via request-review --reviewer reviewer
~/.hermes/profiles/reviewer/SOUL.md   # judges, calls kanban complete when approved
```

**Single-specialist queue:** `kanban.max_in_progress: 1` and `kanban.max_in_progress_per_profile: 1` in `~/.hermes/config.yaml`. One worker at a time across all profiles.

**Why serial:** local llama-server has one model context. Concurrent workers deadlock on the socket or OOM. `agent.disabled_toolsets: [delegation]` is also set per profile to physically remove the spawn tool.

---

## 6. Story lifecycle (operator playbook)

1. **Create card:** `hermes kanban create <title> --body @body.md --assignee <profile> --initial-status blocked` — body ≤2500 chars, ACs as grep/python invocations.
2. **Operator unblocks:** `hermes kanban unblock <id> --reason "<why>"` — converts `blocked/needs_input` → `ready`.
3. **Subscribe:** `hermes kanban notify-subscribe <id> --platform telegram --chat-id 374999219 --chat-type dm --delivery-mode notify+wake` — Telegram pings on every transition.
4. **Dispatcher picks up:** cron tick ≤60s spawns `hermes -p <profile> work kanban task <id>`.
5. **Worker executes:** reads body, implements, runs ACs, calls `hermes kanban request-review <id> --reviewer <next> --summary "..."`.
6. **Reviewer approves:** calls `hermes kanban complete <id> --result "..." --summary "..."`.
7. **Operator reviews `git log main..HEAD --stat`** to confirm commits landed (card status `done` is NOT proof of work — worker can self-archive without committing).

---

## 7. Common ops recipes

### Block a runaway worker
```bash
ps -ef | grep "work kanban task" | grep -v grep   # find PID
kill <PID>                                        # graceful
hermes kanban reclaim <task_id>                   # release the claim
hermes kanban reassign <task_id> <profile> --reclaim --reason "..."
```

### Manually re-route a stuck card (Fix A pattern)
When a worker calls the wrong verb or hangs:
```bash
kill <worker_pid>
hermes kanban reclaim <task_id>
hermes kanban reassign <task_id> reviewer --reclaim --reason "manual force-route"
```

### Subscribe to all cards on a board
```bash
for tid in $(hermes kanban list --json | jq -r '.[].id'); do
  hermes kanban notify-subscribe "$tid" --platform telegram --chat-id 374999219 --chat-type dm --delivery-mode notify+wake
done
```

### Force `max_in_progress: 1` (anti-parallel-leak)
```bash
hermes config set kanban.max_in_progress 1 --force
hermes config set kanban.max_in_progress_per_profile 1 --force
```

### Verify pytest is installed in hermes-agent venv
```bash
/Users/jill/.hermes/hermes-agent/venv/bin/pip install pytest
```

---

## 8. Listener status (as of 2026-09-06)

**🔴 MAINTENANCE MODE.** Plist disabled:
`/Users/jill/Library/LaunchAgents/ai.farm.surveillance-listener-refactor.plist.disabled-20260906-1117stripdown`

Re-enable only after v2 ships and is verified end-to-end.

---

## 9. llama-servers (vision-8B + text-7B + 35B MTP)

All three live, reboot-safe via launchd:

| Server | Model | Port | PID (as of 2026-09-06) | Purpose |
|--------|-------|------|------------------------|---------|
| vision-8B | `qwen2-vl-8b` | 8080 | 76829 | US-001..US-013 + all vision calls |
| text-7B | `qwen2-7b` | 8081 | 20824 | text-only inference |
| 35B MTP | `qwen3.6-35b-a3b` | 8093 | 20664 | specialist profile backend (coder/qa/reviewer) |

Plist files at `~/Library/LaunchAgents/` (not documented here — recreate via `hermes model serve` if lost).

---

## 10. PRD and story map

Master PRD: `docs/PHASE-V2-CORE-PRD-pipeline-buildout.json` (status: `active`, 18 stories).

| Story | Title | File | Status | Kanban ID |
|-------|-------|------|--------|-----------|
| US-001 | infra/pipeline_cooldown.py — PipelineCooldown | `infra/pipeline_cooldown.py` | **done** | t_91cb4824 |
| US-002 | infra/vision_analyzer.py — verify_class() | `infra/vision_analyzer.py` | running | t_35b8a179 |
| US-003 | infra/vision_analyzer.py — detail_class() | `infra/vision_analyzer.py` | blocked | t_5f1f5945 |
| US-004 | telegram_formatter/alert.py — TG#1 | `telegram_formatter/alert.py` | blocked | t_a0ed6b09 |
| US-005 | telegram_formatter/detail.py — TG#2 | `telegram_formatter/detail.py` | blocked | t_9a16c51b |
| US-006 | vehicle_matcher/match.py — pure matcher | `vehicle_matcher/match.py` | blocked | t_3691f28b |
| US-007 | telegram_formatter/match_alert.py — TG#3 | `telegram_formatter/match_alert.py` | blocked | t_0cb8d6f5 |
| US-008 | (SUPERSEDED) | — | superseded | — |
| US-008a | listener/pipeline.py skeleton + stages 1-3 | `listener/pipeline.py` | blocked | t_a394fbb9 |
| US-008b | pipeline.py stages 4-7 (gate/verify_class/TG#1) | `listener/pipeline.py` | blocked | t_90288eab |
| US-008c | pipeline.py stages 8-11 (detail/TG#2/match/TG#3) | `listener/pipeline.py` | blocked | t_487691e2 |
| US-009 | listener/listener.py — handle_webhook() | `listener/listener.py` | blocked | t_1a9ab4b3 |
| US-010 | (TBD) | — | blocked | (orphaned) |
| US-011 | (TBD) | — | blocked | (orphaned) |
| US-012 | tests/test_vision_analyzer.py — 4 tests | `tests/test_vision_analyzer.py` | blocked | t_15176730 |
| US-013 | (SUPERSEDED) | — | superseded | — |
| US-013a | test_pipeline_synthetic.py fixtures | `tests/test_pipeline_synthetic.py` | blocked | t_ca16576a |
| US-013b | test_pipeline_synthetic.py 1 E2E test | `tests/test_pipeline_synthetic.py` | blocked | t_42859fe9 |

---

## 11. Critical constraints (operator-locked, do not relax)

1. **Tests go DOWN on refactors** — never add `TestXxxLegacy` markers, never comment out tests.
2. **NO per-class pipeline submodules** — single `listener/pipeline.py` is the spine.
3. **NO ThreatLevel / threat_level field anywhere** — 5 negative-grep ACs enforce.
4. **NO gate hint to VM1** — gate verdict must not leak into VM1 prompt context.
5. **Sequential execution** — `max_in_progress: 1` is a hard rule, not a default.
6. **Numerical story order** — US-002 before US-003, US-008a before US-008b, etc. No reordering by perceived dependency.
7. **Operator unblocks, not dispatcher** — every card needs explicit `kanban unblock`.
8. **US-008/US-013 split from 2026-09-06 is canonical** — do not re-merge the original monolithic stories.

---

## 12. Lessons learned (operator corrections that became rules)

- **2026-09-06**: "Whenever I tell you to refactor something into simpler code you keep all the legacy code around... the number of tests should go DOWN." → Pivot to from-nothing v2 build.
- **2026-09-06**: "I'm pretty sure native defaults to firecrawl if it's available. Anyway we can deal with that later." → Firecrawl config bug deferred, not a structural fix.
- **2026-09-06**: "Why aren't you running them in numerical order?" → Numerical = correct. PRD dependency fields are file-import gates, not execution order.
- **2026-09-06**: "Now review your skills because you've obviously forgotten what it is you need to be doing here." → Load `kanban-orchestrator` + `kanban-worker` BEFORE touching the board.
- **2026-09-06**: US-002 worker crash-loop (silent exit, no kanban verb). → Anti-silent-exit rule added to coder SOUL.md (top of file, compression-proof).
- **2026-09-06**: qa death-loop (calls `kanban_complete` instead of `request-review --reviewer reviewer`). → Anti-trigger rule added to qa SOUL.md.

---

## 13. Open issues (deferred, not blocking)

- **Firecrawl `web.extract_backend` config:** `~/.hermes/config.yaml` has `web.extract_backend: native` (invalid). Firecrawl is available. Fix: `hermes config set web.extract_backend firecrawl`. Operator deferred 2026-09-06 ("deal with that later").
- **2 orphan kanban tasks:** `t_924b7ae7`, `t_84ee421a` — noise from earlier sessions. Safe to archive.
- **US-010, US-011 stories:** orphaned in PRD, no kanban IDs. Need scoping before assignment.

---

## 14. Verification commands (run after any rebuild)

```bash
# 1. Repo state
cd ~/farm-surveillance-v2 && git log --oneline -5 && git status

# 2. PRD sanity
python3 -c "import json; p=json.load(open('docs/PHASE-V2-CORE-PRD-pipeline-buildout.json')); print(p['status'], len(p['stories']), 'stories')"

# 3. Kanban board state
hermes kanban list --board coder-farm-surveillance

# 4. Tests
python3 -m pytest tests/ -x --tb=short

# 5. Models up
curl -s http://127.0.0.1:8080/health | jq .
curl -s http://127.0.0.1:8093/health | jq .

# 6. Listener status (should be disabled)
ls ~/Library/LaunchAgents/ai.farm.surveillance-listener-refactor.plist.disabled-* 2>/dev/null && echo "listener: maintenance mode OK" || echo "listener: NOT disabled — wrong state"
```

---

**End of recreation guide. Update this file whenever the architecture, story map, or locked constraints change.**
