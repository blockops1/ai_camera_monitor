# PHASE-V2-031 PRD — Single bbox, 10%-per-side rounded-to-multiple-of-32 padding, viz = crop

**Author:** the operator
**Status:** active — final implementation applied via b6388ac
**Phase:** v2-pipeline-rearchitecture
**Plan section:** v2-single-image-manipulation

## Rescoping note (2026-09-13, applied commit b6388ac)

The V2-031a1 implementation (`ea12b6b`, merged `6b1143e`) applied the 10%
+ round-mult-32 padding to **both** `diff_pair_with_bbox` (raw diff
bbox, "green box" path) **and** `subject_bbox_from_two_masks` (logical
AND bbox, "yellow box" path).

On 2026-09-13 the operator observed via Telegram-attached composites
that the green box (drawn from `bbox_a/b`) and the cropped region
(derived from `subject_bbox_a/b`) had **different sizes** for the same
event — root cause: two different bbox sources for two different
purposes, both still 10%-padded. The operator's verbatim:

  *The yellow boxes are bad, the green boxes are good. I have told you
   many times i only want boxes created once. I only want the green
   boxes used.*

  *Yes. And I want it actually removed out of the application.*

The fix in commit **b6388ac** (V2-031a2):

  - DELETE `subject_bbox_from_two_masks()` from `infra/frame_diff.py`
  - DELETE `subject_bbox_from_mask()` (same AND concept)
  - DELETE `_pick_best_cc()` (only used by the removed function)
  - DELETE `subject_bbox_a/b` fields from `GateVerdict`
  - DELETE the AND-mask computation block in `infra/gate.py:924-1020`
    (subject_bbox fallback logic, walk-by carve-out, no_subject_detected
    suppression path — all gone)
  - DELETE `tests/test_frame_diff.py` (tests for the removed function)
  - KEEP `_expand_bbox_pct()` and `DEFAULT_BBOX_PAD_PCT = 0.10` —
    the operator's original 10% + round-mult-32 design rule, still in
    force, now applied only to the diff bbox path (the kept green-box
    path)

**Net:** crop_a = bbox_a (10% pad, mult-32), crop_b = bbox_b. The bbox
drawn on the composite is the exact region sent to Qwen. No separate
"subject" bbox, no AND intersection, no fallback. One bbox per slot.

## Background

On 2026-09-13, the operator identified two coupled bugs in the v2 pipeline:

1. **Box/crop disconnect on Telegram alerts.** The green box drawn on `pairwise_diff.png` and `composite.png` does not match the regions actually cropped into `crop_a.png` / `crop_b.png`. Root cause: two different bboxes from one diff step. `bbox_a` / `bbox_b` (drawn on viz) come from `infra/frame_diff.diff_pair_with_bbox()` → `bbox_from_mask(padding_px=16)`. `subject_bbox_a` / `subject_bbox_b` (used for crops) come from `infra/frame_diff.subject_bbox_from_two_masks(padding_px=8)`. Different masks (raw diff trail vs logical AND of two consecutive diffs) AND different padding constants (16px vs 8px) → visible offset between viz box and crop.

2. **Padding amount is fixed-pixel (16px), scaled to nothing at native 2304×1296 resolution.** Real median bbox from `logs/daemon.log` (1272 samples) is 420×614; 16px padding adds +1.9% width / +1.3% height — negligible. The padding does not meaningfully help YOLO at the median scale, but a fixed-pixel value is wrong by construction for small bboxes (distant subjects get under-padded, close subjects get over-padded relative to bbox area).

3. **YOLO's internal pad-to-multiple-of-32 currently adds black bars** (zero-pad, `constant_values=0`). After the unified-bbox + percentage-pad change, those black bars disappear because the bbox dimensions become multiples of 32 by construction. The operator confirmed 2026-09-13: *"The way that YOLO does the padding already is fine. We don't need to change that"* — meaning YOLO's `pad_to_multiple_of_32` mechanism stays, but the new bbox dimensions ensure it's a no-op.

## Original design (operator's words, 2026-09-13)

> *"Crops should only be created at one time. After the Webhook comes in and after the four images are created from the RTSP stream crop a and crop B are created. That's also the time that the pairwise differential composite should be created. That's the only time for that webhook that any image manipulation should be done. Images should not be resized or compressed at any point in the application."*

