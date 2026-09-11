"""
alert_artifacts.py — AlertArtifacts model + prepare_alert_artifacts orchestrator.

Phase 6B.115 (V2 port): stage 2 of the 11-stage pipeline. Accepts a
GateVerdict (with in-memory PIL crops) and produces three artifact paths
on disk: crop_a.png, crop_b.png, composite.png.

STATUS: stable
THREAD SAFETY: thread-safe (pure function on inputs; no shared mutable state).

LAYER 1: crop_a.png — gate_verdict.crop_a saved as lossless PNG (or
    None when no subject bbox was found).
LAYER 2: crop_b.png — gate_verdict.crop_b saved as lossless PNG (or
    None when no subject bbox was found).
LAYER 3: composite.png — render_motion_composite(frames, bbox_a, bbox_b)
    called ONCE, result path stored.
LAYER 4: full_frame_path — frame_paths[3] (best-frame slot), required,
    never None.

INPUTS:
    - gate_verdict: GateVerdict (from infra.gate.run) — provides
      crop_a, crop_b (PIL.Image or None), frames (list[PIL.Image]),
      bbox_a, bbox_b.
    - frame_paths: list[str] — 4 frame paths from frame capture.
    - output_dir: str — directory for artifact files.

OUTPUTS:
    - AlertArtifacts dataclass (crop_a_path, crop_b_path,
      composite_path, full_frame_path).

PUBLIC API:
    AlertArtifacts dataclass
    prepare_alert_artifacts(gate_verdict, frame_paths, output_dir) -> AlertArtifacts

DOES NOT DO:
    - Does NOT classify vehicles (that's the gate's job).
    - Does NOT send Telegram (that's the pipeline's job).
    - Does NOT compute motion detection (gate already did that).
    - Does NOT resize or re-encode frames.
    - Does NOT draw bboxes on crops (composite handles bboxes).

CALLED BY:
    - listener/pipeline.py (stage 2, after gate runs).

CALLS INTO:
    - infra.motion_visualization.render_motion_composite.
    - PIL.Image.save (PNG, optimize=False).

RELATED:
    - infra.gate.GateVerdict — provides crop_a, crop_b, frames, bboxes.
    - infra.motion_visualization.render_motion_composite — composite renderer.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from infra.motion_visualization import render_motion_composite

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AlertArtifacts dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertArtifacts:
    """Immutable artifact paths produced by prepare_alert_artifacts.

    Fields:
        crop_a_path: Path to crop_a.png (PNG lossless), or None if crop_a
            was None (no subject bbox found by motion-diff).
        crop_b_path: Path to crop_b.png (PNG lossless), or None if crop_b
            was None.
        composite_path: Path to composite.png (render_motion_composite).
        full_frame_path: Path to frame_paths[3] (best frame). Required,
            never None.
    """

    crop_a_path: str | None
    crop_b_path: str | None
    composite_path: str | None
    full_frame_path: str


# ---------------------------------------------------------------------------
# Crop saver
# ---------------------------------------------------------------------------


def _save_crop(pil_image: Image.Image | None, output_dir: str) -> str | None:
    """Save a PIL crop as lossless PNG to output_dir.

    PNG is REQUIRED by V2-019 design principle #7 (zero JPEG).
    optimize=False per operator's "no re-encode" rule.

    Returns the absolute path, or None if pil_image is None.
    """
    if pil_image is None:
        return None
    path = Path(output_dir) / "crop_a.png"  # placeholder; caller overrides for crop_b
    pil_image.save(str(path), format="PNG", optimize=False)
    return str(path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def prepare_alert_artifacts(
    gate_verdict, frame_paths: list[str], output_dir: str
) -> AlertArtifacts:
    """Produce three artifact paths from a gate verdict.

    Orchestrates:
      1. Save crop_a.png from gate_verdict.crop_a (or None).
      2. Save crop_b.png from gate_verdict.crop_b (or None).
      3. Call render_motion_composite(frames, bbox_a, bbox_b, output_dir)
         exactly ONCE and store the result.
      4. Set full_frame_path = frame_paths[3] (best frame).

    Args:
        gate_verdict: GateVerdict from infra.gate.run — provides
            crop_a, crop_b (PIL.Image | None), frames (4 PIL images),
            bbox_a, bbox_b.
        frame_paths: 4 frame paths from the frame capture stage.
        output_dir: canonical alert-scoped directory (from
            infra.paths.data_dir_for).

    Returns:
        AlertArtifacts with all four paths.
    """
    os.makedirs(output_dir, exist_ok=True)

    # 1. Save crop_a.
    crop_a_path = None
    if gate_verdict.crop_a is not None:
        a_path = Path(output_dir) / "crop_a.png"
        gate_verdict.crop_a.save(str(a_path), format="PNG", optimize=False)
        crop_a_path = str(a_path)

    # 2. Save crop_b.
    crop_b_path = None
    if gate_verdict.crop_b is not None:
        b_path = Path(output_dir) / "crop_b.png"
        gate_verdict.crop_b.save(str(b_path), format="PNG", optimize=False)
        crop_b_path = str(b_path)

    # 3. Render composite (called ONCE).
    composite_path = None
    if gate_verdict.frames:
        composite_path = render_motion_composite(
            frames=gate_verdict.frames,
            bbox_a=gate_verdict.bbox_a,
            bbox_b=gate_verdict.bbox_b,
            output_dir=output_dir,
        )
        if not composite_path:
            log.warning(
                f"[{gate_verdict.reason}] composite render returned empty path"
            )

    # 4. Full frame path (best frame slot — required, never None).
    full_frame_path = frame_paths[3] if len(frame_paths) >= 4 else ""

    # Log once per webhook with structured fields.
    log.info(
        f"alert_artifacts: alert_id={gate_verdict.reason} "
        f"crop_a={'present' if crop_a_path else 'None'} "
        f"crop_b={'present' if crop_b_path else 'None'} "
        f"composite_path={composite_path or 'None'} "
        f"full_frame_path={full_frame_path}"
    )

    return AlertArtifacts(
        crop_a_path=crop_a_path,
        crop_b_path=crop_b_path,
        composite_path=composite_path,
        full_frame_path=full_frame_path,
    )
