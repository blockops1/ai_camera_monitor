"""
select_frame — Stage 4: pick the best frame for the alert photo.

STATUS: stable
THREAD SAFETY: single-threaded (one call per alert; runs inside camera semaphore)

INPUTS:
    - ctx: AlertContext (frame_paths, vision_result)

OUTPUTS:
    - mutates ctx.best_frame_path: str
        Defaults to frame_paths[0]. If vision_result.selected_frame is in
        frame_paths, use that.

PUBLIC API:
    select_best_frame_stage(ctx: AlertContext) -> None
        Stage 4 driver. Default: first captured frame. Updated to use
        vision_result["selected_frame"] when present.

DOES NOT DO:
    - Re-encode / resize the frame — the alert layer owns that.
    - Decide which Telegram uses this frame — emit_result_stage sets
      ctx.alert["frame_path"] = best_frame_path.

WHY HERE:
    Single-purpose stage with one trivial rule. Extracted because the
    6-stage flow benefits from each stage having a name + a docstring,
    not a blob of code inside process_alert().

CALLED BY:
    - process_alert (in __init__.py) — Stage 4 driver

CALLS INTO:
    - (nothing — pure function on ctx)

RELATED:
    - §11.86 in PLAN.md — face_visibility priority chain (Phase.86
      extended this; deferred for non-face events)
"""
from __future__ import annotations

import logging

from .context import AlertContext

log = logging.getLogger(__name__)


def select_best_frame_stage(ctx: AlertContext) -> None:
    """Stage 4: pick the best frame for the alert photo.

    Default: first captured frame. Updated priority chain when
    face_visibility is on (Phase.86).

    Mutates ctx: best_frame_path.
    """
    if not ctx.frame_paths:
        ctx.best_frame_path = ""
        return

    # Default: first captured frame.
    ctx.best_frame_path = ctx.frame_paths[0]

    # If vision selected a frame, use that.
    if ctx.vision_result and ctx.vision_result.get("selected_frame"):
        selected = ctx.vision_result.get("selected_frame")
        if selected in ctx.frame_paths:
            ctx.best_frame_path = selected