> *"I don't want Yolo to create another set of boxes and crops. That is not helpful, it wastes processing resources and it produces bad results."*

> (On padding) *"By providing context by doing a padding of the differential pairwise image, it allows Yolo to have contrast to what it sees. If that's the case, what's the optimal padding that we should be using? Should it be more than 16 pixels?"*

> *"Percentage based padding of 20%, and then that is used to define the box, to generate the crop, and then those crops are padded with white space to enable yolo, and the crops are sent in the telegram as part of the alert. That's what I want you to do."*

> (Operator correction, 2026-09-13 out-of-band:) *"oh I meant 20% overall, that means 10% on each side"* — so the per-side constant is `0.10`, not `0.20`. The total bbox expansion is 20% in each dimension.

> (Operator correction, 2026-09-13:) *"You're going too fast here. I did not say that the padding had to be white."* — the white-pad-to-square intermediate design is rejected.

> (Operator refinement, 2026-09-13:) *"The way that YOLO does the padding already is fine. We don't need to change that"* AND *"when the 10% padding is applied to the B box, you can round it up so that the horizontal and vertical is a multiple of 32"* — the canonical bbox is percentage-padded AND rounded UP to the nearest multiple of 32. This single change makes YOLO's existing `pad_to_multiple_of_32` a no-op without touching YOLO code.

## Design — single image-manipulation step, no second padding

**One step, one bbox, three uses:**

1. **Diff step** (`infra/frame_diff.py`): produce a single canonical bbox per frame from `subject_bbox_from_two_masks` with **`pad_pct = 0.10`** (10% per side = 20% overall per dimension), then **round each dimension UP to the nearest multiple of 32**, then clamp to image bounds (re-center on original bbox center, then clamp x/y so the bbox stays in-frame). The resulting bbox is the canonical region used for everything downstream.

2. **Crop generation** (same diff step): PIL crop of the frame at the canonical bbox. The crop is **rectangular** (no white-pad-to-square). The crop dimensions are already multiples of 32, so YOLO's `pad_to_multiple_of_32` is a no-op. The crop saved to disk as `crop_a.png` / `crop_b.png` and sent to Telegram is this exact PIL image — no resize, no compression, no second padding.

3. **YOLO input**: receives the rectangular crop. YOLO's existing `pad_to_multiple_of_32` runs but adds zero pixels (dimensions are already multiples of 32). No black bars, no white border. The operator confirmed YOLO's existing behavior is fine.

4. **Visualization** (`infra/motion_visualization.py`): draw the green box at the same canonical bbox. Green box region = exact pixels in `crop_a.png` / `crop_b.png`. Single source of truth.

5. **Telegram**: sends `crop_a.png` / `crop_b.png` as-is (PNG, native resolution, no resize, no compression — operator directive).

**Concrete changes:**

| # | File:line | Change |
|---|---|---|
| 1 | `infra/frame_diff.py:110` | Replace `DEFAULT_BBOX_PADDING_PX = 16` with `DEFAULT_BBOX_PAD_PCT = 0.10` (10% per side = 20% overall per dimension) |
| 2 | `infra/frame_diff.py` (new helper) | Add `_expand_bbox_pct(bbox, pad_pct, frame_w, frame_h) -> tuple[int,int,int,int]`: percent-pad, round UP to multiple of 32, clamp to image bounds. Re-centers on original bbox center. |
| 3 | `infra/frame_diff.py:156` (`bbox_from_mask` signature) | `padding_px: int = DEFAULT_BBOX_PADDING_PX` → `pad_pct: float = DEFAULT_BBOX_PAD_PCT`. Body delegates to `_expand_bbox_pct`. |
| 4 | `infra/frame_diff.py:370` (`subject_bbox_from_two_masks` signature) | `padding_px: int = 8` → `pad_pct: float = DEFAULT_BBOX_PAD_PCT`. Body delegates to `_expand_bbox_pct`. Docstring updated. |
| 5 | `infra/frame_diff.py:555` (`diff_pair_with_bbox`) | `padding_px: int = DEFAULT_BBOX_PADDING_PX` → `pad_pct: float = DEFAULT_BBOX_PAD_PCT`. Forwards to `bbox_from_mask`. |
| 6 | `infra/frame_diff.py` (return contract) | `subject_bbox_a` / `subject_bbox_b` are now multiples of 32 in both dims. Documented in docstring. |
| 7 | `infra/gate.py:965-966` | `crop_bbox_a = subject_bbox_a; crop_bbox_b = subject_bbox_b` (already true; nothing to change). |
| 8 | `infra/motion_visualization.py:267` | Already reads `bbox_a` / `bbox_b` from the diff step. With change #3 above, those values are now the canonical padded-and-rounded bboxes. Green box region = crop region. **No code change needed here**, only verification. |
| 9 | `infra/quick_classifier.py:344` | Existing `pad_to_multiple_of_32` becomes a no-op for our crops. **No code change needed.** Confirmed by operator. |

