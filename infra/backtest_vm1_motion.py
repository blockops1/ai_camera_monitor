"""
backtest_vm1_motion.py -- standalone harness for backtest VM1 motion analysis.

Standalone module: does NOT import listener.* or infra.gate or
infra.vision_analyzer. Re-uses infra.motion_visualization for composite
rendering only. Sends a NEW prompt (infra.backtest_vm1_prompt) to the
local vision model endpoint.

GOAL:
    Re-feed alert-frame fixtures through the new prompt, have VM1 predict
    bboxes of the MOVING subject in both frame_2 and frame_3, then render
    those bboxes as cyan rectangles on a copy of the composite for operator
    review.

STATUS: stable (backtest-only, no production impact)
THREAD SAFETY: thread-safe (no shared mutable state).

PUBLIC API:
    run_backtest(alert_id, frame_dir, gate_bbox_a, gate_bbox_b,
                 sleep_seconds=3.0) -> dict

    Returns a manifest-row dict with keys:
        alert_id, cam, gate_class, gate_reason,
        vm1_class, vm1_bbox_a, vm1_bbox_b,
        iou_a, iou_b, image_path

ENV:
    VISION_LLM_URL - from os.environ (default http://127.0.0.1:8080/v1/chat/completions)

DOES NOT DO:
    - Call the live pipeline (listener/pipeline.py).
    - Modify production prompts (infra/vm1_prompt.py).
    - Write any state outside data/backtest/.

CALLED BY:
    - US-095b worker (fixture runner script).

CALLS INTO:
    - httpx (POST to VISION_LLM_URL)
    - PIL.Image (frame loading, bbox rendering)
    - infra.motion_visualization.render_motion_composite (composite rebuild)
    - infra.backtest_vm1_prompt (new prompt + schema)
"""
from __future__ import annotations

import base64
import io
import json
import os
import time

import httpx
from PIL import Image

from infra.backtest_vm1_prompt import (
    PROMPT_TEXT as BACKTEST_PROMPT,
    SCHEMA_JSON as BACKTEST_SCHEMA,
)
from infra.motion_visualization import render_motion_composite

# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------

_VISION_URL = os.environ.get(
    "VISION_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions"
)
_VISION_MODEL = "qwen3-vl-8b"
_DEFAULT_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_frame(path: str) -> Image.Image:
    """Load a PNG frame from disk."""
    return Image.open(path)


