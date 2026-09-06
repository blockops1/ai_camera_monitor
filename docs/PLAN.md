# Farm Surveillance v2 — Project Plan

**Author:** Jill  
**Started:** 2026-09-06  
**Source of truth:** This document. PLAN.md has no ancestors.

This is a fresh build, deliberately. The intent is a v2 surveillance
service whose processing pipeline is structurally the §11.117 linear
11-stage flow — no per-class subpackages, no per-class matchers,
no STATE singleton, no threat-level scaffolding. The legacy code in
`farm-surveillance-refactor/` (PER the §11.117 audit on 2026-09-06 at
`b7d1f4b` of `farm-surveillance-refactor`) was an incremental refactor
that did not collapse per-class architecture. v2 starts with the
target shape so no part of the linear pipeline is downstream of a
per-class envelope.

## Design

### Webhook input

Cameras POST alerts to a single webhook endpoint exposed by
`listener/listener.py`. The webhook receiver is short — it validates
the request, registers it on the queue, and returns 200 within
~250 ms. Heavy work happens off-thread.

### Pipeline output

Single Telegram chat (operator DM). Three explicit Telegram stages:

**TG#1** — fires AFTER `vision_model_1`. Image: full frame (best of
the 4 camera frames) annotated with subject bounding box. Body:
pairwise-differential composite thumbnail of the 4 frames; positions
of the moving subject in each frame; single-line "what" description
from VM1's classify output.

**TG#2** — fires AFTER `vision_model_2`. Image: subject close-up
crops (the 2 crops used as VM2 input). Body: VM2's output rendered as
operator-readable text. No threat-level field. Class-specific
prompts for person/animal/vehicle so each TG#2 message reads
differently per class.

**TG#3** — fires AFTER the matcher, only for vehicle. Body: match
hit (vehicle known, plate visible, recognizable match) or miss
(unknown vehicle). One image per result: cropped subject if matched,
differentially-composited full frame if miss.

### Pipeline (11 linear stages, single function)

The pipeline is `listener/pipeline.py::run(event)`. It is a single
async function with 11 numbered stages written top-to-bottom. Each
stage is small (~10-30 lines). Per-class branches happen at the
stage boundary, not inside stages.

```
[1] capture 4 frames        <- infra.capture_4_frames(event)
[2] crops + pairwise diff   <- infra.frame_diff (lifted)
[3] YOLO gate               <- infra.gate (lifted) -> QuickVerdict
[4] cooldown check          <- infra.pipeline_cooldown.should_suppress(camera, classification)
[5] vision model 1 (VM1)    <- vision-8B, 2 crops alone, NO gate hint
[6] TG#1                    <- telegram_formatter.format_alert(...)
[7] vision model 2 (VM2)    <- vision-8B, class-specific prompt
[8] TG#2                    <- telegram_formatter.format_detail(...)
[9] match                   <- vehicle_matcher.match(...); skip if not vehicle
[10] TG#3                   <- telegram_formatter.format_match(...); skip if not vehicle
[11] cooldown.record_hit    <- infra.pipeline_cooldown.record_hit(camera, classification)
```

VM1 returns `Classification` (vehicle, person, animal). VM2 prompt
switches on that classification. Stages 9-10 conditional on
classification == vehicle.

### Configuration

Three env files at repo root, mode 600, never committed:

- `camera-creds.env` — per-camera IP + HTTP/RTSP credentials (6 keys × 6 cameras = 36 keys)
- `telegram-creds.env` — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `llm-creds.env` — `VISION_LLM_URL/_MODEL/_TOKEN`, `TEXT_LLM_URL/_MODEL/_TOKEN`

`.env.example` templates committed with empty values.

### Cooldown keys

The §11.117 throttle keys on `(camera_id, classification)`. Two
distinct cooldowns exist:

1. **`MotionCooldown`** (lifted byte-identical from
   `infra/cooldown.py`) — minute-level webhook dedup. Stage-0
   concern: if the camera POSTs the same motion event twice within
   60s, the second is dropped before pipeline entry. Different
   concern from §11.117 throttle.

2. **`PipelineCooldown`** (built fresh in `infra/pipeline_cooldown.py`)
   — per-`(camera, classification)` throttle keyed on the gate's
   tentative classification. Stage-4 concern: if the gate has
   classified the same `(camera, class)` recently AND no match
   attempt succeeded, the event is dropped before VM1.
   `record_hit` at Stage 11 keys on the same tuple, with
   `matcher_hit=match_result` to let a successful match bypass the
   next cooldown in the same window.

The gate's tentative classification is the source of truth for
cooldown keying. VM1 may override it downstream; that override is
purely informational for Stages 7-10.

### LLaMA topology

3 llama-servers, all kept up per operator direction:

- `35B-MTP` on `:8093` — kept configured but unused in v2 pipeline
- `vision-8B` on `:8080` — VM1 + VM2 in v2 pipeline
- `text-9B` on `:8081` — kept configured but unused in v2 pipeline

`llm-creds.env` keeps `*_LLM_*` keys for the two unused servers
because the topology might shift and reuse them.

### Listener deployment

