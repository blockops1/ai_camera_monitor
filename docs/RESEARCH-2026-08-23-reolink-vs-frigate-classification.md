# RESEARCH-2026-08-23 — Reolink on-camera classification vs Frigate architecture

**Question:** Note's idea (2026-08-23): add a front-end to the listener that does major classification of different motion types and routes to the correct pipeline. The Reolink on-camera classifier isn't accurate enough.

**Approach:** Compare how Reolink's on-camera AI works vs how Frigate (open-source NVR with object detection) handles motion + classification. Use the comparison to decide whether our pipeline needs a new front-end layer, and where it should slot in.

**Sources:** Reolink Smart Detection docs, Frigate motion detection docs, Frigate hardware/detector docs, community forum threads on false positives. URLs inline.

---

## TL;DR

Reolink runs a **single-stage, on-camera, fixed-classifier**. Frigate runs a **two-stage pipeline** (CPU motion + dedicated AI accelerator for object detection). Reolink's classifier is trained for the *common* case; it fails on the cases Reolink didn't train for (headlight flares at night, distant shadows, motion near mask boundaries, small/partial objects). Frigate's architecture is *more accurate* but requires a Coral/Hailo/OpenVINO detector — which we already have a different equivalent of (Qwen3-VL via `infra/vision_analyzer`).

**Implication for our pipeline:** We already have the architecture Frigate pioneered — **a motion front-end followed by an AI classifier** — but ours uses a heavier LLM (Qwen3-VL) instead of a YOLO-class detector. Our pain isn't architecture, it's the *Reolink pre-classification stage* that fires BEFORE our pipeline even sees the frame. A new listener front-end would only help if we discard the Reolink `event` field and route based on our own classifier output.

---

## Reolink Smart Detection — how it works

**Architecture:** Single-stage. Runs on the camera's own SoC/ASIC. The webhook delivers an `event` field that is one of: `motion`, `vehicle`, `person`, `pet`, `animal` (model-dependent). No bbox, no confidence, no frame at the moment of trigger — only the label and a timestamp.

**Mechanism** (per Reolink docs): "based on traditional motion detection or PIR detection but with advanced algorithms and technology, which can tell person, vehicle, pet, or animal, etc. from other objects." The actual model is closed — Reolink doesn't publish architecture details. It's almost certainly a small CNN (MobileNet/YOLO-tiny class) running on the camera's V100/HiSilicon chip. Optimized for the 5–10 classes Reolink markets.

