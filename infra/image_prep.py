"""
image_prep.py — Frame image processing (downscale + face-region crop).

STATUS: stable
THREAD SAFETY: thread-safe (PIL Image operations are CPU-bound but
    stateless; concurrent calls each open their own Image)

INPUTS:
    - file: a JPEG path on disk (frame_path for both functions)
    - function arg bbox_small: list[float] (required for crop) — Qwen's
      face bbox as [x1, y1, x2, y2] in `small_size` image coords
    - function arg small_size: tuple[int, int] (default (1280, 720))
    - function arg crop_size: tuple[int, int] (default INSIGHTFACE_CROP_SIZE)
    - function arg output_dir: str | None — auto-creates per-call tmpdir
      when None, so concurrent alerts can't collide on identical basenames

OUTPUTS:
    - return value (downscale_for_qwen): str — path to the downscaled JPEG
    - return value (crop_face_region_from_4k): str — path to the crop JPEG
    - writes files: <output_dir>/qwen_<base>.jpg, crop_<base>_face.jpg
    - PIL.Image import is deferred inside each function (module-load
      robustness if PIL is missing)

PUBLIC API:
    QWEN_INPUT_SIZE: tuple[int, int] = (1280, 720)
        Resolution sent to Qwen3-VL for scene understanding. ~950 tokens
        per image — fits comfortably in llama-server's per-slot budget
        under --parallel 4.
    INSIGHTFACE_CROP_SIZE: tuple[int, int] = (640, 640)
        InsightFace's working window. The detector resizes any input
        to this internally, so a 640x640 crop preserves maximum face
        detail.
    downscale_for_qwen(frame_path: str, output_dir: str | None = None) -> str
        Resize a single frame to QWEN_INPUT_SIZE. The 4K original is
        preserved on disk; Qwen gets the downscaled copy.
    crop_face_region_from_4k(frame_path: str, bbox_small: list[float],
                             small_size: tuple[int, int] = (1280, 720),
                             crop_size: tuple[int, int] = INSIGHTFACE_CROP_SIZE,
                             output_dir: str | None = None) -> str
        Extract a square crop centered on Qwen's reported bbox from the
        ORIGINAL high-res frame (not the downscaled one). Used by Phase
        6A to feed InsightFace.

DOES NOT DO:
    - Capture frames → infra.frame_capture (this is post-capture processing)
    - Encode to base64 for Qwen → infra.prompt_templates._encode_image
      (this module writes JPEGs to disk; base64 happens at prompt build)
    - Run face detection / InsightFace → Phase 6A pipeline
    - Validate bbox sanity (e.g. bbox outside image bounds) — clamping
      is applied at the crop boundaries but other inputs pass through

WHY HERE:
    Per AGENTS.md §4 (one module = one job), this was extracted from
    frame_capture.py in Part 9 step 6. Image preparation (downscale +
    crop) is conceptually distinct from frame capture (RTSP/snapshot
    reading). They share no state and have different dependencies
    (PIL vs PyAV). Keeping them in one module forced imports of PIL
    on every capture call even when only the RTSP path was used.

    The two functions are independent — downscale_for_qwen is called
    by infra.prompt_templates (lazy import) before base64 encoding;
    crop_face_region_from_4k is called by infra.pipeline_integration
    after Qwen reports a face bbox. They share PIL imports but no
    shared helpers.

    crop_face_region_from_4k uses bbox CENTER + relative coords rather
    than bbox corners because bbox accuracy is fuzzy (Qwen's box may be
    30x60 px in a 1280x720 image, off by ~5px). Center-cropping
    guarantees the face is inside the crop regardless of bbox jitter.

CALLED BY:
    - infra.prompt_templates._encode_image (lazy import of downscale_for_qwen)
    - infra.pipeline_integration.run_phase6a_recognition (crop_face_region_from_4k)

CALLS INTO:
    - PIL.Image (Pillow): open, thumbnail, crop, save
    - os.makedirs (stdlib)
    - tempfile.mkdtemp (stdlib)
    - logging (stdlib): log.debug / log.info on success

RELATED:
    - infra.frame_capture: produces the high-res JPEGs this module consumes
    - infra.persistent_rtsp: alternative source of frames (in-memory)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

log = logging.getLogger(__name__)


# Resolution sent to Qwen3-VL for scene understanding. Qwen just needs to
# report "what's in the scene" and a bbox around any face — it does not
# need full 4K detail. 720p keeps each image at ~950 image tokens,
# comfortably fitting in llama-server's per-slot budget under --parallel 4.
QWEN_INPUT_SIZE: tuple[int, int] = (1280, 720)


def downscale_for_qwen(frame_path: str, output_dir: str | None = None) -> str:
    """Downscale a frame to QWEN_INPUT_SIZE for sending to Qwen3-VL.

    Why this exists: at native 4K (3840x2160), Qwen allocates ~4100 image
    tokens per image — exceeding llama-server's per-slot ctx budget under
    --parallel 4 (default 2048/slot, configurable to 4096/slot at --ctx-size
    16384). At 720p, the same image is ~950 tokens — fits comfortably and
    Qwen still returns reliable bboxes (validated 2026-07-20: face bboxes
    on 720p map correctly to 4K coords via relative centering).

    The 4K original is preserved on disk for archival and InsightFace
    cropping — Qwen gets the downscaled copy, InsightFace gets a 640x640
    crop from the 4K original centered on Qwen's reported bbox.

    Args:
        frame_path: Path to the full-resolution JPEG (e.g. 4K).
        output_dir: Where to write the downscaled JPEG. Defaults to a
            per-call subdirectory under /tmp so concurrent alerts can't
            collide on identical basenames.

    Returns:
        Path to the downscaled JPEG. Caller is responsible for cleanup
        if desired — files are small and macOS cleans /tmp periodically.
    """
    import tempfile

    from PIL import Image

    img = Image.open(frame_path).convert("RGB")
    img.thumbnail(QWEN_INPUT_SIZE, Image.Resampling.LANCZOS)

    if output_dir is None:
        # Per-call subdir keeps concurrent alerts from clobbering each
        # other when frames share a basename (e.g. frame_001.jpg appears
        # in every alert dir).
        output_dir = tempfile.mkdtemp(prefix="qwen_downscaled_")
    os.makedirs(output_dir, exist_ok=True)

    base = os.path.basename(frame_path)
    out_path = os.path.join(output_dir, f"qwen_{base}")
    img.save(out_path, format="JPEG", quality=85)
    log.debug(f"Downscaled {frame_path} → {out_path}")
    return out_path


# InsightFace's working window. The detector resizes any input to this
# internally, so a 640x640 crop from the 4K frame preserves maximum face
# detail. The crop dimension equals the detector's input — feeding larger
# wastes encode/decode time without improving detection.
INSIGHTFACE_CROP_SIZE: tuple[int, int] = (640, 640)


def crop_face_region_from_4k(
    frame_path: str,
    bbox_small: Sequence[float | int],
    small_size: tuple[int, int] = (1280, 720),
    crop_size: tuple[int, int] = INSIGHTFACE_CROP_SIZE,
    output_dir: str | None = None,
) -> str:
    """Crop a region around a face bbox from a high-res frame.

    Used by Phase 6A: Qwen reports the face bbox in the DOWNSAMPLED image's
    coords (e.g. 1280x720). We convert that bbox's CENTER to relative
    coords (0..1) and crop a `crop_size` region from the ORIGINAL full-res
    frame (e.g. 3840x2160) at those relative coords.

    Why center + relative coords (not just bbox corner): bbox accuracy is
    fuzzy — Qwen's box may be 30x60 px in a 1280x720 image, off by ~5px on
    each side. Center-cropping on the bbox center guarantees the face is
    inside the crop regardless of bbox jitter. We don't snap to bbox
    corners because we want some context around the face (ears, hair)
    which helps InsightFace's landmark detector.

    Args:
        frame_path: Path to the high-res JPEG (e.g. 4K).
        bbox_small: Qwen's face bbox as [x1, y1, x2, y2] in
            `small_size` image coords. Must be 4 numbers.
        small_size: (width, height) of the image Qwen saw.
        crop_size: (width, height) of the crop to extract. Defaults to
            INSIGHTFACE_CROP_SIZE (640x640). Always square.
        output_dir: Where to write the crop JPEG. Defaults to a per-call
            tmpdir to avoid collisions.

    Returns:
        Path to the crop JPEG.
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

    base = os.path.basename(frame_path).replace(".jpg", "_face.jpg")
    out_path = os.path.join(output_dir, f"crop_{base}")
    crop.save(out_path, format="JPEG", quality=95)
    log.info(
        f"Phase 6A: cropped face region from {frame_path} "
        f"({full_w}x{full_h}) → {out_path} ({crop_w}x{crop_h}) "
        f"at relative center ({cx_frac:.3f}, {cy_frac:.3f})"
    )
    return out_path
