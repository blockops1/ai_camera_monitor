# v2 Lift Plan — modules from farm-surveillance-refactor that v2 imports

Generated 2026-09-06 at v2 repo start. **No-code-only manifest** —
every file below is measured by `grep` for cross-deps. Files with
zero `infra.`, `listener.`, `telegram` imports are "safe lift" (no
circular deps). Files with internal `infra.*` deps are lifted along
with their deps.

## Safe-lift (zero infra/listener/telegram imports)

| File | Lines | Role | v2 path |
|---|---|---|---|
| `infra/cooldown.py` | 279 | `MotionCooldown` (minute-level webhook dedup) | `infra/cooldown.py` |
| `infra/gate_cooldown.py` | 264 | `is_in_gate_cooldown` (queue routing dedup) | `infra/gate_cooldown.py` |
| `infra/frame_diff.py` | 606 | pairwise_diff + bbox helpers | `infra/frame_diff.py` |
| `infra/classify_schema.py` | 111 | `ClassLabel` enum (vehicle/person/animal) | `infra/classify_schema.py` |
| `infra/quick_classifier.py` | 664 | YOLO gate + ONNX runtime | `infra/quick_classifier.py` |
| `vehicle_position/motion_detector_impl.py` | 349 | trajectory computation (§11.176) | `vehicle_position/motion_detector_impl.py` |
| `infra/motion_types.py` | TBD | `MotionResult`, `MovingObject` dataclasses | `infra/motion_types.py` |

## Lift-with-deps (depend on infra.paths and infra.timezone)

| File | Lines | Role | v2 path |
|---|---|---|---|
| `infra/vision_cache.py` | 269 | seconds_since_last_person | `infra/vision_cache.py` |

Lift together with `infra/paths.py` + `infra/timezone.py`.

## Lift-with-listener-deps (need to be reordered)

| File | Lines | v2 disposition |
|---|---|---|
| `listener/motion_gate_pipeline.py` | 1,115 | move to `infra/gate.py` (it's a gate, not a listener concept) |
| `vehicle_position/motion_detector.py` | TBD | move to `infra/motion_detector.py` |
| `vehicle_position/crop_extractor.py` | TBD | move to `infra/crop_extractor.py` |

## Not lifted (architecture debt being deliberately dropped)

| File | Lines | Why dropped |
|---|---|---|
| `listener/vehicle_pipeline/` (9 files) | 1,978 | per-class pipeline; §11.117 uses linear pipeline |
| `listener/vehicle_event_pipeline.py` | 733 | legacy monolith |
| `listener/person_event_pipeline.py` | 1,094 | per-class pipeline |
| `listener/animal_event_pipeline.py` | 273 | per-class pipeline |
| `listener/matcher_adapters.py` | 240 | per-class dispatch |
| `listener/state.py` | TBD | STATE singleton (counters) — only matters for legacy emit |
| `infra/two_call_cascade.py` | TBD | text-9B cascade (§11.117 drops call 1) |
| `infra/classify_prompt.py` | 92 | text-9B classify prompt |
| `infra/classify_validator.py` | 158 | text-9B classify validator |
| Threat-level LLM scaffolding | TBD | §11.117 strips threat-level |

## Build-from-spec

- `listener/pipeline.py::run` — 11-stage linear function (target ~200 lines)
- `listener/listener.py::handle_webhook` — webhook receiver (target ~80 lines)
- `infra/cooldown_key.py` — `(camera_id, classification)` throttling (PipelineCooldown, already done in PR1 of refactor repo at `1fe8986`; I'll rewrite it cleanly here)
- `infra/vision_analyzer.py::verify_class()` — VM1 prompt (vision-8B, no gate hint)
- `infra/vision_analyzer.py::detail_class()` — VM2 prompt (class-specific)
- 3 Telegram stage functions
- `vehicle_matcher/` — keep as-is

## Prompts in v2 (3 prompts, built fresh)

Per O4 confirmation: prompts for people (person), animals (animal),
and adults = "what kind of adult" = vehicle context. Three
class-specific prompts for VM2. Also one VM1 prompt that has no gate
hint and just describes what vision-8B sees in the 2 crops.

## Test surface

Target: **~50-100 tests total, all asserting §11.117 behavior.**
Today's 2,213 tests assert conflicting legacy + new behavior; this is
the reset point. Test count is not the goal — coverage of the
§11.117 design is.
