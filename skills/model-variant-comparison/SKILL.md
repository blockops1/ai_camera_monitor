---
name: model-variant-comparison
description: "Swap ML models. Measured: FP, recall, latency, failure mode."
category: mlops/inference
---

# Model Variant Comparison — Measured, Not Vouched

## When to use

Before recommending any ML model swap — whether it's a different size of the same architecture (yolov8n→m→x), a different family (yolov8→yolov11), or a fine-tuned variant. Codified 2026-08-25 from Phase 6B.116 night-model work where yolov8m looked like the obvious upgrade and the actual benchmark showed it was worse for the use case.

**The reflex to resist:** "model X is bigger / newer / has more params, recommend it." Without measured numbers on the actual failure data, this is guessing. Bigger often means: higher conf floor (more conservative, fewer FPs but also fewer real detections), slower latency, or just trained on a slightly different distribution.

**Anti-pattern (the wrong call):** *"yolov8m is bigger, it should be better at night" — would have shipped yolov8m if we hadn't actually tested.* Measured: yolov8m dropped FPs from 30→0 at conf ≥ 0.40, but cost 3x latency (10ms vs 3.4ms median on CoreML) AND missed real positives (1/20 vs 4/20 at conf≥0.30). One of its high-conf hits was a lens flare called "horse 0.456" — bigger model just shifts the failure class, doesn't fix the underlying mode. The real fix is a dual-condition heuristic gate (conf + implausible-class filter), not a model swap.

## The four-measurement pattern

A correct head-to-head has **four measurements**, not just one:

1. **False-positive count** at the production conf floor — the regression that triggered the swap consideration.
2. **True-positive recall** at the same floor — did the new model find real objects the old one missed?
3. **Latency** — production budget is real. yolov8n is ~5ms on CoreML; yolov8m is ~45ms; yolov8x is ~150ms. 9x latency for marginal FP reduction is usually not worth it.
4. **Failure-mode analysis** — when the new model returns a high-conf hit, is it a real detection or a different FP class? Bigger models often shift the failure class (lens-flare→"horse") rather than eliminate FPs.

All four need to be measured on the **same evaluation set** — same frames, same conf floor, same NMS, same postprocess. Comparing across different test sets or thresholds is meaningless.

**Measured latency on Apple Silicon CoreML (50 runs, native 2304×1296 → letterboxed 640×640, the actual production path):**

| Model | Median | Mean | p95 | Per alert (4 frames) |
|---|---|---|---|---|
| yolov8n | 3.4 ms | 3.5 ms | 3.6 ms | 13.6 ms |
| yolov8m | 10.0 ms | 10.0 ms | 10.2 ms | 40.0 ms |
| yolov8x | ~30 ms (estimated) | — | — | ~120 ms |

The 3x latency cost from n→m is **real but irrelevant** when the alert budget is dominated by Qwen3-VL (1-3s per alert) and Telegram sends (~100ms). Don't refuse a model swap on latency alone; combine with FP/recall/failure-mode analysis.

## The shape-mismatch trap

YOLOv8 family has `(1, 84, 8400)` output (84 = 4 bbox + 80 classes). But:
- Single-class models (pyronear fire detection) have `(1, 5, 8400)`
- Segmentation models have different output structure entirely
- Custom-trained models may have N classes != 80

**Always check the output shape with a real inference before assuming two models are comparable.** A `5` in the middle dim means single-class — a "yolov8" model that returns 40,000 detections per frame is not a general COCO model. Patch a `check_output_shape()` helper into the comparison probe.

## The probe template

`scripts/probe_<task>_model_comparison.py`:

```python
MODELS = {
    "<name> (<size>)": str(_ROOT / "models" / "<file>.onnx"),
    ...
}

CONFIDENCE_FLOOR = 0.40  # what production uses

for label, path in MODELS.items():
    session = load_model(path)
    if not check_output_shape(session):
        results[label] = {"skipped": True, ...}
        continue
    # ... run on the evaluation set, accumulate fp_count, by_class, mean_conf
```

Print a single comparison table at the end:

```
=== <Task> Model Comparison ===
Eval set: 80 <frames>
Conf floor: 0.40

Model            FPs    FP/frame   Frames w/det   Mean conf
----------------------------------------------------------------
yolov8n (12MB)   30     0.375      30             0.440
yolov8m (112MB)  0      0.000      0              0.000    ← winner on FP
yolov8x (273MB)  0      0.000      0              0.000

Class breakdown (FP classes):
  yolov8n: person(20), boat(10)
  yolov8m: (none above floor)
  yolov8x: (none above floor)

Recommendation: stay with yolov8n — 9x latency cost for marginal FP reduction,
and the heuristic gate already kills 93% of yolov8n's FPs.
```

