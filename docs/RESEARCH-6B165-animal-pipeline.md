# Research: Animal Pipeline (Phase.165)

**Status:** DRAFT — research-only. Awaiting Note's approval before any PLAN.md entry or code.
**Decided 2026-08-29** (chat):
- All 7 YOLO animal classes supported (dog, cat, horse, sheep, cow, bear, bird) — bear is real on this property.
- No animal face rec, no embedding-based re-ID. Stable-attribute matching only (like 6B.163).
- 2-tier threat (concerning / routine), not the 4-tier used by the person pipeline.
- **Qwen is authoritative for species, NOT the gate YOLO.** Note 2026-08-29: *"the vision model is smarter than Yolo. So whatever the vision model ends up deciding, I think it's what we're going to go with regarding animals."* Gate YOLO is a fast filter to suppress noise at cooldown; Qwen decides what the animal actually is.

---

## Problem

The current event pipeline (`listener/listener.py → _process_alert_safe`) routes events like this:

```
event in {person, people} on a person-gatekeeper camera  →  process_person_event
everything else                                          →  process_alert (vehicle path)
```

There is **no `process_animal_event`**. Animal events fall to the vehicle pipeline, which would try to match a vehicle against an animal — wrong. They are effectively suppressed today by `gate_cooldown.default: 120s` (verified in `logs/listener.log`, e.g. `gate_cooldown: suppressed (camera=Outside Front Garage event='animal')`).

A small fraction of "animal" events are correctly demuxed to the person pipeline when the gate's YOLO confirms `class=person` (verified: `event_promotion: 'animal' → 'people'`). That path is fine and untouched by this phase.

`QUEUE_ANIMAL` is already defined in `listener/listener.py:397` but has no consumer.

**Goal.** Mirror the person pipeline for animals: separate Qwen pass, separate enrollment registry, separate Telegram body, per-camera cooldown. Bears get a different (more urgent) Telegram than a tabby at the back door at 2pm.

---

## Architecture (mirror of person pipeline)

### New modules

| File | Purpose | Mirrors |
|---|---|---|
| `listener/animal_event_pipeline.py` | 4-stage alert pipeline | `listener/person_event_pipeline.py` |
| `infra/animal_matcher.py` | Stable-attribute match against known animals | `infra/person_matcher.py` (no face rec) |
| `infra/animal_prompt_template.py` | Qwen schema for animals | `infra/person_prompt_template.py` |
| `data/animals/known_animals.json` | Enrolled-animal registry | `data/vehicles/known_vehicles.json` |
| `listener/tests/test_animal_*_6B165.py` | Unit + integration tests | `listener/tests/test_person_event_pipeline_6B106.py` |
| `scripts/enroll_animal.py` | Manual enrollment CLI | `scripts/enroll_person.py` |

### Edited files

| File | Change |
|---|---|
| `listener/listener.py` | Route `event_type='animal'` → `process_animal_event()`; consume `QUEUE_ANIMAL` |
| `infra/prompt_templates.py` | Add `mode='animal'` to `select_prompt_template()` |
| `infra/gate_cooldown.py` (if needed) | Honor per-camera `gate_cooldown.animal` (already supports arbitrary classes, but verify) |
| `config/motion_gate_thresholds.json` | Add `animal` cooldowns to every camera's `gate_cooldown` block |
| `infra/motion_gate_pipeline.py` (if needed) | Verify `gate_enabled.animal` toggle is honored (likely already covered by `is_gate_enabled()` generic impl) |

### Untouched

- Person pipeline (6B.163 stable-attrs, 6B.164 vision_attrs logging)
- Vehicle pipeline
- Gate `event_promotion: 'animal' → 'people'` (correct behavior, do not disturb)

---

## Stage flow (animal pipeline)

Same shape as person pipeline. Cut points where animals differ from people are flagged.

### Stage 1 — capture
- Mirror of `person_event_pipeline.capture_stage()`.
- Difference: animals don't have face crops. Skip the face-crop selection logic from `select_best_frame_stage`; instead pick the widest-frame crop that contains the gate's detected bbox.
- Save to `data/frames/<alert_id>/frame_001..N.jpg` + the gated bbox crop (single crop, not multiple).

### Stage 2 — identify (vision)
- Call `infra/animal_prompt_template.build_animal_prompt()` → Qwen schema.
- Qwen returns: `species`, `breed`, `color`, `size`, `distinctive_markings`, `behavior`, `owner_hint`, `confidence`.
- `_coerce_vision_result()` mirror — handles empty / malformed Qwen responses (returns NoMatch with `reason='qwen_returned_empty'`).
- `_log_vision_attrs()` — same pattern as 6B.164, logs every animal Qwen pass for post-hoc debugging.