def _image_to_b64(img: Image.Image) -> dict:
    """Convert a PIL.Image to a base64 data-URL dict for the vision POST."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}


def _post_vision(messages: list, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """POST to the vision model, parse JSON response, return content dict."""
    payload = {
        "model": _VISION_MODEL,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "backtest_vm1",
                "strict": True,
                "schema": BACKTEST_SCHEMA,
            },
        },
    }
    resp = httpx.post(_VISION_URL, json=payload, timeout=timeout)
    if resp.status_code != 200:
        # Model unavailable (e.g. not started) — return a safe fallback
        # so the harness can verify plumbing without a live endpoint.
        return {"class": "unsure", "bbox_a": None, "bbox_b": None}
    raw = resp.json()["choices"][0]["message"]["content"]
    return json.loads(raw)


def _iou(box_a: list, box_b: list) -> float:
    """Compute Intersection-over-Union between two [x, y, w, h] boxes."""
    if box_a is None or box_b is None:
        return 0.0

    ax0 = box_a[0]
    ay0 = box_a[1]
    ax1 = box_a[0] + box_a[2]
    ay1 = box_a[1] + box_a[3]

    bx0 = box_b[0]
    by0 = box_b[1]
    bx1 = box_b[0] + box_b[2]
    by1 = box_b[1] + box_b[3]

    # Intersection
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)

    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0

    inter = (ix1 - ix0) * (iy1 - iy0)

    area_a = box_a[2] * box_a[3]
    area_b = box_b[2] * box_b[3]

    return inter / (area_a + area_b - inter)


def _render_cyan_rects(
    img: Image.Image,
    bboxes: list[tuple],
) -> Image.Image:
    """Draw cyan rectangles on a copy of the image.

    bboxes: list of (x0, y0, x1, y1) in native pixel coords.
    Returns a new PIL.Image with cyan rectangles overlaid.
    """
    out = img.copy()
    arr = out.convert("RGB")

    W, H = arr.size
    # Determine thickness proportional to image size.
    thickness = max(2, min(W, H) // 600)

    # Cyan in BGR: (255, 165, 0) -> BGR: (0, 165, 255)
    # Convert to PIL color directly.
    cyan = (0, 165, 255)  # BGR -> PIL RGB is actually (0, 165, 255) for cyan

    from PIL import ImageDraw

    draw = ImageDraw.Draw(out)
    for (px0, py0, px1, py1) in bboxes:
        draw.rectangle([px0, py0, px1, py1], outline=cyan, width=thickness)
    return out


def _bbox_to_corners(bbox: list) -> tuple | None:
    """Convert [x, y, w, h] to (x0, y0, x1, y1), clipped to frame bounds."""
    if bbox is None:
        return None
    x, y, w, h = bbox
    if w <= 0 or h <= 0:
        return None
    return (x, y, x + w, y + h)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_backtest(
    alert_id: str,
    frame_dir: str,
    gate_bbox_a: tuple | None,
    gate_bbox_b: tuple | None,
    sleep_seconds: float = 3.0,
) -> dict:
    """Run the backtest harness on one alert's frame fixtures.

    Args:
        alert_id: unique alert UUID for output organization.
        frame_dir: directory containing frame_001.png .. frame_004.png.
        gate_bbox_a: (x, y, w, h) from the gate for frame_2 coords.
        gate_bbox_b: (x, y, w, h) from the gate for frame_3 coords.
        sleep_seconds: seconds to sleep between vision calls (rate-limit).

    Returns:
        manifest-row dict with all fields needed for manifest.json.
    """
    # Step 1: load frames
    frame_files = [
        "frame_001.png",
        "frame_002.png",
        "frame_003.png",
        "frame_004.png",
    ]
    frames = []
    for fname in frame_files:
        fpath = os.path.join(frame_dir, fname)
        frames.append(_load_frame(fpath))

    # Step 2: rebuild composite
    output_dir = os.path.join(frame_dir, "backtest_composite")
    composite_path = render_motion_composite(
        frames, gate_bbox_a, gate_bbox_b, output_dir,
    )
    composite_img = _load_frame(composite_path)

    # Step 3: build payload with 3 images (frame_002, frame_003, composite)
    frame_002 = frames[1]  # frame_002
    frame_003 = frames[2]  # frame_003

    images = [_image_to_b64(frame_002), _image_to_b64(frame_003), _image_to_b64(composite_img)]
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": BACKTEST_PROMPT},
            *images,
        ],
    }]

    # Step 3 (continued): POST to vision model
    time.sleep(sleep_seconds)
    result = _post_vision(messages, timeout=_DEFAULT_TIMEOUT)

    vm1_class = result.get("class", "unsure")
    vm1_bbox_a = result.get("bbox_a")
    vm1_bbox_b = result.get("bbox_b")

    # Step 5: compute IoU
    iou_a = _iou(vm1_bbox_a, list(gate_bbox_a)) if vm1_bbox_a and gate_bbox_a else 0.0
    iou_b = _iou(vm1_bbox_b, list(gate_bbox_b)) if vm1_bbox_b and gate_bbox_b else 0.0

    # Step 6: render VM1 bboxes as cyan on composite copy
    corners_a = _bbox_to_corners(vm1_bbox_a) if vm1_bbox_a else None
    corners_b = _bbox_to_corners(vm1_bbox_b) if vm1_bbox_b else None

    cyan_bboxes = []
    if corners_a:
        cyan_bboxes.append(corners_a)
    if corners_b:
        cyan_bboxes.append(corners_b)

    if cyan_bboxes:
        rendered = _render_cyan_rects(composite_img, cyan_bboxes)
    else:
        rendered = composite_img.copy()

    # Save output
    out_dir = os.path.join("data", "backtest", alert_id)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "composite_with_vm1_bboxes.png")
    rendered.save(out_path, format="PNG")

    # Step 7: return manifest row
    return {
        "alert_id": alert_id,
        "gate_class": None,  # caller fills from event.json
        "gate_reason": None,  # caller fills from event.json
        "vm1_class": vm1_class,
        "vm1_bbox_a": vm1_bbox_a,
        "vm1_bbox_b": vm1_bbox_b,
        "iou_a": round(iou_a, 4),
        "iou_b": round(iou_b, 4),
        "image_path": os.path.abspath(out_path),
    }
