# BUGS 2026-09-02: Person identification — radical simplification

## Status

DRAFT — superseded by PLAN §11.115 (2026-09-02 PM). This doc is now the **symptom + root-cause record** for the original zero-hits bug; the fix lives in §11.115.

## Symptom (verbatim from Note, 2026-09-02)

> *"I walked around the building every camera got a shot at my face all I've gotten is a few alerts with shots on my back. I don't know what the hell the system is doing"*

> *"I want you to work on the improving the person identification. I've been walking around here for the past hour I don't think I've gotten any identification hits"*

## Note's directives (verbatim, in order received today)

1. > *"instead of a cool down it should prioritize any crops that have a face in it and pass those through"*
2. > *"move the cool down to after we get an answer back from the vision model"*
3. > *"the entire purpose of the cooldown is to prevent me from getting excessive telegram notifications. We can run the models as much as we want with as many images as we want"*
4. > *"There should be only one pipeline, starting at the web hook, going to the pairwise differential, going to YOLO, going to the first call to the vision model. That should be a singular pipeline. Only after the call to the vision model should it diverge."*
5. > *"the first call to Quinn is going to be with a prompt to define if the two crops that are sent are of a vehicle or a person or an animal or something else. Depending on that answer, there is a second call to Quinn with a vehicle, animal, or person specific prompt."*
6. > *"after that answer comes back from Quinn, the vehicle goes to the vehicle mature, animal goes to the animal matcher, person goes to the face detection, and then the person matcher"*
7. > *"ignore any web hook from a camera that does not have an RTSP stream running. So by definition any camera that has an RTSP stream set up is a gatekeeper and everything else gets ignored."*
8. > *"the cool down code removed"*
9. > *"We're doing a radical simplification of this system and it's gonna make it more reliable and deterministic"*
10. > (clarification) *"the prompt that we're going to use for Quinn just gonna ask which of the two images has a better face visible and then we will choose that image and use that one to send to the face recognition application"* (2026-09-02 PM)
11. > (clarification) *"for [class] other... we're just gonna log for now. No telegram"* (2026-09-02 PM)
12. > (clarification) *"just call it crop a and crop B those are great ways to refer to it"* (2026-09-02 PM)
13. > *"Yeah I always want both crops sent in the telegram body. You can call it better crop"* (2026-09-02 PM) — Telegram body always includes both `crop_a` + `crop_b`. Schema field is `better_crop: enum`.
14. > *"regarding the face you should phrase it more like if the face is visible which one looks better so it doesn't try to just decide that a face is visible when there isn't actually one"* (2026-09-02 PM) — prompt phrasing critical: only return `crop_a`/`crop_b` when a face IS actually visible; default to `neither` when uncertain. Prevents false-positive face_visible picks (root cause of Bug C).

## Synthesis

Note's nine directives describe one design: **radical simplification**. The current production system has multiple competing sources of truth for "what is this alert?" (camera routing, gate verdict, event type, YOLO, three different Qwen calls). Note is collapsing them into one pipeline with one authoritative classifier.

**The 3 bugs from the earlier draft of this doc are absorbed by the architecture change:**