### Stage 3 — match
- `infra/animal_matcher.match_animal(vision_result, known_animals)` → `MatchVerdict | NoMatch`.
- No face-rec path. No clothing. **Just stable-attribute weighted ensemble** like 6B.163.
- **Qwen is the source of truth for species.** Gate YOLO runs first as a fast cooldown filter (suppress noise without ever calling Qwen); once an alert reaches Qwen, Qwen's `species` field is authoritative for matching. Known-animal entries are filtered by `species == vision_result.species` before scoring — if Qwen says "bear" and we have 3 enrolled dogs and 1 enrolled bear, only the bear is scored; the dogs get 0.0 score (species mismatch).
- Priority order (recommended):
  1. **species** (from Qwen — hard filter, must match)
  2. **color** (high weight)
  3. **size** (medium weight)
  4. **distinctive_markings** (medium weight; freeform string, use sequence ratio)
  5. **breed** (lower weight — Qwen often wrong on breeds, treat as soft signal)

  Weighted ensemble example (tuneable):
  ```
  ensemble_score = (
    color_score       * 0.40 +
    size_score        * 0.20 +
    markings_score    * 0.25 +
    breed_score       * 0.15
  )
  ```
  Threshold = 0.55 (lower than person pipeline 0.65 — fewer features to match on, and breeds are noisy).

- Result: `MatchVerdict(animal_id='a_black_labrador', confidence=0.82)` or `NoMatch(reason='no_known_animal', best_candidate='a_brown_tabby', best_candidate_confidence=0.41)`.

### Stage 4 — emit
- Telegram body: shorter than person body. Format:
  ```
  🐾 Animal: dog (golden retriever)
  Matched: <name> (0.82) — or — Unknown (best guess: brown tabby, 0.41)
  Color: golden | Size: large
  Markings: no collar, docked tail
  Camera: Outside Front Garage @ 13:53 EDT
  Threat: routine | cooldown OK
  ```
- 2-tier threat:
  - `concerning` — bear, large unknown mammal at night, any animal on door camera between 22:00–06:00, multiple unknown animals in single alert.
  - `routine` — everything else.
- Frames: send the single bbox crop (not 2-image album, not 6-image media group). Animals are simpler than people.

---

## Per-camera config additions

`config/motion_gate_thresholds.json` — add `gate_cooldown.animal` to every camera. Proposed defaults (tuneable after week 1 of data):

```json
"Outside Front Solar":  { ..., "gate_cooldown": { ..., "animal": 300 } }
"Outside Front Garage": { ..., "gate_cooldown": { ..., "animal": 300 } }
"Outside Back Solar":   { ..., "gate_cooldown": { ..., "animal": 300 } }
"Front Door Outside":   { ..., "gate_cooldown": { ..., "animal": 180 } }
"Outside Front Power":  { ..., "gate_cooldown": { ..., "animal": 300 } }
"Back Door Inside":     { ..., "gate_cooldown": { ..., "animal": 60  } }  // cats wander in, we want each detection
```

`gate_enabled.animal: true` is the default (matches 6B.156 — all cameras are gatekeepers for all classes).

---

## Known animals registry

`data/animals/known_animals.json` — top-level shape mirrors `known_vehicles.json`:

```json
{
  "version": 1,
  "animals": [
    {
      "id": "a_mr_whiskers",
      "species": "cat",
      "name": "Mr. Whiskers",
      "owner": "name one",
      "role": "pet",
      "verified": true,
      "stable_attributes": {
        "breed": "tabby",
        "color": "brown",
        "size": "medium",
        "markings": "white paws, green collar"
      },
      "enrolled_at": "2026-08-29",
      "source_alerts": ["dbc711d0-..."]
    }
  ]
}
```

`loader` mirrors `known_vehicles/load_known_vehicles.py`: `load_known_animals() -> list[dict]`.

