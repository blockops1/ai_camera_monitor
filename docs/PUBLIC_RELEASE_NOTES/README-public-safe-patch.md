# README public-safety patch

**Target file:** `README.md`
**Current commit:** `c498761 docs(readme): add alert examples + Reolink camera setup walkthrough`
**Land on:** next release (v0.6.2.1 or v0.6.3, per operator directive 2026-09-15)
**Author:** jill (operator-authored, assistant-drafted)
**Scope:** rewrite top-of-README (lead with WHY, not WHAT); strip internal dev-status leaks elsewhere; preserve operational config tables and Reolink setup walkthrough

---

## Why this patch exists

Operator verbatim 2026-09-15:

> "OK, good. Now I was also looking at the public GitHub, the read me, and I saw up top that we have internal development status in the Readme? Linear-pipeline webhook consumer for farm IP cameras. Receives Reolink motion alerts, runs an 11-stage processing pipeline, fires Telegram notifications on subject detection and match results. Status: v2 daemon live on port 8090; delivering real alerts. Current PRD (PHASE-V2-017) complete as of 2026-09-08..."

> "we just wrote the patch, will get it in there when we next update two version 6.2 or whatever is the next number."

> "you can't start the first paragraph with crap like that the first paragraph has to be an explanation of why the product exists and how it's different from other projects. After that, it's OK to go with the detail"

The README leads with a mechanical description ("linear-pipeline webhook consumer… 11-stage processing pipeline…") and a status block that names internal dev artifacts (PRD IDs, dates, feature names). Both must go. The lead paragraph must answer **why the project exists** and **what's different about it** — not list what it does.

---

## Patch

```diff
--- a/README.md
+++ b/README.md
@@ -1,12 +1,29 @@
 # farm-surveillance-v2

-Linear-pipeline webhook consumer for farm IP cameras. Receives
-Reolink motion alerts, runs an 11-stage processing pipeline, fires
-Telegram notifications on subject detection and match results.
-
-> **Status:** v2 daemon live on port 8090; delivering real alerts.
-> Current PRD ([PHASE-V2-017](docs/PHASE-V2-017-PRD-rtsp-robustness-and-delivery.json))
-> complete as of 2026-09-08 — registry prefix key, telegram env loading,
-> scheduled_reconnect_watchdog, max_reconnect_attempts cap.
+Most consumer IP-camera systems treat every motion alert as worth showing you. They
+flood Telegram with branches moving in the wind, headlights sweeping across a driveway,
+IR glare flickering off a fence. The result is the same as no notification at all: you
+silence the channel.
+
+**farm-surveillance-v2** is the opposite. It runs a small vision pipeline on each motion
+event — a YOLO first-pass gate to filter obvious noise, a vision-LLM (Qwen3-VL) to
+verify the subject and pull structured attributes (color, body, make/model, breed),
+and a motion-aware crop so only the **moving** subject ends up in the alert, not every
+vehicle visible in frame. Per-camera confidence thresholds let you tune a windy porch
+camera to demand 0.85 before an alert fires while a quiet driveway camera alerts at 0.50.
+
+The pipeline delivers up to three Telegram messages per event: a wide-frame alert with
+the motion box, a cropped close-up of the subject, and (if recognized) a structured
+match result. The crop is the part that earns its keep — it's the difference between
+"a dog crossed the driveway" and "a small fluffy dog crossed the driveway, here is
+just the dog."
+
+Built for Reolink IP cameras; runs as a single Python daemon under `launchd` on macOS.
+Phase PRDs in [`docs/`](docs/).

 ## What this is
```

### Editorial reasoning (per section)

| Change | Reason |
|---|---|
| **New lead (3 paragraphs)** | Answers WHY: noise-flood problem → how v2 solves it (YOLO gate + LLM verify + motion crop) → what's different (per-camera thresholds, crop discipline) |
| **`# farm-surveillance-v2`** | Title stays — that's the project name |
| **Drop "Linear-pipeline webhook consumer…"** | Mechanical description. Doesn't explain why. |
| **Drop "11-stage processing pipeline"** | Internal stage count — not a public API concept |
| **Drop "Status: v2 daemon live on port 8090; delivering real alerts"** | Internal deployment status. README isn't a status page. |
| **Drop "Current PRD (PHASE-V2-017) complete as of 2026-09-08 — registry prefix key, telegram env loading, scheduled_reconnect_watchdog, max_reconnect_attempts cap"** | Internal roadmap + internal feature names. Has no meaning to a public reader. |
| **Drop "Built for Reolink IP cameras; runs as a single Python daemon under `launchd` on macOS. Phase PRDs in [`docs/`](docs/)."** | Stays in new lead |

### What stays untouched