The recommendation should land in PLAN.md as a measured decision, not a vibes-based one.

## Latency measurement

Use `time.perf_counter()` around a warm-up + N inferences:

```python
# Warm-up
session.run(...)
# Measure
t0 = time.perf_counter()
for _ in range(N):
    session.run(...)
mean_ms = (time.perf_counter() - t0) * 1000 / N
```

Report `mean_ms` per frame, **not** total time. Also report on the actual production execution provider (CoreML on Apple Silicon, CUDA on Linux, CPU as fallback) — cross-provider latency differs wildly.

## Failure-mode analysis

When a new model returns a high-confidence detection, **look at the image**. Vision-analyze the actual frame — was that really a vehicle/person/animal, or is it a different false-positive class?

Pattern observed: bigger model with fewer FPs but the one FP it has is a high-confidence misclassification (lens flare → "horse 0.456"). The "0 FPs" headline hides the fact that the model is now confidently wrong on a different pattern. **Confusion-matrix-by-image, not by-count.**

## When "bigger" is actually the right call

- The bigger model has lower conf-floor behavior (catches real objects the smaller misses) — verify with TP recall
- The bigger model returns FPs in a class that doesn't matter for the use case
- Latency budget has room (e.g., batch processing, not real-time)
- The failure-mode shift is acceptable (lens-flare→"horse" is the same severity as tree→"person")

## Companion: dual-condition heuristic gate for noisy data

After the model comparison reveals that "no model cleanly solves this," consider a **dual-condition heuristic** that combines:

1. **Conf threshold** — catches low-confidence noise (yolov8n pattern: conf 0.26-0.38 → noise)
2. **Implausible-class filter** — catches high-confidence-but-wrong-class noise (yolov8m pattern: conf 0.40-0.50 but class is `bowl`/`cup`/`dining table`/`train` — physically impossible on a rural property)

Suppress if EITHER condition is true. This is **model-agnostic** — works on any future model variant without retuning the conf floor.

Pattern observed in 2026-08-25 yolov8m+heuristic v2:

| Combination | FPs at conf ≥ 0.40 | FP rate |
|---|---|---|
| yolov8n alone | 30 | 37.5% |
| yolov8n + conf-only heuristic | 1 | 1.25% |
| **yolov8n + conf+class heuristic** | **1** | **1.25%** |
| yolov8m alone | 0 | 0% |
| yolov8m + conf-only heuristic | 4 | 5% |
| **yolov8m + conf+class heuristic** | **3** | **3.75%** |

Why conf+class > conf-only: yolov8m shifts noise into a *higher* conf band (0.40-0.50) where the conf floor doesn't trigger. The class filter catches `bowl`/`cup`/`dining table` regardless of conf. Plausible surveillance classes (`person`, `car`, `truck`, `cat`, `dog`, `horse`, `cow`, `sheep`, `bear`, `bird`) are NEVER filtered by class — only by conf — so real positives survive.

**Implementation pattern** (Phase 6B.116 §11.47):
- `NIGHT_IMPLAUSIBLE_CLASSES = frozenset({...38 classes...})` — physically implausible at night on rural property
- Suppress if `(conf < NIGHT_CONF_FLOOR OR top_class ∈ NIGHT_IMPLAUSIBLE_CLASSES)` AND night-time AND brightness_ratio > NIGHT_BRIGHTNESS_RATIO
- All three thresholds are env vars: `MOTION_GATE_NIGHT_SUPPRESS_ENABLED`, `MOTION_GATE_NIGHT_CONF_FLOOR`, `MOTION_GATE_NIGHT_BRIGHTNESS_RATIO`

See `farm-vision-alert-routing/references/2026-08-25-night-yolo-fp-investigation.md` for the full night-mode investigation that produced this.

## When NOT to use this skill

- **Greenfield model selection** (no incumbent to compare against) — use a benchmark on your target metric instead
- **Fine-tuning experiment comparison** — different question, use training-eval tooling
- **One-off accuracy check** — `model.eval()` on a single frame isn't a comparison

## References

- `scripts/probe_yolo_night_comparison.py` — the worked example from Phase 6B.116 (the yolov8n→m→x night-FP comparison)
- `references/failure_mode_examples.md` — the yolov8m "horse 0.456" lens flare, the yolov8n "tree→person 0.34" tree case, the yolov8x "won't fire on night" case
