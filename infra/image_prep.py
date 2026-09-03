"""
image_prep.py — Frame image processing (face-region crop; downscale removed §11.88).

STATUS: stable
THREAD SAFETY: thread-safe (PIL Image operations are CPU-bound but
    stateless; concurrent calls each open their own Image)

INPUTS:
    - file: a PNG path on disk (frame_path for both functions).
      §11.88 (2026-09-01): frames are PNGs (lossless), not JPEGs.
    - function arg bbox_small: list[float] (required for crop) — Qwen's
      face bbox as [x1, y1, x2, y2] in `small_size` image coords
    - function arg small_size: tuple[int, int] (default NATIVE_RES = 2304x1296)
    - function arg crop_size: tuple[int, int] (default INSIGHTFACE_CROP_SIZE)
    - function arg output_dir: str | None — auto-creates per-call tmpdir
      when None, so concurrent alerts can't collide on identical basenames

OUTPUTS:
    - return value (downscale_for_qwen): str — frame_path unchanged (pass-through)
    - return value (crop_face_region_from_4k): str — path to the crop PNG
    - writes files: crop_<base>_face.png
      (was qwen_<base>.jpg + crop_<base>_face.jpg pre-§11.88)
    - PIL.Image import is deferred inside each function (module-load
      robustness if PIL is missing)

PUBLIC API:
    NATIVE_RES: tuple[int, int] = (2304, 1296)
        Reolink RLC-833A native resolution (§11.88). Qwen3-VL now sees
        frames at this resolution — no downsample. ~4100 image tokens/image,
        fits within --ctx-size 16384 under --parallel 4.
    INSIGHTFACE_CROP_SIZE: tuple[int, int] = (640, 640)
        InsightFace's working window. The detector resizes any input
        to this internally, so a 640x640 crop preserves maximum face
        detail.
    downscale_for_qwen(frame_path: str, output_dir: str | None = None) -> str
        §11.88 PASS-THROUGH. Returns frame_path unchanged. Kept as a named
        pass-through so existing callers (infra.prompt_templates) don't break.
    crop_face_region_from_4k(frame_path: str, bbox_small: list[float],
                             small_size: tuple[int, int] = NATIVE_RES,
                             crop_size: tuple[int, int] = INSIGHTFACE_CROP_SIZE,
                             output_dir: str | None = None) -> str
        Extract a square crop centered on Qwen's reported bbox from the
        frame. Used by Phase 6A to feed InsightFace. Since §11.88,
        small_size defaults to NATIVE_RES because Qwen sees native res.

DOES NOT DO:
    - Capture frames → infra.frame_capture (this is post-capture processing)
    - Encode to base64 for Qwen → infra.prompt_templates._encode_image
      (this module operates on file paths; base64 happens at prompt build)
    - Run face detection / InsightFace → Phase 6A pipeline
    - Validate bbox sanity (e.g. bbox outside image bounds) — clamping
      is applied at the crop boundaries but other inputs pass through
    - Downsample frames for Qwen → removed in §11.88 (was here pre-2026-09-01)

WHY HERE:
    Per AGENTS.md §4 (one module = one job), this was extracted from
    frame_capture.py in Part 9 step 6. Image preparation (face-region
    crop, previously also downsample) is conceptually distinct from
    frame capture (RTSP/snapshot reading). They share no state and
    have different dependencies (PIL vs PyAV). Keeping them in one
    module forced imports of PIL on every capture call even when only
    the RTSP path was used.

    §11.88 (2026-09-01) — the downscale half of this module was
    removed. Functionally the module is now: `crop_face_region_from_4k`
    + the pass-through `downscale_for_qwen` kept for call-site
    compatibility. Inputs (PNG files) and outputs (PNG crops) are now
    uniform — see PUBLIC API.

    The remaining function is called by infra.pipeline_integration
    after Qwen reports a face bbox. PIL is imported lazily inside the
    function (module-load robustness if PIL is missing).

    crop_face_region_from_4k uses bbox CENTER + relative coords rather
    than bbox corners because bbox accuracy is fuzzy (Qwen's box may
    be 30x60 px, off by ~5px). Center-cropping guarantees the face is
    inside the crop regardless of bbox jitter. With §11.88's native-res
    Qwen input, bbox jitter in absolute pixels halves vs 1280x720.

CALLED BY:
    - infra.prompt_templates._encode_image (lazy import of downscale_for_qwen)
    - infra.pipeline_integration.run_phase6a_recognition (crop_face_region_from_4k)

CALLS INTO:
    - PIL.Image (Pillow): open, thumbnail, crop, save
    - os.makedirs (stdlib)
    - tempfile.mkdtemp (stdlib)
    - logging (stdlib): log.debug / log.info on success

RELATED:
    - infra.frame_capture: produces the high-res PNGs this module consumes
    - infra.persistent_rtsp: alternative source of frames (in-memory)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

log = logging.getLogger(__name__)


# §11.88 (2026-09-01) — downscale_for_qwen is now a PASS-THROUGH. Previously
# resized frames to (1280, 720) before sending to Qwen3-VL to fit llama-server's
# per-slot token budget (--parallel 4, --ctx-size 16384 -> ~4096 tok/slot).
# At native 4K (~4100 tokens/image), that budget is full but feasible. The
# downsample caused bbox jitter for face recognition (Qwen bbox in 1280x720
# coords has 1.8x more pixel-jitter than the same bbox in native 4K). Sending
# Qwen the native-resolution PNG removes one lossy step and gives InsightFace
# a more accurate face-center anchor.
NATIVE_RES: tuple[int, int] = (2304, 1296)
"""Reolink RLC-833A native resolution (width, height). Frames from
infra.persistent_rtsp / infra.frame_capture are at this size after §11.88."""


def downscale_for_qwen(frame_path: str, output_dir: str | None = None) -> str:
    """PASS-THROUGH (was downsample).

    §11.88 (2026-09-01) — removed the 1280x720 downsample. Qwen3-VL now sees
    the native-resolution PNG frames directly. Cost: ~4100 image tokens per
    image (fits within --ctx-size 16384 under --parallel 4 = 4096/slot).

    This function is kept as a pass-through so existing callers don't break.
    The function signature is unchanged; it returns frame_path unchanged.

    Args:
        frame_path: Path to the full-resolution PNG (e.g. 2304x1296).
        output_dir: IGNORED — kept for backward-compatibility only.

    Returns:
        frame_path unchanged.
    """
    return frame_path


# InsightFace's working window. The detector resizes any input to this
# internally, so a 640x640 crop from the 4K frame preserves maximum face
# detail. The crop dimension equals the detector's input — feeding larger
# wastes encode/decode time without improving detection.
INSIGHTFACE_CROP_SIZE: tuple[int, int] = (640, 640)


def crop_face_region_from_4k(
    frame_path: str,
    bbox_small: Sequence[float | int],
    small_size: tuple[int, int] = NATIVE_RES,
    crop_size: tuple[int, int] = INSIGHTFACE_CROP_SIZE,
    output_dir: str | None = None,
) -> str:
    """Crop a region around a face bbox from a high-res frame.

    Used by Phase 6A: Qwen reports the face bbox in the image it saw.
    §11.88 (2026-09-01): Qwen now sees native-resolution PNG (2304x1296),
    so `small_size` defaults to NATIVE_RES. Previously Qwen saw 1280x720
    downscaled and `small_size` defaulted to (1280, 720).

    We convert the bbox's CENTER to relative coords (0..1) and crop a
    `crop_size` region from the ORIGINAL frame at those relative coords.
    Since `small_size` now equals the frame's native res, the relative
    coords land pixel-perfect (no rescaling ambiguity).

    Why center + relative coords (not just bbox corner): bbox accuracy is
    fuzzy — Qwen's box may be 30x60 px, off by ~5px on each side.
    Center-cropping on the bbox center guarantees the face is inside
    the crop regardless of bbox jitter. We don't snap to bbox corners
    because we want some context around the face (ears, hair) which
    helps InsightFace's landmark detector.

    Args:
        frame_path: Path to the high-res frame (e.g. PNG, 2304x1296).
        bbox_small: Qwen's face bbox as [x1, y1, x2, y2] in
            `small_size` image coords. Must be 4 numbers.
        small_size: (width, height) of the image Qwen saw. Defaults to
            NATIVE_RES (2304x1296). Pass the literal image size Qwen
            actually saw if you've downscaled externally.
        crop_size: (width, height) of the crop to extract. Defaults to
            INSIGHTFACE_CROP_SIZE (640x640). Always square.
        output_dir: Where to write the crop PNG. Defaults to a per-call
            tmpdir to avoid collisions.

    Returns:
        Path to the crop PNG.
    """
    import tempfile

    from PIL import Image

    if len(bbox_small) != 4:
        raise ValueError(f"bbox_small must be [x1,y1,x2,y2], got {bbox_small}")

    small_w, small_h = small_size
    crop_w, crop_h = crop_size
    if crop_w != crop_h:
        raise ValueError(f"crop_size must be square (got {crop_size})")

    # Compute bbox center in relative coords (0..1)
    cx_small = (bbox_small[0] + bbox_small[2]) / 2.0
    cy_small = (bbox_small[1] + bbox_small[3]) / 2.0
    cx_frac = cx_small / small_w
    cy_frac = cy_small / small_h

    # Open high-res frame and crop around the relative center
    img = Image.open(frame_path).convert("RGB")
    full_w, full_h = img.size
    cx_full = int(cx_frac * full_w)
    cy_full = int(cy_frac * full_h)

    half = crop_w // 2
    x1 = max(0, cx_full - half)
    y1 = max(0, cy_full - half)
    x2 = min(full_w, x1 + crop_w)
    y2 = min(full_h, y1 + crop_h)
    # If the crop hit an image edge, shift it back so we still get a
    # full crop_size region (instead of a smaller rectangle).
    if x2 - x1 < crop_w:
        x1 = max(0, full_w - crop_w)
        x2 = full_w
    if y2 - y1 < crop_h:
        y1 = max(0, full_h - crop_h)
        y2 = full_h

    crop = img.crop((x1, y1, x2, y2))

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="face_crop_")
    os.makedirs(output_dir, exist_ok=True)

    base = os.path.basename(frame_path)
    base_no_ext = os.path.splitext(base)[0]
    out_path = os.path.join(output_dir, f"crop_{base_no_ext}_face.png")
    crop.save(out_path, format="PNG")
    log.info(
        f"Phase 6A: cropped face region from {frame_path} "
        f"({full_w}x{full_h}) → {out_path} ({crop_w}x{crop_h}) "
        f"at relative center ({cx_frac:.3f}, {cy_frac:.3f})"
    )
    return out_path