| Lines | Why kept |
|---|---|
| `## What this is` (line 12+) and the bulleted list | Genuinely useful overview; not status-y |
| `## What alerts look like` and the example image tables | **This is the differentiator** — public readers see what the alerts actually look like. The "small fluffy dog crossing the gravel" example is gold. |
| `## Reolink camera setup` (lines ~60-156) | Step-by-step setup walkthrough. Genuinely useful to public consumers. |
| The Diagnostics / Layout / Configuration / Daemon management / Development sections | Mostly operational — useful, not status-y. See "secondary edits" below for individual tightening. |

---

## Secondary edits (separate hunks, same patch)

These are smaller individual leaks scattered through the rest of the README. Each gets its own hunk.

### Hunk 2: Strip internal story IDs (lines 162-167)

```diff
@@ -162,7 +162,7 @@

 Each camera has its own `PersistentRTSPReader` (in
 [`infra/frame_capture.py`](infra/frame_capture.py)) with two defense-in-depth
-watchdogs (US-017d / US-017e):
+watchdogs:

 | Mechanism | Cadence | Source |
 |---|---|---|
 | `scheduled_reconnect_watchdog` | every `FARMSV_RTSP_RECONNECT_SECONDS` (default 3600s) | proactive close+respawn |
```

**Reason:** `US-017d` / `US-017e` are internal kanban story IDs. Public readers don't have those.

### Hunk 3: Strip v1 refactor path (line 170)

```diff
@@ -170 +169,0 @@
-Plus the existing `CameraCaptureRegistry._reconnect_loop` for
-cross-reader registry-level recoveries. Ported from v1 refactor `<V1_REPO_PATH>/infra/persistent_rtsp.py`.
```

**Reason:** `<V1_REPO_PATH>` is a placeholder string, not a real link. References internal monorepo path. Strip entirely.

### Hunk 4: Strip test count + plist mention (lines 174-177)

```diff
@@ -174,4 +174,3 @@

 ## Diagnostics

-- `GET /debug/rtsp` — per-reader ring size, decoded total, last-frame age,
-  container-open, health flag, error count
-- Unified log: `logs/daemon.log` (single stream, plist routes stdout here)
-- `pytest tests/ -x --tb=short` — 122 tests passing
+- `GET /debug/rtsp` — per-reader ring size, decoded total, last-frame age,
+  container-open, health flag, error count
+- Unified log: `logs/daemon.log` (single stream, daemon writes here)
```