## Why this design matches the original

- **One image-manipulation step** — diff step produces bbox + crop + visualization. No second-pass cropping, no white-pad.
- **No resize** — PIL crop at native resolution. PIL `Image.pad()` is not used at all.
- **No compression** — PNG stays PNG on disk and on the wire (V2-030 separately handles the file-vs-photo rendering problem with `type='photo'` and a bumped httpx timeout, NOT compression).
- **No YOLO bboxes/crops** — YOLO classifies the crop we hand it. YOLO's `raw_predictions[].bbox_xyxy` is logged for debugging only (already in `QuickVerdict`) but is not used to generate new regions downstream. A future US-031a3 may add a defensive guard for that field — separate story.
- **Percentage padding gives YOLO contrast** — operator's verbatim concern: *"padding of the differential pairwise image, it allows Yolo to have contrast to what it sees"*. 10% per-side padding gives consistent context regardless of subject bbox size.
- **Round-up to multiple of 32** — operator's verbatim refinement. Makes YOLO's existing pad a no-op. Single line of math.
- **Green box = crop region** — single bbox source. No mismatch possible. Operator can visually verify on every alert: the green box on the composite exactly outlines the pixels in `crop_a.png` / `crop_b.png`.

## Story index

### US-031a — single-bbox 10%-per-side rounded-to-multiple-of-32 padding

- **Branch:** `code/v2-031a-single-bbox-rounded-pad`
- **Owner:** coder
- **Workspace:** `worktree:<v2-repo>` (operator sets absolute path at dispatch time; `default` board has no `default_workdir`)
- **Depends on:** V2-030 (so the dispatch order is: V2-030 → V2-031, no concurrent dispatcher traffic on `infra/frame_diff.py` and `telegram_formatter/dispatcher.py`)

**Acceptance criteria:**

1. `infra/frame_diff.py:110` defines `DEFAULT_BBOX_PAD_PCT = 0.10` (10% per side = 20% overall per dimension; replaces `DEFAULT_BBOX_PADDING_PX = 16`).
2. New helper `_expand_bbox_pct(bbox, pad_pct, frame_w, frame_h) -> tuple[int,int,int,int]` at `infra/frame_diff.py` top. Math:
   - `raw_w = round(w * (1 + 2*pad_pct))`, `raw_h = round(h * (1 + 2*pad_pct))`
   - `new_w = ((raw_w + 31) // 32) * 32` (round UP to nearest multiple of 32; cap at frame_w)
   - `new_h = ((raw_h + 31) // 32) * 32` (round UP to nearest multiple of 32; cap at frame_h)
   - Re-center on original bbox center: `new_x = round(cx - new_w/2)`, `new_y = round(cy - new_h/2)`
   - Clamp: `new_x = max(0, min(new_x, frame_w - new_w))`, `new_y = max(0, min(new_y, frame_h - new_h))`