**Tuning controls exposed to the user:**
- Sensitivity (0–50, generic for all events)
- Minimum/maximum object size (filters by pixel area at the model's input resolution)
- Detection zone (polygon mask; reduces area the model considers)
- Alarm delay (e.g., "person must be visible for 2s before alert fires") — fires only after the label is *sustained*

**Known limitations** (from Reolink community + forum threads):
- **Headlights/IR flares at night** → routinely misclassified as `vehicle`. The model can't distinguish headlight glow from a vehicle's silhouette, especially at distance.
- **Shadows** → routinely misclassified as `motion` (or `person` in extreme cases). The motion detection stage fires on luminance change; the classifier stage sees the silhouette.
- **No confidence score** in the webhook payload. The label is binary.
- **Two-wheeled vehicles** (bicycles, motorcycles) explicitly NOT detected — documented limitation.
- **Day/night mode flip** changes the model's input distribution every day at dusk/dawn; users report this as a major source of false positives.
- **Bounding boxes not in the webhook** — we get the label but no `(x, y, w, h)`. So we can't visually verify what Reolink classified.

**What we get on the webhook:**
```json
{
  "event": "vehicle",
  "channel": 0,
  "time": "2026-08-23T03:14:15+0000"
}
```
That's it. No frame, no bbox, no confidence.

---

## Frigate — how it works

**Architecture:** Two-stage pipeline. Documented at `docs.frigate.video/configuration/motion_detection/` and `frigate/hardware/`.

**Stage 1 — Motion detection (CPU):**
- Runs on every frame at low resolution (default 320×160 @ 1 fps).
- Uses OpenCV background subtraction (cv2.threshold + contour_area). The docs explicitly say "Frigate uses motion detection as a first line check to see if there is anything happening in the frame worth checking with object detection."
- Tunable parameters per camera: `threshold` (luminance delta, 1-255, default 30), `contour_area` (min pixels for a motion blob, default 10), `lightning_threshold` (treat large-area changes as recalibration, not motion).
- Output: motion boxes (red rectangles in debug view). No labels — just "this region changed enough to be worth a closer look."
- Tunable motion masks per camera to exclude known-bad regions (trees, busy roads, timestamps).

**Stage 2 — Object detection (AI accelerator):**
- Only runs on regions flagged by Stage 1.
- Runs on a dedicated detector (Google Coral EdgeTPU, Hailo-8, Apple Silicon NPU, Nvidia GPU, Intel OpenVINO). The detector runs a YOLO-family model (ssdlite/mobilenet on Coral, yolov8/yolov9 on Hailo/OpenVINO/Apple Silicon).
- Output: bounding boxes WITH confidence scores, and a class label from a fixed COCO subset (person, car, truck, bicycle, dog, cat, etc.).
- Frigate+ (paid) ships higher-accuracy custom models trained on user-submitted clips.

**Key architectural insight** (Frigate docs): "To balance it, Frigate uses the CPU to look for movement, then sends those frames to the Coral to do object detection." This is the opposite of Reolink: Frigate does cheap broad motion detection, then expensive targeted classification. Reolink tries to do both at once on a single low-power chip.

**Why Frigate handles false positives better:**
- Motion is just a trigger ("is there *something* to look at?"), not a label. A headlight flare triggers motion but the detector says "no person/car" with high confidence → no alert.
- Day/night tuning is per-camera config (user can run different `motion` settings for day vs night via Home Assistant automation).
- Motion masks let you exclude known false-positive regions entirely.
- Detector confidence threshold (default ~0.5, configurable) lets you reject low-confidence predictions.
- User can replace the model entirely (Frigate+).

**Frigate limitations vs us:**
- Requires dedicated hardware (Coral/Hailo/GPU costs ~$60–$200).
- Detector only knows COCO classes — can't tell "Tesla Model Y" vs "Toyota Camry" without a custom model.
- No scene-level reasoning ("is this person a threat?") without an LLM enrichment stage.

---

## Comparison table

| Dimension | Reolink (our current) | Frigate | Our pipeline |
|---|---|---|---|
| **Motion detection** | Camera-side, integrated into classifier | CPU-side, separate stage | `infra/motion_detector.py` (OpenCV, on server) |
| **Classifier** | On-camera CNN, fixed classes, no confidence | Off-board YOLO on Coral/Hailo, bbox + confidence | Qwen3-VL via `infra/vision_analyzer.py` (server-side LLM) |
| **Latency** | <1s (camera-side) | ~50–500ms motion + ~10–50ms detection | ~2–5s (capture + Qwen inference) |
| **Class granularity** | 5 fixed classes (motion/person/vehicle/pet/animal) | ~80 COCO classes (person/car/truck/dog/etc.) | Open-ended (Qwen returns whatever it sees; we constrain via prompt) |
| **Confidence score** | No | Yes (per detection) | Yes (Qwen returns `confidence: 0.0–1.0`) |
| **Bbox** | Not exposed | Yes (per detection) | Yes (`bboxes` in Qwen response) |
| **False positive handling** | Sensitivity + zone + min/max size; limited | Motion mask + contour threshold + confidence threshold + model swap | Vision prompt + threat-level LLM (`infra.alert_generator`) |
| **Failure mode at night** | Headlight flare → vehicle; shadows → motion/person | Detector sees "no person/car" → no alert | Qwen prompt explicitly asks about flare/shadow; usually correct but slower |
| **Custom model swap** | No (firmware-locked) | Yes (Frigate+ or community models) | No (Qwen is the model) |
| **Hardware needed** | None (in-camera) | Coral/Hailo/GPU ~$60-200 | llama-server on Mac mini (already have) |

---

## What does this mean for the listener front-end idea?

**Architecture match:** Our pipeline is structurally Frigate-style already — cheap server-side motion → expensive AI classifier. We don't need a new layer to *imitate* Frigate; we already have one.

**What we DO need** is a way to handle the Reolink `event` field as untrusted input. Today the listener:
1. Receives webhook with `event: vehicle` (from Reolink).
2. Routes to `process_alert()` → `_process_alert()` → `vehicle_event_pipeline.py`.
3. Reolink's label determines which pipeline runs, but our own motion + Qwen then re-verify.

The pain point Note described: Reolink misclassifies (headlight flare → `event: vehicle`), and our pipeline then runs the *expensive* vehicle path on a headlight flare, even though our own Qwen would correctly say "no vehicle." That's wasted compute + Telegram noise.

**Two architectural options:**

### Option A — Listener front-end (Note's idea)

Add a `infra/motion_classifier.py` (or `listener/motion_classifier.py`) that runs a fast heuristic check BEFORE `process_alert()`. Could be:

- **Pure heuristics** (no new vision call): use `infra/motion_detector.py` outputs (motion area, bbox aspect ratio, time-of-day IR/visible flag from camera config, motion duration) to score "looks like vehicle" vs "looks like person" vs "looks like noise." Fast, free, but limited accuracy.
- **New low-cost vision pass**: a small YOLO-style detector running on llama-server (we'd need a new model) or on the Qwen GPU with a much shorter prompt. Faster than full Qwen vehicle prompt, slower than heuristics.

**Pros:** catches obvious Reolink misclassifications before the heavy pipeline. Cuts noise. Cheap if heuristic-only.
**Cons:** adds latency (~50-200ms heuristic, ~1-2s small-vision). Doesn't solve the case where Reolink correctly labels `motion` but we want `vehicle` — still need to run the heavy classifier. New module to maintain.

### Option B — Suppress at the pipeline exit (current direction)

Let the pipeline run. After Qwen identifies "actually a headlight flare, not a vehicle," suppress the Telegram. Add an override rule similar to `infra/alert_overrides_offhours.py` for "vision-says-no, Reolink-says-yes" mismatch.

**Pros:** zero new modules. Reuses existing alert pipeline. Already partly implemented (we have vision vs Reolink disagreement logger at `listener/listener.py:3543` per §11.30).
**Cons:** still pays the compute cost on every misclassified alert. The Telegram is suppressed, but the work happened.

### Option C — Hybrid (recommended for first pass)

Keep the Reolink `event` field for routing (cheap), but add a heuristic front-end (`infra/motion_classifier.py`) that runs **before** Qwen is called. The heuristic uses:
- Motion area + duration from `infra/motion_detector.py`
- Time-of-day (night = lower threshold for vehicle)
- Per-camera historical FP rate (config-driven)

If heuristic score is "definitely noise" → drop the alert silently. If "ambiguous" → proceed to Qwen. If "confident" (e.g., large slow motion at midday) → proceed to Qwen with a hinted prompt ("this looks like a vehicle, verify").

**Pros:** cheap heuristic filter catches the obvious cases; the heavier Qwen call still runs when needed. The Qwen call's output is the source of truth (not Reolink). Config-driven per camera so we can tune without redeploys.
**Cons:** heuristic accuracy is the limit. Adds a module.

---

## Compute hierarchy — Note's question 2026-08-23

Note asked: "is our `motion_detector.py` the lowest-compute way of doing motion classification, or could we run something even cheaper on every RTSP frame?"

**Current `infra/motion_detector.py` compute profile (verified 2026-08-23):**

- Frame size: 1280×960 grayscale (`RESIZE_W=1280, RESIZE_H=960` at `infra/motion_detector.py:69-70`)
- Algorithm: pairwise `cv2.absdiff` + `cv2.threshold(MOTION_THRESHOLD=25)` per consecutive frame, then connected components for bbox extraction
- Trigger: runs on 6 captured frames per webhook (NOT every RTSP frame)
- Per-frame cost: ~5-10 ms CPU on M-series Mac mini
- It does NOT classify (no person/vehicle/animal). Only detects motion + bbox.

**Compute hierarchy (lowest → highest per frame):**

| # | Method | Cost/frame | What it tells you |
|---|---|---|---|
| 1 | Camera-side pixel alarm (Reolink's built-in motion detection) | **0 server CPU** | "something moved" → fires webhook with `event` field |
| 2 | Frame diff at 160×120 grayscale on server | **~0.3-0.5 ms CPU** | "something moved, here's where" (bbox only, no class) |
| 3 | Frame diff at 320×240 grayscale | **~1 ms CPU** | Same as #2, better small-object sensitivity |
| 4 | MOG2 background subtractor at 160×120 | **~2-3 ms CPU** | Same as #2, adapts to gradual lighting changes |
| 5 | Frame diff at 1280×960 (what we do today) | **~5-10 ms CPU** | Same as #2, full-res |
| 6 | Small CNN (MobileNetV2-INT8 / YOLO-NAS-P) at 160×120 | **~5-15 ms CPU / ~2 ms GPU** | Adds class labels (person/car/animal) |
| 7 | Qwen3-VL (what we run today after Reolink fires) | **~1-3 s GPU per CALL** | Full scene reasoning + bbox + class + confidence |

We are at tier #5. To go lower we have three real options:

**Option 1 — Server-side per-frame diff (tier #2):** run `cv2.absdiff` at 160×120 grayscale on every frame from `persistent_rtsp.py`. At 5 fps × 6 cameras = ~2.5 ms/sec/camera = ~15 ms/sec total = **~0.015% CPU on a Mac mini**. Essentially free. Downside: tells you motion is happening, not what it is. We lose Reolink's `event` classification entirely.

**Option 2 — Small CNN per-frame (tier #6):** tiny classifier (MobileNetV2-INT8, ~3MB) on every frame at 160×120. ~2 ms GPU/frame = ~60 ms/sec total for 6 cameras @ 5fps. Still cheap. Gives us class labels at the frame level. Downside: needs new model hosting, training, has its own FP sources.

**Option 3 — Trust Reolink more (tier #1):** per-camera tuning of Reolink sensitivity, minimum/max object size, alarm delay. Reolink's ASIC does the work; zero server CPU. Downside: limited to 5 classes, no confidence score, no bbox in webhook.

**Architectural mapping back to the listener front-end idea:**

- "Trigger our pipeline before Reolink does" → Option 1 (server-side per-frame diff)
- "Pre-filter before Qwen" → Option 2 (small CNN per-frame, only call Qwen when CNN says class+confidence)
- "Override Reolink's label" → heuristic at webhook time using existing `motion_detector.py` outputs (research doc Option C)

**Recommendation:** Tier #2/#3 (cheap triggers) + tier #7 (Qwen) is the correct Frigate-style architecture. The lowest-compute path to the *specific* problem Note described (Reolink misclassifies) is **Option 3 — better Reolink config** (free) or **the existing pipeline's own disagreement override** (already partly shipped, see `listener/listener.py:3543` vision-vs-Reolink disagreement logger from §11.30).

If the goal is "always-on frame-level motion detection independent of Reolink," Option 1 (~0.015% CPU) is the answer. Add it as a parallel motion channel that fires the webhook → pipeline when *our* diff detects motion, regardless of whether Reolink fires.

---

## Note's refined idea (2026-08-23, second pass)

After the compute hierarchy discussion, Note clarified the goal:

> "we can rely on the camera for motion detection, and listen to its classification, but that we also figure out a low cost way of peeling off two frames from the rtsp when there is a motion alert to determine where the motion is in the images, and running a low-cost low compute classifier on the location of the motion in the image as the first step of the listener."

**Translation:** Keep Reolink as the trigger (its pixel alarm + classification). When Reolink fires the webhook, peel off 2 frames from the persistent RTSP buffer, find the motion region, then run a SMALL CNN on just that crop — *before* invoking Qwen. Use the small CNN's class label as a fast first-pass classifier.

**Mapping to our current pipeline (verified 2026-08-23):**

What we do today:
1. Reolink fires webhook → `capture_stage()` reads 6 frames at 2s intervals from RTSP (`listener/vehicle_event_pipeline.py:238-268`)
2. `identify_stage()` runs `infra/motion_detector.py` on those 6 frames at 1280×960 grayscale → bbox + trajectory (~50ms total)
3. Same `identify_stage()` then invokes `infra.vision_analyzer.analyze_frames_queued()` with Qwen3-VL on the 3 motion crops → class + scene reasoning (~1-3s on GPU)
4. If Qwen identifies a vehicle → `_send_arriving_message()` → matcher → emit Telegram

What Note is proposing (delta from current):
1. Same Reolink trigger ✓
2. **Read 2 frames instead of 6** (cheap — saves 4 frame reads + ~30ms of motion-detection compute)
3. **Run motion detection at MUCH lower resolution** (160×120 instead of 1280×960 — saves ~95% of motion-detector compute, drops ~5ms → ~0.5ms)
4. **Run a small CNN on the motion crop** (NEW STEP — adds ~2ms GPU per frame) → fast class label
5. Use small-CNN label to decide:
   - If `vehicle/person/animal` with high confidence → proceed to Qwen with a hinted prompt
   - If `shadow/flare/noise` with high confidence → suppress the alert (drop before Qwen runs)
   - If low confidence → proceed to Qwen unchanged

**Compute delta vs current:**

| Step | Today | Proposed | Savings/Cost |
|---|---|---|---|
| Frame reads from RTSP | 6 frames @ 3840×2160 (~3-5s capture) | 2 frames @ 3840×2160 (~1-1.5s capture) | ~3s saved on capture |
| Motion detection compute | 6 frames @ 1280×960 (~50ms) | 2 frames @ 160×120 (~1ms) | ~49ms saved |
| Small CNN on motion crop | not done | 2 frames × ~2ms GPU | ~4ms added |
| Qwen call | always fires | fires only when small-CNN is confident OR low-confidence | saves 1-3s on suppressed alerts (most FPs) |
| **Net cost on a normal alert** | ~5s end-to-end | ~3s end-to-end | ~40% faster |
| **Net cost on a noise/shadow event** | ~5s end-to-end | ~1.5s end-to-end (no Qwen) | ~70% faster + no Qwen $ |

**The small-CNN choice — key architectural decision:**

We need a small CNN that runs at ~2ms/frame on GPU (or ~5-15ms on CPU) and returns class + confidence for ~5 categories: `person`, `vehicle`, `animal`, `shadow/flare`, `noise`. Realistic options:

- **MobileNetV3-Small INT8** (~2MB) trained on COCO + custom shadow/flare class. Fast, mature, easy to fine-tune. ~2ms GPU.
- **YOLO-NAS-P** (smaller variant, ~10MB). ~5ms GPU. More accurate, more setup.
- **EfficientNet-B0 INT8** (~5MB). Middle ground. ~3ms GPU.
- **Reolink's own classifier API** — Reolink cameras expose the classification + bbox via the ONVIF/RTSP metadata stream in some firmware versions. If our cameras expose this, we get the small-CNN step for free (Reolink's ASIC did the work). **Needs investigation: do our 6 cameras expose bbox + class via ONVIF/RTSP metadata?**

**Recommendation for first pass:**

1. **Verify whether Reolink cameras expose bbox + class via ONVIF/RTSP metadata** (free, on-camera). If yes, we don't need a small CNN at all — we already get the answer from the camera, just in a different stream.
2. **If not exposed:** start with **MobileNetV3-Small INT8** (smallest, fastest, easiest to host on llama-server alongside Qwen). Train a quick 5-class classifier on our existing `data/frames/<alert_id>/` corpus (we have hundreds of labeled examples from the past month's alerts).
3. **Slot it in as `identify_stage` step 2**: after `motion_detector` finds the bbox, run small-CNN on the crop, use its label to decide whether to invoke Qwen or suppress.

**Code sketch (where it slots in):**

```python
# listener/vehicle_event_pipeline.py — identify_stage (existing)

# Phase.NEW: peek 2 frames from persistent RTSP instead of 6 (cheaper)
ctx.frame_paths = peek_frames(ctx.rtsp_url, count=2)  # NEW: 2 frames, not 6

# Phase.NEW: motion detection at 160×120 (cheaper)
motion_result = detect_motion_at_low_res(ctx.frame_paths)  # 160×120 instead of 1280×960

# Phase.NEW: small CNN classification on motion crop (NEW STEP)
crop_path = crop_top_n(motion_result)[0]  # extract top motion bbox
quick_label = small_cnn.classify(crop_path)  # ~2ms GPU
# quick_label = ("vehicle", 0.92) | ("shadow", 0.88) | ("unknown", 0.45)

# Phase.NEW: confidence gate
if quick_label.class == "shadow" and quick_label.confidence > 0.85:
    log.info(f"[{ctx.alert_id}] suppressed by small-CNN: {quick_label}")
    return  # skip Qwen, no Telegram
elif quick_label.confidence > 0.80:
    # High-confidence classification — pass hint to Qwen prompt
    ctx.vision_hint = quick_label
    # fall through to Qwen

# Qwen only runs on uncertain or non-noise cases
vision_result = analyze_frames_queued(ctx.frame_paths, hint=ctx.vision_hint)
```

**Open questions for Note:**

1. Do our 6 Reolink cameras expose bbox + class via ONVIF/RTSP metadata? (Decides whether we need to host a small CNN at all.)
2. What training data do we have for the small CNN? (Existing `data/frames/<alert_id>/` corpus, but how many labeled shadow/flare cases?)
3. What's the acceptable false-negative rate on the small-CNN gate? (Suppressing a real vehicle is much worse than letting a shadow through to Qwen.)
4. Where does the small CNN run? llama-server GPU? Separate process? (Mac mini has shared GPU with Qwen — contention risk.)

---

## Next step (proposed)

Before building, **Note's 2026-08-22 preference** says investigate before asking. Specifically:

1. Pull last 30 days of `data/audit/*.jsonl` and categorize alerts by `(camera, Reolink_event, Qwen_classification)` triples. Where are the false positives concentrated? Which cameras? Which event types?
2. Pull a sample of false-positive frames from `data/frames/<alert_id>/` and look at them. Are they headlight flares? Shadows? Time-of-day patterns?
3. Check `infra/motion_detector.py` outputs for the false-positive cases — would heuristics have caught them?
4. Check whether Reolink cameras expose bbox + class via ONVIF/RTSP metadata. (If yes, no small CNN needed.)
5. Decide: which model (MobileNetV3-Small / YOLO-NAS-P / etc.) and what training data. Write PLAN §11.NN per AGENTS.md "PLAN.md phase first for enrichment work."

This becomes the first concrete step on the task. No code change yet — gather evidence first.

---

## Pretrained model availability — Note's question 2026-08-23 (third pass)

Note asked: "Does the CNN not come already trained? If not, are there open source training corpus that are available for download?"

**Short answer: YES, pretrained models come out of the box for the easy classes (person/car/animal/truck/bicycle/etc.). COCO is the standard. NO, pretrained models don't exist for the false-positive classes we care about most (shadow, headlight flare) — those need labeled data.**

### Pretrained models for "real object" classes

These ship pretrained on COCO (or ImageNet + COCO) and give us `person / car / truck / bicycle / dog / cat / horse / sheep / cow / bird` etc. out of the box:

| Model | Source | Pretrained on | Inference speed | Notes |
|---|---|---|---|---|
| `torchvision.models.detection.ssdlite320_mobilenet_v3_large(pretrained=True)` | PyTorch torchvision | COCO 2017 (80 classes incl. person, car, truck, bicycle, dog, cat, horse, sheep, cow) | ~5-10 ms CPU, ~2 ms GPU | The MobileNetV3 backbone Note asked about. Drop-in, no training. |
| `torchvision.models.detection.fasterrcnn_mobilenet_v3_large_fpn(pretrained=True)` | PyTorch torchvision | COCO 2017 | ~30-50 ms CPU, ~10 ms GPU | More accurate than SSDLite, slower. |
| YOLOv8n / YOLOv8s (Ultralytics) | github.com/ultralytics/ultralytics | COCO 2017 (80 classes) | ~5 ms GPU (v8n) / ~10 ms GPU (v8s) | Industry standard. Pretrained weights downloadable. |
| YOLOv9t / YOLOv9s | github.com/WongKinYiu/yolov9 | COCO 2017 | ~5-10 ms GPU | Newer, better accuracy/efficiency than v8 at same size. |
| EfficientNet-B0 + SSD head (TF/PyTorch) | Various | COCO 2017 | ~10 ms GPU | |

**What this means for our pipeline:** The "vehicle / person / animal" part of the classifier comes FREE. `ssdlite320_mobilenet_v3_large(pretrained=True)` is ~5MB, runs at ~2ms on GPU, and already detects `person (label 1), bicycle (2), car (3), motorcycle (4), bus (6), train (7), truck (8), cat (16), dog (17), horse (19), sheep (20), cow (21), bear (24)` etc. with bounding boxes + confidence.

We can run it today without any training. That gives us 80% of what we wanted.

### Pretrained models for "false positive" classes — NOT available

We DO NOT get pretrained models for:

- **Shadow** — no public dataset has outdoor-camera shadows labeled as a class. COCO has no `shadow` category. Roboflow Universe has a few small (~1-5K images) community datasets but they're not standardized and are mostly indoor/selfie shadows, not outdoor surveillance.
- **Headlight flare** — Flare7K dataset exists (Google Drive, ~7K images of nighttime lens flare) but it's for **flare REMOVAL** (image enhancement), not flare DETECTION as a class. Reolink's misclassification isn't "is this a flare pixel?" — it's "is this a flare that LOOKS like a vehicle?" Different problem.
- **Insect on lens / spider web / raindrop / IR bounce** — no public datasets.
- **Time-of-day shadow (long ground shadow that moves like a person)** — no public datasets.

**The honest answer:** for the false-positive categories, we'd need to build our own training corpus from our own cameras. That's the hard part.

### Options for the FP-class training data

1. **Use our own `data/frames/<alert_id>/` corpus + label manually.**
   - We have hundreds of labeled examples from past alerts (Qwen already told us what they were). The "shadow / flare" cases are in there but mixed with the real alerts.
   - Cost: 2-4 hours to label ~500-1000 crops. Manual but straightforward.
   - Quality: HIGH — these are the actual failure modes on our cameras, in our conditions.

2. **Use a public night-driving dataset + add our own negatives.**
   - Nighttime Driving Images Dataset (Kaggle, ~1.5K images) has headlights + vehicles.
   - ntnu-arl/vehicles-nighttime (GitHub) has labeled night vehicle classifications.
   - aasharma90/NightTime_Datasets (GitHub list) is a meta-resource with ~40 datasets.
   - Cost: 4-8 hours to combine + label for our 5-class schema. Quality MEDIUM — useful for vehicle/flare distinction but not for surveillance-specific noise.

3. **Use a synthetic data approach.**
   - Render shadows + flares onto our existing frames with Blender/Pillow.
   - Generate hundreds of "false positive" examples without needing real ones.
   - Quality MEDIUM — synthetic ≠ real, but it's cheap.

4. **Skip the FP-class training entirely. Use a different strategy.**
   - Don't try to train a "shadow detector." Instead, train an anomaly detector: "this is NOT a normal person/car/animal, suppress."
   - Use the pretrained COCO model's confidence as the gate: if the highest COCO class is <0.5 confidence → likely noise/flare → suppress.
   - Cost: 0 hours. Uses the pretrained model alone.
   - Quality: LOW-MEDIUM — won't catch shadow/flare specifically, but does catch weird stuff the COCO model wasn't trained on (which is most false positives).

### Recommendation

**First pass: option 4 (use COCO confidence threshold alone, no FP training).** This gives us a working classifier in an afternoon:

```python
# Pseudocode
model = torchvision.models.detection.ssdlite320_mobilenet_v3_large(pretrained=True)
predictions = model(crop_tensor)
# predictions[0]['scores'] = [0.92, 0.31, 0.08, ...]  # descending
# predictions[0]['labels'] = [3, 1, 17, ...]          # car, person, dog, ...

top_score = predictions[0]['scores'][0].item()
top_class = COCO_INSTANCE_CATEGORY_NAMES[predictions[0]['labels'][0].item()]

if top_score < 0.5:
    # Pretrained model doesn't see a clear person/car/animal
    # → likely shadow / flare / noise → suppress
    return SUPPRESS
elif top_score >= 0.5 and top_class in {'person', 'car', 'truck', 'bicycle', 'dog', 'cat', 'horse', 'sheep', 'cow'}:
    # Confident real object → fall through to Qwen with hint
    return PASS_WITH_HINT(top_class)
else:
    # Low-confidence but matched something — let Qwen decide
    return PASS
```

**Second pass (if option 4 doesn't catch enough FPs):** add option 1 (label 500-1000 of our own FP crops, fine-tune the SSD head for 1 epoch). ~3 hours of labeling + ~30 min of training.

### What's actually possible in a single sitting

If we want to ship something today:

1. `pip install torch torchvision` (already have these in our venv?)
2. `model = ssdlite320_mobilenet_v3_large(pretrained=True)` — one line
3. Write the gate logic (option 4 above) — ~30 lines
4. Slot it into `identify_stage` between `motion_detector` and `analyze_frames_queued`
5. Run probe with synthetic webhook → verify gate fires correctly on a few known cases

Cost: ~2-3 hours of dev work, zero training data needed. We get a meaningful first-pass classifier immediately.

The more sophisticated "fine-tune for shadow/flare" version is a follow-up that requires real labeled data and is at least a day's work.


---

## Status — 2026-08-23 (architecture locked, see PLAN §11.37)

The research findings above informed the LOCKED architecture in **PLAN §11.37** (`git log --oneline | grep 1928bcc`).

**Key resolved questions:**
- Threshold strategy: per-class + per-camera (was open in this doc)
- Routing decision: vehicle wins on ambiguity (Q2/Q3 locked)
- Where the gate lives: NEW module `listener/motion_gate_pipeline.py` (was open)
- Orchestration: listener.py → motion_gate → route (was open)
- Frame pair math: Option C1 (two diffs, each bbox from its LATER frame) — confirmed

**Module shipped 2026-08-23:** `infra/quick_classifier.py` (commit `c26992f`) — YOLOv8n ONNX wrapper with CoreML EP, no torch install needed. Replaced the torch-based strategy outlined above.

**Pipeline change status:** Both `vehicle_event_pipeline.py` and `person_event_pipeline.py` are UNCHANGED in the §11.37 phase. Their `capture_stage` becomes redundant (will be removed in a follow-up phase after week 1 of production data).

**Next file work for §11.37:** `listener/motion_gate_pipeline.py`, `infra/frame_diff.py`, `config/motion_gate_thresholds.json`. No code yet — awaiting Note's approval to start implementation.