| Original bug | What §11.115 does to it |
|---|---|
| Bug A — cooldown runs before any work | Cooldown module is deleted entirely. No replacement at entry. |
| Bug B — gate suppresses single-crop person | Gate's Rule 5 catch-all removed. Gate becomes pure noise suppression. Qwen call 1 is the authoritative classifier. |
| Bug C — Qwen face_bbox hallucinates, InsightFace finds 0 faces | `face_bbox` field dropped from person schema (call 2). Schema now uses `better_crop: enum` ("crop_a"|"crop_b"|"neither"). InsightFace runs on the Qwen-chosen crop (Note's design). |

## Original root-cause inventory (archived for reference)

### Bug A — Cooldown runs before any work, suppresses person events wholesale

**Where:** `infra/gate_cooldown.py` + `listener/listener.py:1775` (call site).

**Listener log proof** (alert `0603da05`, 14:25:29, Outside Back Solar):
```
[0603da05] gate_cooldown: suppressed (camera='Outside Back Solar' event='people') —
           no gate, no pipeline, no Telegram
```

`is_in_gate_cooldown(camera_name, event)` runs at `listener.py:1775` BEFORE frame capture, BEFORE the motion gate, BEFORE Qwen. Once a person event has fired on a camera, every subsequent person event on that same camera is suppressed for the configured window (120s on OBS, 0/absent elsewhere — per `config/motion_gate_thresholds.json`).

**§11.115 resolution:** delete `infra/gate_cooldown.py` entirely.

### Bug B — Motion gate suppresses `high_conf_person_not_vehicle_no_pipeline` on non-gatekeeper cameras

**Where:** `listener/motion_gate_pipeline.py:766-771` (Rule 5 catchall).

**Listener log proof** (15:01:11, Back Door Inside):
```
motion_gate: decision=suppress class=person conf=0.60
             reason=high_conf_person_not_vehicle_no_pipeline
```

**§11.115 resolution:** strip Rule 5 person-suppression. Gate becomes pure noise filter (suppress if motion-detected-but-YOLO-empty). Qwen call 1 re-classifies downstream.

### Bug C — Face crop bbox can land off the actual face (Qwen bbox hallucination)

**Where:** `infra/prompt_templates.py` (Qwen prompt) + `infra/person_prompt_template.py` (schema) + `infra/image_prep.py:crop_face_region_from_4k` + `listener/person_event_pipeline.py:_run_face_recognition`

**§11.115 resolution:** drop `face_bbox` field from person schema (call 2 prompt). Replace `face_visible: bool` with `better_crop: enum` ("crop_a"|"crop_b"|"neither"). Qwen call 2 picks which crop has the better face; InsightFace runs on that crop (or skips if "neither"). No Qwen bbox lookup, no 640×640 re-crop.

## The new architecture (PLAN §11.115)

See `PLAN.md` §11.115 for the full design. The summary:

```
webhook
  → RTSP-presence filter (cameras with RTSP = gatekeepers; rest dropped)
  → pairwise_diff (frame_2, frame_3) + (frame_3, frame_4) → subject_bbox_a/b
  → crop_a = frame_2.crop(subject_bbox_a), crop_b = frame_3.crop(subject_bbox_b)
  → YOLO on crop_a + crop_b (cheap pre-filter)
  → Qwen call 1: SHARED CLASSIFY (vehicle/person/animal/other)
  → DIVERGE BY CLASS:
       vehicle → Qwen call 2 (vehicle schema) → vehicle matcher → Telegram
       person  → Qwen call 2 (person schema: better_crop enum + attributes + signature, NO bbox)
               → Qwen picks which crop has the better face (crop_a|crop_b|neither)
               → if better_crop != neither: InsightFace on chosen crop → face matcher
               → Telegram send includes BOTH crop_a + crop_b (2-image media group)
       animal  → Qwen call 2 (animal schema) → animal matcher → Telegram
       other   → log only (no Telegram; per Note 2026-09-02 PM)
```

**No cooldown. No camera-as-gatekeeper. No gate-as-router. One pipeline, divergence after Qwen call 1.**

## Tests (per `software-development-practices` §3 — TDD)

Tests written before implementation, per the workflow.

### New tests

- `tests/test_classify_prompt.py` — schema + prompt template, all 4 classes (`vehicle`, `person`, `animal`, `other`) parse correctly.
- `tests/test_classify_validator.py` — validates Qwen response, falls back to `"other"` on parse failure / out-of-vocabulary class.
- `tests/test_two_call_cascade.py` — orchestrates call 1 + call 2, asserts call 2 schema is class-correct.
- `tests/test_single_pipeline.py` — end-to-end. ~15 tests:
  - webhook with no RTSP camera → drop, log `no_rtsp_dropped`
  - webhook with RTSP camera → pipeline runs end-to-end
  - YOLO-empty motion → suppress (no Qwen call)
  - Qwen call 1 returns `"vehicle"` → call 2 uses vehicle schema
  - Qwen call 1 returns `"person"` → call 2 uses person schema, no `face_bbox` field
  - Qwen call 1 returns `"animal"` → call 2 uses animal schema
  - Qwen call 1 returns `"other"` → log only (no Telegram), per Note's directive
  - Qwen call 1 parse failure → fall back to `"other"`
  - person path: better_crop=crop_a → InsightFace on crop_a
  - person path: better_crop=crop_b → InsightFace on crop_b
  - person path: better_crop=neither → skip InsightFace
  - vehicle path: matcher + emit + notify (existing tests reused)
  - animal path: matcher + emit + notify (new, scaffold → full)

### Tests deleted

- `listener/tests/test_gate_cooldown.py` — module deleted
- `listener/tests/test_listener_gate_routing.py` — pipeline dispatch removed
- `listener/tests/test_motion_gate_pipeline.py` Rule 5 tests — suppression logic removed
- `listener/tests/test_animal_pipeline_6B165_4.py` cooldown parts — module deleted

### Tests rewritten

- `listener/tests/test_person_event_pipeline.py` — rewrite to test call 2 only (no `face_bbox`)
- `listener/tests/test_vehicle_pipeline/*` — pipeline orchestration tests rewrite to test call 1+2 orchestration; matcher/emit/notify tests stay

## Risk + rollout

**Risk: high.** Radical simplification. Big-bang merge after observation period.

**Why this is right:** fewer sources of truth = more deterministic. One pipeline = one mental model. Two Qwen calls per event but only when class-relevant; class-`other` exits early.

**Rollback:** develop on feature branch; merge to main only after §11.115.14 (24-48h observation).

## Execution outcome

Leave blank until §11.115 greenlit.