3. `bbox_from_mask` (currently line 156): `padding_px: int = DEFAULT_BBOX_PADDING_PX` → `pad_pct: float = DEFAULT_BBOX_PAD_PCT`. Body delegates to `_expand_bbox_pct` (frame dims come from the mask array).
4. `subject_bbox_from_two_masks` (currently line 370): `padding_px: int = 8` → `pad_pct: float = DEFAULT_BBOX_PAD_PCT`. Body delegates to `_expand_bbox_pct` (frame dims come from mask_a). Docstring "padding_px=8" → "pad_pct=0.10 (10% per side = 20% overall; rounded up to multiple of 32)".
5. `diff_pair_with_bbox` (currently line 555): `padding_px: int = DEFAULT_BBOX_PADDING_PX` → `pad_pct: float = DEFAULT_BBOX_PAD_PCT`. Forwards to `bbox_from_mask`.
6. Both consumers (`bbox_from_mask`, `subject_bbox_from_two_masks`) now use the same `DEFAULT_BBOX_PAD_PCT = 0.10`. After the change, both bboxes are multiples of 32 in both dims.
7. Tests in `tests/test_frame_diff.py` (one class `TestPadPct`, 4 tests):
   - `test_expand_bbox_pct_10_default`: `bbox=(100,200,50,80)`, frame `2304x1296` → `(93, 192, 64, 96)` — `50*1.20=60 → rounded up to 64`, `80*1.20=96` (already multiple of 32), centered on `(125, 240)`: `new_x = round(125-32) = 93`, `new_y = round(240-48) = 192`.
   - `test_expand_bbox_pct_clamps_to_image`: bbox at far corner `bbox=(2200,1200,100,90)` in frame `2304x1296` → both width and height clamp to fit, bbox stays inside frame.
   - `test_expand_bbox_pct_is_multiple_of_32`: assert `result[2] % 32 == 0 and result[3] % 32 == 0` for ten random bboxes across the size distribution seen in `logs/daemon.log`.
   - `test_subject_bbox_uses_default_pct`: regression — `subject_bbox_from_two_masks(mask_a, mask_b)` returns pct-padded-and-rounded bbox.
8. Tests in `tests/test_motion_visualization.py` (1 test):
   - `test_green_box_matches_crop_bbox`: given `crop_bbox_a` from a real alert, the green box on `pairwise_diff` has the same `(x0, y0, x1, y1)` as `crop_bbox_a`.
9. Live verification: trigger an alert on any camera. After it fires:
   - `data/frames/<camera>/<alert_id>/crop_a.png` and `crop_b.png` have width and height that are both multiples of 32 (verify with `identify -format '%w %h\n'`).
   - `pairwise_diff.png` has a green box whose region exactly equals the visible content of `crop_a.png`.
10. Pytest suite green (no regressions).
11. `grep -rn DEFAULT_BBOX_PADDING_PX infra/ --include='*.py' | grep -v test_` returns nothing.
12. `grep -n DEFAULT_BBOX_PAD_PCT infra/frame_diff.py` returns one line with `0.10`.

## Shipping definition

All US-031 stories marked done; pytest green (no regressions); `infra/frame_diff.py:110` reads `DEFAULT_BBOX_PAD_PCT = 0.10`; both `bbox_a` / `bbox_b` and `subject_bbox_a` / `subject_bbox_b` are produced via the same `_expand_bbox_pct` helper; `crop_a.png` / `crop_b.png` have width and height that are both multiples of 32; green box on `pairwise_diff.png` / `composite.png` exactly matches the crop region; live alert shows the same visual alignment operator reported missing on 2026-09-13; tree clean; commits pushed to forgejo `origin/main`.

## Scope exclusions

- Changing the canonical on-disk PNG format is NOT this PRD (zero-JPEG rule from US-019 unchanged).
- Pre-compressing PNG → JPEG for Telegram upload is NOT this PRD (V2-030 handles the file-vs-photo rendering problem with `type='photo'` + httpx timeout bump, not compression).
- Caching uploaded JPEGs in `data/tg_uploads/` is NOT this PRD (no cache, no second copy on disk; PNG goes straight to Telegram).
- Switching YOLO's residual `pad_to_multiple_of_32` from zero-padding to white-padding is NOT this PRD — the operator confirmed 2026-09-13 that YOLO's existing black-zero pad is fine, since the new bbox dimensions make it a no-op anyway.
- Tuning the 10%-per-side (20% overall) padding value (e.g., to 15% or 25%) is NOT this PRD — the operator chose 10% per side (20% overall). Future tuning is a separate decision.
- Letterbox-to-square (e.g., 640×640) or white-pad-to-square is NOT this PRD — the operator retracted the white-pad-to-square idea and the natural design is now: rectangular crop, dimensions rounded up to multiples of 32, no second padding.
- Adding a defensive guard against `QuickVerdict.raw_predictions.bbox_xyxy` being consumed downstream is NOT this PRD — separate story US-031a3 (if approved).
- Changing the per-camera motion thresholds or the gate verdict logic is NOT this PRD.
- Backfilling historical alerts is NOT this PRD — only new alerts produced after this ships use the new pipeline.