**Reason:** Test counts rot ("122 tests" is already stale; we're at 391). Plist mention is implementation detail. Daemon writes to log either way.

### Hunk 5: Strip test count from Layout block (line 186)

```diff
@@ -186 +185,1 @@
-tests/                   # pytest (122 tests)
+tests/                   # pytest
```

**Reason:** Same as above.

### Hunk 6: Strip "~/.env has 26 other keys" + US-017b attribution (line 201)

```diff
@@ -201 +200,1 @@
-| `~/.env` (user home) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHAT_ID`, plus 26 other keys | `listener/daemon.py::_load_home_env` (added in US-017b) |
+| `~/.env` (user home) | Telegram credentials + runtime flags (see `.env.example`) | `listener/daemon.py::_load_home_env` |
```

**Reason:** "26 other keys" is implementation noise. US-017b is an internal story ID. Public readers just need "Telegram creds + runtime flags, see .env.example."

### Hunk 7: Strip PRD roadmap table (lines 253-262)

```diff
@@ -253,10 +252,0 @@

-## PRDs
-
-Each phase is a numbered PRD in `docs/PHASE-V2-*.json`. Current state:
-
-| PRD | Phase | Status |
-|---|---|---|
-| PHASE-V2-CORE | pipeline buildout | done |
-| PHASE-V2-014 | camera webhook activation | done |
-| PHASE-V2-016 | diagnostic visibility + boot hardening | done |
-| PHASE-V2-017 | rtsp robustness + telegram delivery | done |
-
 ## License
```

**Reason:** README is not a project tracker. Internal roadmap belongs in an internal-only doc, not a public README. The `docs/PHASE-V2-*.json` files exist as evidence either way; the README doesn't need to enumerate them.

### Hunk 8: Fix license line — currently contradicts the actual LICENSE file (line 266)

```diff
@@ -266 +255,1 @@
-Operator-private for now. License TBD before public squash release.
+This project is licensed under the MIT License — see [`LICENSE`](LICENSE).
```

**Reason:** The README currently says "Operator-private for now. License TBD before public squash release." but the repo **already has an MIT LICENSE file** (added in v0.6.2, blob `278dcfb`, 21 lines, "MIT License / Copyright (c) 2026 The ai-camera-monitor authors"). The README line is stale and contradicts the published license. Confirmed via `curl https://api.github.com/repos/blockops1/ai_camera_monitor/license` → `{"key":"mit","spdx_id":"MIT"}`. The README has been wrong since v0.6.2 shipped.

### Hunk 9: Move Telegram env-var rename tip to docs (lines 269-273)

```diff
@@ -269,5 +257,0 @@

-## Telegram home chat env var name
-The canonical env var name is `TELEGRAM_HOME_CHAT_ID`, **not** `TELEGRAM_CHAT_ID`. If your `~/.env` has the wrong name, the dispatcher will raise `ConfigError` with the exact rename step on every alert. Verify with:
-```bash
-python3 -c 'from dotenv import dotenv_values; from pathlib import Path; print(bool(dotenv_values(Path.home() / ".env").get("TELEGRAM_HOME_CHAT_ID")))'
-```
-If this prints `False`, rename `TELEGRAM_CHAT_ID` to `TELEGRAM_HOME_CHAT_ID` in `~/.env`.
```

**Reason:** Operator-direct troubleshooting tip, not a public-README feature. Move to `docs/PUBLIC_RELEASE_NOTES/TROUBLESHOOTING.md` (companion doc, also lands with the next release). README should describe features, not run installation debugging commands.

---

## Combined diff stat (estimated)

```
 README.md | 51 ++++++++-----------------
 1 file changed, 12 insertions(+), 39 deletions(-)
```

Net: trim ~27 lines. Lead grows from 10 lines to 28 lines but every new line earns its place (problem → differentiator → what alerts look like → how it runs).

---

## Operator pre-merge checklist

When landing this on the next release:

1. [ ] Apply all hunks (or merge this file as a single patch with `git apply`)
2. [ ] Verify `cat README.md | head -30` reads as a public-safe overview (problem → differentiator → what alerts look like)
3. [ ] Verify `grep -c 'US-0' README.md` returns 0 (no internal story IDs leak through)
4. [ ] Verify `grep -c 'PHASE-V2-' README.md` returns 0 (no internal PRD roadmap in README)
5. [ ] Verify `grep -c 'v1 refactor' README.md` returns 0
6. [ ] Verify `grep -c 'Operator-private' README.md` returns 0 (license line now points at LICENSE file)
7. [ ] Land together with `.mailmap` ride-along as v0.6.2.1 (or absorb into v0.6.3 if bigger changes are queued)
8. [ ] Move "Telegram home chat env var name" tip to `docs/PUBLIC_RELEASE_NOTES/TROUBLESHOOTING.md` as part of the same release (companion doc already drafted at `docs/PUBLIC_RELEASE_NOTES/TROUBLESHOOTING.md`)
9. [ ] Add LICENSE file to the release if not already present on github (LICENSE is at blob `278dcfb75ec0711aef0ed7b7caee92e15ca53b74`, MIT, 21 lines, "Copyright (c) 2026 The ai-camera-monitor authors")

---

## Related context

- Operator directive 2026-09-14: privacy waiver for v0.6.2 public release (image-as-is, no sanitization)
- v0.6.2 already shipped (`fb6d14d` on github/main) with the leaky README — this patch back-fills that release
- `.mailmap` ride-along pending at `0154f78` on local main, not yet pushed
- This patch is **NOT** applied to local `main` per operator directive — they want to land it together with the next version bump

## Files

- This file: `docs/PUBLIC_RELEASE_NOTES/README-public-safe-patch.md`
- Target: `README.md` (untouched in working tree)
- Companion doc (also lands with the next release): `docs/PUBLIC_RELEASE_NOTES/TROUBLESHOOTING.md`

## License clarification

Operator directive 2026-09-15:

> "We published this with MIT license before and that license will continue now. Why would you suggest changing it? Where are you getting your guidance from?"

The github repo has had MIT since v0.6.2 (`LICENSE` blob `278dcfb`, added in `fb6d14d` v0.6.2 release cut). The README has been wrong since then — it claims "Operator-private for now. License TBD before public squash release" but the actual repo state is MIT. **The README license line is the bug, not the LICENSE.** Hunk 8 fixes the README to point at the existing LICENSE file.

Cross-check commands used:

```bash
curl -s https://api.github.com/repos/blockops1/ai_camera_monitor/license
# → {"key":"mit","name":"MIT License","spdx_id":"MIT",...}

curl -s https://raw.githubusercontent.com/blockops1/ai_camera_monitor/main/LICENSE
# → MIT License / Copyright (c) 2026 The ai-camera-monitor authors / [standard 21-line MIT text]

git ls-tree github/main -- LICENSE
# → 100644 blob 278dcfb75ec0711aef0ed7b7caee92e15ca53b74  LICENSE

git ls-tree main -- LICENSE
# → (empty) LICENSE missing from local main
```

So local `main` (where the next release will be cut from) is missing LICENSE. When the next public release lands, ensure LICENSE is committed to the release branch — otherwise the next github release will be re-broken.
