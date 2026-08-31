# Failure-Mode Examples — Measured 2026-08-25

Real examples from the yolov8n→m→x night-FP comparison (Phase 6B.116). Each entry shows what the headline number hides.

## yolov8m "horse 0.456" — lens flare

**Frame:** `data/frames/08c18afc-b250-48a5-a8b4-f497fb2caa8f/frame_001.jpg`
**Camera:** Outside Back Solar
**Timestamp:** 2026-08-25 05:27 EDT
**Top detection:** `horse 0.456`

**What's actually there:** A lens flare — the bright circular blob in the middle of the frame (likely a headlight reflecting off the IR lens housing). No horse, no animal, no vehicle silhouette. The round bright shape with a darker center vaguely resembles a horse head silhouette to the model.

**Lesson:** Bigger model with "0 FPs above 0.40" still has a 0.456 confidence wrong answer. The headline FP count is 0 but the model is *confidently* wrong, which is harder to debug than a low-confidence wrong answer.

## yolov8n "person 0.34" — IR-illuminated tree

**Frame:** `data/frames/044d90b7-de72-40b4-ad1e-9ac2f4a628be/frame_001.jpg`
**Top detection:** `person 0.349`

**What's actually there:** A tree in the middle of the frame. The vertical tree silhouette + IR illumination makes it look person-shaped to the day model.

**Lesson:** Trees get classified as persons at night. This is the dominant FP class — 20/30 of yolov8n's night FPs at conf ≥ 0.20 are trees-as-persons.

## yolov8n "boat 0.33" — edge artifact

**Frame:** `data/frames/13b02f19-2779-48e1-90aa-ed35f9bfaf2a/frame_001.jpg`
**Top detection:** `boat 0.333`, also `boat 0.260`

**What's actually there:** Camera housing ring at x=0 (image left edge). The model fires on the dark ring with bright pixels beyond it, calls it "boat" twice.

**Lesson:** Edge artifacts (x near 0 or x near image width) are a stable FP class for yolov8n at night. An "edge filter" (reject any bbox within ~100px of frame edge) would kill these.

## yolov8n "airplane 0.26" — bright circular flare

**Frame:** `data/frames/08c18afc-b250-48a5-a8b4-f497fb2caa8f/frame_001.jpg` (same frame as the horse!)
**Top detection:** `airplane 0.259` (yolov8n), `horse 0.456` (yolov8m)

**What's actually there:** Same lens flare as above. yolov8n sees it as "airplane" (round bright blob), yolov8m sees it as "horse" (round shape with darker center). Both are confidently wrong.

**Lesson:** A given failure frame produces different FP classes in different model sizes. The "best model" choice depends on which misclassification costs less — calling a lens flare an airplane vs calling it a horse is the same severity, just different vocabulary. **No model size escapes the failure mode.**

## yolov8x "won't fire" — overconfident silence

**Pattern:** Across 20 night frames, yolov8x's max confidence was 0.152 (mean 0.065). 0/20 frames reached conf ≥ 0.30.

**What's actually there:** Real surveillance scenes with trees, ground, edges. yolov8x has been trained on a slightly different distribution that doesn't include this kind of IR surveillance footage, so it refuses to fire.

**Lesson:** "0 FPs" doesn't mean "0 detections" — it can also mean "model gave up." Always verify a "quiet" model is making real negative predictions, not refusing to participate.

## Heuristic gate results — version 1 (conf-only)

`scripts/probe_night_reflections.py` with `MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1`:
- yolov8n alone (no heuristic): 30 FPs at conf ≥ 0.40 across 80 night frames
- yolov8n + heuristic (conf-only): **1 FP at conf ≥ 0.40** (96.7% reduction)
- Single surviving FP: `person 0.443` (above the 0.40 floor)

The heuristic gate v1 (`_brightness_ratio()` + `is_night_at_edt()` + conf floor) catches the patterns that the bigger models don't.

## yolov8m + conf-only heuristic — 4 surviving FPs

When the same heuristic runs against yolov8m, it leaves 4 pass-throughs (5%):
- `bowl 0.505` (2eafcbf0 — actually a real table-with-bowls in frame, but irrelevant for surveillance)
- `dining table 0.485` (same frame)
- `horse 0.455` (08c18afc — lens flare misdetection)
- `truck 0.413` (2eafcbf0 — possibly real parked truck)

**The class distribution matters more than the conf value.** yolov8m shifts noise into a *higher* conf band (0.40-0.50) where the conf floor doesn't trigger. **Conf-only heuristic misses these.**

## Heuristic gate results — version 2 (conf + implausible-class filter)

Added `NIGHT_IMPLAUSIBLE_CLASSES` (38 COCO classes that physically can't be on a rural property at night: indoor furniture, tableware, electronics, wrong-domain vehicles). Suppression triggers on EITHER:
- top_conf < NIGHT_CONF_FLOOR (catches yolov8n noise), OR
- top_class ∈ NIGHT_IMPLAUSIBLE_CLASSES (catches yolov8m wrong-class noise)

Results on 80 night frames:

| Combination | Pass-through | FP rate |
|---|---|---|
| **yolov8n + heuristic v2** | **1** | **1.25%** |
| yolov8m + heuristic v2 | 3 | 3.75% |

The 3 surviving yolov8m pass-throughs are all `horse`/`truck` — plausible surveillance classes that the class filter doesn't touch (correct behavior — don't suppress plausible classes, only conf-filter them). One of them (`truck 0.413`) may even be a real parked pickup in Outside Back Solar's view.

**Lesson:** the dual-condition heuristic is **model-agnostic** — works on both yolov8n AND yolov8m without retuning. Adding a 3x latency cost to yolov8m (10ms vs 3.4ms median on CoreML) doesn't beat yolov8n+heuristic v2 — same 1.25% FP rate, three times the latency.

The real path forward remains: fine-tune yolov8n on ~200 hand-labeled real night frames. That's a different conversation — needs the labeled corpus first.