## Verification checklist

- `grep -rn DEFAULT_BBOX_PADDING_PX infra/ --include='*.py' | grep -v test_` returns nothing.
- `grep -n DEFAULT_BBOX_PAD_PCT infra/frame_diff.py` returns one line with `0.10`.
- `pytest tests/ -v` is green (full suite, no regressions).
- Live: trigger alert on any camera; `ls data/frames/<camera>/<alert_id>/` shows `crop_a.png` and `crop_b.png`; `identify -format '%w %h\n' data/frames/<camera>/<alert_id>/crop_a.png` returns `W H` where `W % 32 == 0` and `H % 32 == 0`.
- Live: open `crop_a.png` and `pairwise_diff.png` side-by-side; the green box on pairwise_diff exactly outlines the subject region of crop_a (no black bars around crop_a, since YOLO's pad is a no-op).
- `ruff check infra/` clean.
- Tree clean (no uncommitted changes); commits pushed to forgejo `origin/main`.

## Manual override notes

- For the median bbox (420×614, per `logs/daemon.log`): 10% per side yields (504, 737). Round up to multiples of 32: (512, 768). +52% area growth, but the crop is now YOLO-ready with no further padding. YOLO sees a clean 512×768 frame with no black bars.
- For very small bboxes (e.g., 60×40 distant subject): 10% per side yields (72, 48). Round up to multiples of 32: (96, 64). +156% area growth, but absolute is still small (~96×64). YOLO sees a small subject in a rectangular frame. Consistent and predictable.
- For very large bboxes (e.g., 1500×1000 close-up subject): 10% per side yields (1800, 1200). Round up to multiples of 32: (1824, 1216). +48% area. Bbox stays within 2304×1296 frame.
- Edge case: bbox at image boundary. The 10% per-side + round-to-32 expansion is clamped to image bounds BEFORE PIL crop. The crop cannot extend the subject region beyond the frame. Behavior is well-defined for all bbox positions.
- Edge case: bbox where `w == h` (square subject). After 10% per side, it may still be square if both raw dims round to the same multiple of 32, or rectangular if they round to different multiples. Both cases produce valid multiples-of-32 bboxes.

## References

- Operator directive, 2026-09-13 (verbatim): see "Original design" section above.
- Operator refinement, 2026-09-13: *"The way that YOLO does the padding already is fine. We don't need to change that"* — YOLO's `pad_to_multiple_of_32` stays.
- Operator refinement, 2026-09-13: *"when the 10% padding is applied to the B box, you can round it up so that the horizontal and vertical is a multiple of 32"* — the canonical bbox is rounded UP to multiples of 32.
- `infra/frame_diff.py:108-110` — current `DEFAULT_BBOX_PADDING_PX = 16` (the bug).
- `infra/gate.py:60-71` — current architecture docstring showing the original intent.
- `infra/gate.py:965-966` — `crop_bbox_a = subject_bbox_a` (already correct; subject bbox becomes the source of truth via this PRD).
- `infra/motion_visualization.py:260-280` — current green-box drawing code (no change needed).
- `infra/quick_classifier.py:297-360` — current YOLO `classify_frame` (no change needed; `pad_to_multiple_of_32` becomes a no-op).
- `logs/daemon.log` — 1272 samples of `crop_bbox_a=...` for grounding the padding math.
- Phase 6B.144 §11.66 (referenced in `infra/gate.py:42-44`) — earlier "YOLO-tighten" attempt that was reverted for the same reason (YOLO should not generate new bboxes/crops).
- V2-030 PRD — companion card handling the Telegram `type='document'` → `type='photo'` rendering bug. Ship V2-030 first so the two cards don't conflict on `telegram_formatter/dispatcher.py`.
