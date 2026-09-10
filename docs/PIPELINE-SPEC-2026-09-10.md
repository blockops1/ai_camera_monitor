# Canonical Pipeline Spec (operator-locked, 2026-09-10)

## Order of operations

For every webhook the listener receives, this is the ONLY valid order:

1. **Webhook arrives** at `POST /alert` (port 8090).
2. **Four images** are captured from the camera's per-camera RTSP ring memory and saved as `frame_001.png` through `frame_004.png` under `data/frames/<camera_id>/<alert_id>/`.
3. **ONE TIME — image manipulation**: produce `crop_a.png`, `crop_b.png`, and `pairwise_diff.png` from those four frames, all under `data/frames/<camera_id>/<alert_id>/`. **No other image manipulation happens for this webhook.**
4. **YOLO gate** classifies `crop_a` / `crop_b` and returns one of `vehicle`, `person`, `animal`. If gate says "suppress" → drop, do NOT proceed.
5. **Cooldown check**: if `(camera_id, classification)` is within cooldown window → drop, do NOT proceed.
6. **VM1** (vision model one) verifies the class is `vehicle` / `person` / `animal`.
7. **TG#1 fires**: caption = `Camera: <label>` + `Detected: <class>` + `Confidence: <conf>` + the four per-frame bbox positions. Attachments: `frame_004.png` (one of the four full images) + `pairwise_diff.png`.
8. **VM2** is called with a **mode-specific prompt** based on the class:
   - `vehicle` → license plate, distinctive features, color, type
   - `person` → clothing, height, identity markers
   - `animal` → species, color, markings
9. **TG#2 fires**: caption = `Camera: <label>` + `Mode: <mode>` + `Class confirmed: ...` + mode-specific fields. Attachments: `crop_a.png` + `crop_b.png`.
10. **Match** against known entities (per-class matchers, not just vehicles).
11. **TG#3 fires**: caption = `Camera: <label>` + `Match: <matched | no_match>` + matched identity name (if any). Attachments: none.
12. **Cooldown counter starts** ONLY after TG#3 successfully dispatches.

## Constraints

- **No resize, no compress, no re-encode** anywhere. PNG native. `optimize=True` in PIL save is **FORBIDDEN** (it triggers a re-encode).
- **Cooldown** is keyed by `(camera_id, classification)`. `should_suppress` is called AFTER the gate (item 5), not before. `record_hit` is called AT THE END (item 12), not in the middle.
- **Single image-manipulation point** = item 3 above. Three artifacts produced (`crop_a`, `crop_b`, `pairwise_diff`). All under `data/frames/<camera_id>/<alert_id>/`. No `/tmp` writes. No code outside this orchestrator may save an image.
- **TG#1 attachments count = 2** (one full frame + pairwise_diff), not 4 or 6.
- **TG#2 attachments count = 2** (crop_a + crop_b).
- **TG#3 attachments count = 0**.
- **YOLO gate** must bucket into `{vehicle, person, animal}` — not raw 80-class. The bucket mapping is fixed:
  - `vehicle` ← {car, truck, bus, motorcycle, bicycle, train, boat}
  - `person` ← {person}
  - `animal` ← {cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe, ...}

## Mapping to PRDs

| Spec rule | PRD scope |
|---|---|
| R1, R2 (single manip point, canonical paths) | **V2-024** (rewrite) + **V2-021** (path canonicalization) |
| R3 (gate bucketing) | **V2-024** |
| R4 (cooldown position: after gate) | **V2-024** |
| R6 (TG#1 photo count + positions in caption) | **V2-024** |
| R9 (per-class matchers, not just vehicle) | **V2-024** |
| R11 (cooldown record_hit at end) | **V2-024** |
| R5, R7, R8, R10 (already correct, verify only) | verification only, no new cards |