Same plist pattern as v1 (`ai.farm.surveillance-listener.plist`).
The v2 binary runs `python -m listener`. Plist registered with
`launchctl bootstrap`. Bounce pattern: rename plist to
`.disabled-<date>-<reason>`, run `launchctl bootout`, restart by
renaming back and `launchctl bootstrap`.

The current refactor listener is in maintenance mode (plist renamed
`.disabled-20260906-1117stripdown`). v2 listener does not start
until §11.117 design is verified end-to-end and operator approves
bounce.

## Stages & acceptance

### Stage 0 — scaffold (DONE 2026-09-06, commit `51a6321`)

- Empty v2 repo at `~/farm-surveillance-v2/`
- 11 proven modules lifted byte-identical from refactor repo
- `.gitignore` excludes env files and run-time artifacts
- 3 `.env.example` templates committed
- `docs/V2-LIFT-PLAN.md` inventories what is not lifted

### Stage 1 — `infra/pipeline_cooldown.py` (PipelineCooldown)

Build fresh, mirror the PR1 shape from refactor repo `1fe8986`:

```
class PipelineCooldown:
    def should_suppress(self, camera_id: str, classification: str) -> bool: ...
    def record_hit(self, camera_id: str, classification: str, *, matcher_hit: bool) -> None: ...
```

Internal dict keyed on `(camera_id, classification)` tuple. TTL
configurable, default 60s. `should_suppress` returns True if the
key has a recent recorded hit AND `matcher_hit` was False.

### Stage 2 — `infra/vision_analyzer.py::verify_class()` (VM1)

Single function that takes 2 crops (bytes) and asks vision-8B on
`:8080` to classify as `vehicle`/`person`/`animal`. Prompt has NO
gate hint and NO suggestion of what the subject might be. Output
parses as JSON `{"class": "vehicle|person|animal"}`. Returns a
classification enum value from `infra.classify_schema`.

### Stage 3 — `infra/vision_analyzer.py::detail_class()` (VM2)

Single function that takes 2 crops + classification, and asks
vision-8B for class-specific detail. Three prompts (selectable by
classification arg):

- `vehicle` — describe vehicle: type (car/SUV/truck/motorcycle/bicycle),
  color, distinguishing marks, plate visible?
- `person` — describe person: clothing, hat, carried items, posture,
  approximate height/build, face visible?
- `animal` — describe animal: species if obvious, color/size, breed
  markers if any.

Returns a structured dict. No threat-level field at any classification.

### Stage 4 — 3 Telegram stage functions

Telegram Formatter is pure-functions only. Each function returns
`(caption_text, image_bytes)` ready to send. Caller handles the
Telegram API call.

- `telegram_formatter.alert.py::format_alert(event, vm1_result, frame_4) -> tuple[str, bytes]`
- `telegram_formatter.detail.py::format_detail(vm2_result, crops) -> tuple[str, bytes]`
- `telegram_formatter.match.py::format_match(match_result, vm2_result, crops) -> tuple[str, bytes]`

### Stage 5 — `listener/pipeline.py::run()` (the 11-stage linear function)

Single async function. Stages 4-11 written inside one `try`-block
with explicit early returns for cooldown drops. Looks like the
diagram above.

### Stage 6 — `listener/listener.py::handle_webhook()`

Webapp or FastAPI handler. Validates request signature, enqueues,
returns 200. Target ~80 lines.

### Stage 7 — tests

Target ~50-100 tests:

- `tests/test_pipeline_cooldown.py` — key shape, TTL, matcher_hit bypass
- `tests/test_vision_analyzer.py` — VM1 prompt has no gate hint;
  VM2 prompt switches by class
- `tests/test_pipeline.py` — 11 stages fire in order; cooldown
  drops Stages 5-10; non-vehicle skips Stages 9-10
- `tests/test_listener_webhook.py` — webhook validates + enqueues
- `tests/test_telegram_formatter.py` — three formatters; no
  threat-level field at any classification

No test asserting behavior that §11.117 invalidates. No test
importing from refactor repo.

## Acceptance criteria

The v2 release ships when:

1. `pytest tests/` passes 100% on the target test set
2. `ruff check .` clean
3. Stage 5 (pipeline.py) is ~200-250 lines, not 2,200
4. Zero per-class branches in `pipeline.py::run`
5. `cooldown.record_hit` keys on `(camera, classification)` only
6. VM1 prompt has no gate hint (snapshot-tested)
7. Stage 9 fires only for `classification == vehicle`
8. Three Telegram functions exist; no TG emission in pipeline code
9. public squashed build of v2 lands at
   `blockops1/ai_camera_monitor`
10. operator green-lights listener bounce from maintenance mode
    to v2 binary

## What we are not carrying forward

- The 4,568 lines of legacy per-class pipeline code documented in
  `farm-surveillance-refactor/docs/REFACTOR-STRIPDOWN-2026-09-06.md`
- The 2,213 tests asserting those legacy behaviors
- The STATE singleton counter machinery
- The Qwen text-9B classify prompt + validator
- Threat-level LLM scaffolding
- The 11-stage cascade (`two_call_cascade.py`) — VM1 replaces call 1
- All `_send_arriving_message` envelopes per class

The 11 lifted modules are not "v1 code in v2." They are the parts of
v1 that are already structurally clean (no per-class branching, no
listener-cycle, single-purpose). v2 composes them into a linear
pipeline that v1 never had.
