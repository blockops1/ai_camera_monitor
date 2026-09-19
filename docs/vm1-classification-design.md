# VM1 Vision Classification Design

**Task:** US-060e — VM1 schema and classification redesign
**Date:** 2026-09-19
**Status:** Implemented

## 1. Purpose

VM1 is the first-stage vision classifier in the farm surveillance pipeline.
It receives two motion-streak crops from a Reolink camera event and produces
a lightweight classification that determines which VM2 detail mode to use.
VM1 must be fast, cheap, and reliable — it gates the expensive downstream
calls.

## 2. Architecture Overview

```
Camera → YOLO gate (tentative) → VM1 (class/subclass) → VM2 mode select →
  TG#1 (alert) → TG#2 (detail) → TG#3 (match)
```

- **YOLO gate** (`infra/gate.py`): lightweight heuristic gate based on crop
  analysis. Determines if an alert fires at all.
- **VM1** (`infra/vm1_prompt.py`): classifies subject into vehicle/person/animal/unsure.
  The response_format layer constrains Qwen to JSON with defined fields.
- **VM2** (`infra/detail_mode.py`): detailed classification in mode-specific
  prompt (vehicle/person/animal/unsure).

## 3. VM1 Schema Evolution

### 3.1 Old Schema (pre-2026-09-19)

```json
{
  "class": "vehicle" | "person" | "animal" | "unsure",
  "confidence": 0.92
}
```

Problems:
- Confidence was a float (e.g., 0.92) — difficult to map to Telegram display
  ("0.92" is not human-readable)
- No subclass information — could not show "dog", "sedan", etc.
- No count — unknown if one animal or a pack
- No face_visible — person classification could not distinguish face-landed
  from body-only
- No notes — lost useful observations

### 3.2 New Schema (current)

```json
{
  "class": "vehicle" | "person" | "animal" | "unsure",
  "subclass": "string" | null,
  "confidence": "definite" | "likely" | "unsure",
  "count": "integer" | null,
  "face_visible": "boolean" | null,
  "notes": "string" | null
}
```

Field definitions:

- **class** — primary subject classification. One of the four allowed values.
- **subclass** — more specific identification. E.g. "dog", "sedan", "labrador".
  Null if class is "unsure" or if the subclass is unobservable.
- **confidence** — confidence level as a human-readable enum:
  - "definite" — high visual certainty, clear subject, unambiguous
  - "likely" — best call with caveats (lighting, partial occlusion)
  - "unsure" — guessing between plausible alternatives
- **count** — number of subjects visible. Null if unobservable.
- **face_visible** — true if a human face is visible; null if class is not
  "person" or face is not visible.
- **notes** — additional observations. Null if none.

### 3.3 Telegram Caption Rendering

The new schema produces more informative captions:

- **With subclass:** `Vision: animal/dog (definite)`
- **Without subclass:** `Vision: vehicle (likely)`
- **Person with face:** `Vision: person (likely)` with face_visible=true
  in the structured result (used by detail.py for VM2 prompt refinement)

## 4. Prompt Design

### 4.1 Prompt Text (`infra/vm1_prompt.py::PROMPT_TEXT`)

The prompt text:
- Describes the two-crop input format
- Defines the four-class taxonomy with clear boundaries
- Explains confidence levels with natural language criteria
- Provides example output
- Explicitly forbids prose, markdown, or extra keys

### 4.2 Schema Constraint (`infra/vm1_prompt.py::SCHEMA_JSON`)

The response_format JSON schema enforces:
- All six required fields
- Enum constraints on `class` and `confidence`
- Nullability on optional fields
- No additional properties (no extra keys)

The schema and prompt text are kept in sync — this is a hard requirement
(per Rule 8 of the vision-prompt-design skill).

## 5. Dispatch Surface

VM1 output's `class` field feeds into the `detail_mode` dispatch in
`infra/vision_analyzer.py`. The dispatch dict uses the `MODE_NAME` constant
(`"vm1_classify"`) as the canonical key.

The dispatch selects one of four VM2 modes:
- `vehicle` — license plate, color, make, model
- `person` — attributes, signature (stable/transient)
- `animal` — species, breed, size, distinctive features
- `unsure` — no detail call (recoverable)

## 6. Operator Constraints

### 6.1 No Hinting (Rule from 2026-09-06)

VM1 must NOT receive any hint about what is in the image. The two crops
alone determine the classification. This is enforced by the prompt text
and the dispatch pipeline (no camera metadata, no YOLO label injected
into the VM1 prompt).

### 6.2 Fourth Class Mandatory (Rule 6)

Qwen defaults toward picking a class. The "unsure" option is mandatory to
prevent false positives. A wrong vehicle-vs-person call is worse than a
missed call — wrong calls trigger expensive downstream processing for
nothing.

## 7. Files Modified

| File | Change |
|------|--------|
| `infra/vm1_prompt.py` | New schema (subclass, count, face_visible, notes); confidence as enum |
| `telegram_formatter/alert.py` | Caption renders "class/subclass (confidence)" format |
| `telegram_formatter/detail.py` | Animal mode uses subclass field |
| `tests/test_alert_message.py` | Updated `_make_vm1_result` helper |
| `tests/test_alert_message_caption.py` | Updated `_make_vm1_result` helper |
| `tests/test_alert_route.py` | Updated `_pipeline_result` helper |
| `tests/test_pipeline_segmented.py` | Updated `_VM1_RESULT` constant |
| `docs/vm1-classification-design.md` | New — this design document |

## 8. Test Strategy

### 8.1 Unit Tests
- All existing unit tests updated to use the new VM1 schema
- `_make_vm1_result()` factory produces valid new-schema dicts by default
- Tests verify caption rendering with subclass present and absent

### 8.2 Replay Integration Test
- `tests/test_vm1_alert_982fb50c_replay.py` runs the full pipeline
  with real frame data from `data/frames/OUTSIDE_FRONT_GARAGE/`
- Verifies end-to-end classification pipeline with new schema

### 8.3 Deleted Tests
- Tests exercising the old `{class, confidence}`-only schema were removed
  (they are no longer applicable to the new schema)

## 9. Future Work

- Add subclass validation: ensure subclass values conform to expected
  vocabularies per class
- Add count validation: count must be >= 1 when not null
- Consider adding a `bounding_box` field for post-classification cropping
- Extend VM2 to use subclass information for more targeted detail prompts
