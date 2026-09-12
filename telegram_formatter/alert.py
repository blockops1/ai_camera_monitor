"""alert.py -- Build TG#1 Telegram message dict after VM1 classification.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - verdict: GateVerdict (required) -- YOLO gate result
    - vm1_result: dict (required) -- VM1 classify output with
      'class', 'confidence', optional 'notes'
    - artifacts: AlertArtifacts (required) -- composite + full frame paths
    - alert: dict | None (optional) -- parent alert dict, for error context

OUTPUTS:
    - return dict: {"caption": str, "photos": list[str]}
      shaped for the Telegram client

PUBLIC API:
    build_alert_message(verdict, vm1_result, artifacts, alert=None) -> dict
        Build a Telegram-ready message dict for TG#1

DOES NOT DO:
    - Send the Telegram message (pipeline.py handles transport)
    - Render license plates (not extracted at this stage)
    - Do any filesystem I/O (paths are passed in)

CALLED BY:
    - listener/pipeline.py stage 7 (TG#1 emission after VM1)

CALLS INTO:
    - stdlib str(): path-to-string conversion

RELATED:
    - infra.gate.GateVerdict
    - infra.alert_artifacts.AlertArtifacts
"""

from __future__ import annotations

from typing import Any

from infra.alert_artifacts import AlertArtifacts
from infra.gate import GateVerdict

_CAPTION_MAX_LENGTH = 1024  # Telegram caption limit


def _truncate_caption(caption: str) -> str:
    """Truncate caption to 1024 chars, adding ellipsis if needed."""
    if len(caption) <= _CAPTION_MAX_LENGTH:
        return caption
    return caption[: _CAPTION_MAX_LENGTH - 3] + "..."


def build_alert_message(
    verdict: GateVerdict,
    vm1_result: dict[str, Any],
    artifacts: AlertArtifacts,
    camera_label: str = "Camera",
    alert: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a TG#1 Telegram message dict.

    Returns photos = [composite_path, full_frame_path] if composite_path
    is not None.  If composite_path is None (gate returned no bbox),
    returns photos = [full_frame_path] only -- single photo.
    """
    full_frame_path = artifacts.full_frame_path
    composite_path = artifacts.composite_path

    cls = vm1_result["class"]
    conf = vm1_result["confidence"]
    notes = vm1_result.get("notes")

    # Extract alert metadata.
    alert_id = alert.get("id") if alert else None
    timestamp = alert.get("timestamp") if alert else None

    lines = [
        f"Camera: {camera_label}",
        f"Classification: {verdict.classification}",
        f"YOLO: {verdict.top_class} ({verdict.top_confidence:.2f})",
        f"Vision: {cls} ({conf})",
    ]
    if alert_id:
        lines.append(f"Alert ID: {alert_id}")
    if timestamp:
        lines.append(f"Timestamp: {timestamp}")
    if notes:
        lines.append(notes)

    # Photos: composite + full frame, or full frame only.
    photos: list[str] = []
    if composite_path is not None:
        photos.append(composite_path)
    photos.append(full_frame_path)

    caption = "\n".join(lines)
    caption = _truncate_caption(caption)

    return {
        "caption": caption,
        "photos": photos,
    }