Initial population: enroll **0–2 verified animals** from observed data during the implementation phase (e.g. name one's cat if any alert shows a consistent indoor cat). No need to enroll every observed animal — many will be "unknown" by design.

---

## Qwen schema (animal)

`infra/animal_prompt_template.ANIMAL_SCHEMA_JSON`:

```json
{
  "type": "object",
  "properties": {
    "species": {
      "type": "string",
      "enum": ["dog", "cat", "horse", "sheep", "cow", "bear", "bird", "unknown"]
    },
    "breed":       { "type": ["string", "null"] },
    "color":       { "type": ["string", "null"], "enum": ["black","brown","white","gray","tan","golden","mixed",null] },
    "size":        { "type": ["string", "null"], "enum": ["small","medium","large",null] },
    "distinctive_markings": { "type": ["string", "null"] },
    "behavior":    { "type": ["string", "null"], "enum": ["walking","running","standing","sitting","lying","eating","aggressive","unknown",null] },
    "owner_hint":  { "type": ["string", "null"] },
    "confidence":  { "type": "number", "minimum": 0, "maximum": 1 }
  },
  "required": ["species", "confidence"]
}
```

Qwen prompt: "Identify and describe the animal in this frame. Return the species (dog/cat/horse/sheep/cow/bear/bird/unknown), breed if you can identify it, color, size, distinctive markings, behavior, and your confidence. Your species decision is authoritative — be careful and specific. If you see a dog, say dog. If you see a bear, say bear. Do not classify the animal based on its surroundings."

---

## Phase plan (incremental, reviewable)

Each phase is small. Each is deployable + revertable.

### §11.86 — Phase.165.1: pipeline scaffold
- Create `listener/animal_event_pipeline.py` with `AlertContext` dataclass + `process_animal_event()` skeleton.
- Wire dispatch in `listener/listener.py`: route `event_type='animal'` → `process_animal_event`.
- No Qwen call yet, no Telegram yet — just confirms the route works and writes an INFO log per animal alert.
- **Tests:** confirm one of today's `gate_cooldown: suppressed` alerts becomes `animal_pipeline: received` (audit log only, no Telegram).

### §11.87 — Phase.165.2: animal matcher
- `infra/animal_matcher.py` with `match_animal()`.
- Weighted ensemble per the priority order above.
- **Tests:** `tests/test_animal_matcher_6B165.py` — happy path, species mismatch = skip, all-null attrs = score 0.0, weighted ensemble arithmetic.

### §11.88 — Phase.165.3: prompt template + select_prompt_template mode
- `infra/animal_prompt_template.py` with `ANIMAL_SCHEMA_JSON` + `build_animal_prompt()`.
- Edit `infra/prompt_templates.py` `select_prompt_template(mode='animal', ...)`.
- **Tests:** schema parses, prompt mentions "animal" not "person".

### §11.89 — Phase.165.4: per-camera animal cooldowns + gate_enabled toggle
- Add `gate_cooldown.animal` to every camera in `config/motion_gate_thresholds.json`.
- Verify `infra/is_gate_enabled(camera, 'animal')` honors the toggle (likely already works generically; confirm).
- **Tests:** cooldown applied per camera; toggle respected.

### §11.90 — Phase.165.5: known animals registry + loader
- `data/animals/known_animals.json` initial empty `{"version": 1, "animals": []}`.
- `known_animals/load_known_animals.py`.
- Enroll 1-2 verified animals from observed data (cat, dog — whatever's clear).
- **Tests:** loader returns enrolled animals, schema validated, dedup by id.

### §11.91 — Phase.165.6: Telegram emit (2-tier threat)
- `infra/telegram_formatter/format_animal_alert_body()`.
- 2-tier threat scoring function (concerning / routine) — explicit rule list.
- Wire into `emit_result_stage`.
- **Tests:** concerning triggers on bear + night; routine triggers on daytime cat.

### §11.92 — Phase.165.7: end-to-end tests + deploy
- `listener/tests/test_animal_pipeline_6B165.py` — full pipeline walkthrough on a real alert.
- `pytest listener/tests/ tests/ infra/tests/` — must remain green (276 + 43 + infra passes preserved).
- Listener restart.
- Monitor `logs/listener.log` for 24h, then review with Note.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Animal Qwen calls flood Qwen (10x more alerts than people) | Per-camera cooldown is mandatory in §11.89 before Qwen is even called |
| Qwen hallucinates species (e.g. dog → bear at night) | **Accepted.** Per Note 2026-08-29, Qwen's species is authoritative. YOLO is only used as a pre-Qwen cooldown filter — if a dog is misclassified as a bear by Qwen, the alert fires as a bear (concerning), which is the safer failure mode. We will catch these in 7-day observation and tune the prompt if it becomes a recurring issue |
| Bear detection rare but urgent — false positive risk | Gate YOLO threshold for bear stays at 0.6 (default), no lowering. Bear-class events at night still escalate to `concerning` threat |
| Cats detected on every BDI frame | BDI cooldown = 60s (shortest), but the matching tier still gates noise |
| Animals fall through to vehicle pipeline accidentally | `_process_alert_safe` dispatch order: person → animal → vehicle (explicit) |
| 6B.163 stable-attribute code is reused — drift risk | `infra/animal_matcher.py` is a NEW module, not a shared file |

---

## Acceptance criteria (phase 6B.165 end-state)

1. Animal event on OFG cat at 14:00 EDT → Telegram body "🐾 Animal: cat (tabby), Matched: Mr. Whiskers (0.78), routine, cooldown OK." Single crop photo attached.
2. Bear event on OFS at 03:00 EDT → Telegram body "🐾 Animal: bear, Unknown, concerning (large mammal + night), camera OFS." Single crop photo attached.
3. Unknown dog event at 13:53 EDT → Telegram body "🐾 Animal: dog (?), Unknown (best: Mr. Whiskers @ 0.41), routine." Single crop photo attached.
4. Same cat reappears within 60s on BDI → suppressed by cooldown, audit log only, no Telegram.
5. All person/vehicle tests still pass (276 + 43 + infra unchanged).
6. Listener PID stable; new `animal_pipeline` log lines visible in `logs/listener.log`.

---

## Out of scope (deferred)

- Animal re-ID via embeddings / face rec (deferred indefinitely — needs research, no good open model on mac mini)
- Cross-camera animal tracking (a single animal appearing on multiple cameras in sequence → "same bear")
- Bird-species identification (we'd need a different model entirely)
- Per-animal Telegram filter (mute Mr. Whiskers, alert on bear only) — possible future feature
- Animal telemetry / activity heatmaps — not requested
