"""Composite Telegram body + sender — the motion-trail composite.

Phase.111 (2026-08-21) — RESTORED after the 6B.105c slim dropped it.

The composite Telegram was the SECOND Telegram in the legacy lead motion message
stack (motion + composite + match). Phase.79 (2026-08-14) introduced
render_motion_composite; Phase.105c (2026-08-21) slimmed listener.py
and removed the calling code. The composite module is still here
(`infra/motion_visualization.py`) and tested (10+ tests), but the slim
pipeline never calls it — so the motion-trail visualization hasn't been
sending since the slim shipped.

This module:
  * `build_composite_telegram_body(input)` — pure function, builds the
    composite Telegram body string.
  * `send_composite_alert(...)` — renders the composite JPEG via
    infra.motion_visualization.render_motion_composite, then sends it
    as a Telegram photo-with-caption via infra.send_telegram.

Body layout (matches what the legacy archive had):
    🛣️ <b>Motion trail at {camera_name}</b>
       {captured_at}
       trajectory: {top-left → center → bottom-right}

Sender is failure-tolerant: any render failure or send failure logs a
warning and returns False. The composite Telegram is enrichment — a
failure must NEVER suppress the lead motion Telegram that already went
out, and must NEVER suppress the match Telegram that follows.

STATUS: stable
THREAD SAFETY: sender does I/O (disk read + Telegram HTTP POST) but
    holds no shared mutable state. Body builder is a pure function.
INPUTS:
  - CompositeTelegramInput (camera_name, captured_at_iso, trajectory,
    alert_id)
  - send_composite_alert args: alert_id, camera_name, frame_paths,
    primary_moving_object (MovingObject or None), bot_token, chat_id,
    captured_at
OUTPUTS:
  - body: str (no I/O)
  - send: bool (True on successful Telegram send)
PUBLIC API:
  CompositeTelegramInput — frozen dataclass
  build_composite_telegram_body(input) -> str
  send_composite_alert(alert_id, camera_name, frame_paths,
                       primary_moving_object, bot_token, chat_id,
                       captured_at) -> bool
DOES NOT DO:
  - Detect motion (lives in vehicle_position.motion_detector)
  - Render the composite image (lives in infra.motion_visualization)
  - Send the lead motion Telegram (lives in infra.notifier.notify)
  - Send the match Telegram (lives in infra.notifier.notify in slim)
CALLED BY:
  - listener/vehicle_event_pipeline.py:emit_result_stage (Phase.111)
  - telegram_formatter/tests/test_composite_telegram.py
CALLS INTO:
  - infra.motion_visualization.render_motion_composite (lazy import)
  - infra.send_telegram.send_photo_with_caption (lazy import)
RELATED:
  - infra.motion_types.MovingObject — supplies bbox_per_frame + trajectory
  - infra.motion_visualization.render_motion_composite — the renderer
  - Plan §11.21 (composite motion-trail alert, 6B.79 + 6B.111)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from infra.cameras import display_name_for

log = logging.getLogger("telegram_formatter.composite_telegram")


@dataclass(frozen=True)
class CompositeTelegramInput:
    """All inputs needed to build the Vehicle-In-Motion Telegram body.

    The composite Telegram (Telegram #2 in the lead motion message stack — see
    PLAN.md §6B.111 + §6B.112) reports the vehicle-in-motion event with
    the cumulative pairwise-diff + bbox-overlay composite image attached.
    This is the "second telegram alert reporting vehicle in motion
    detected" per Note's spec (2026-08-21).

    Attributes:
        camera_name:      Camera identifier — either a code (e.g. "CAM1") or a
                          friendly name (e.g. "<FRIENDLY_NAME>"). The header
                          resolves it through infra.cameras.display_name_for
                          so the body reflects the registry's canonical
                          display name (Phase.167 §13.5 Commit 13).
        captured_at_iso:  ISO-8601 timestamp string (rendered EDT)
                          — when the camera fired, NOT when Telegram sent
        trajectory:       Sequence of position labels (e.g.
                          ["B2", "C3", "D4"]). Empty list = no motion
                          detected (composite Telegram won't fire).
        vision_summary:   Verbatim identification from Qwen3-VL
                          (e.g. "white Honda Civic sedan"). Empty if
                          vision failed. Surfaced as the "identified as"
                          line in the body.
        alert_id:         Optional alert identifier, kept ONLY for log
                          correlation. Phase.114 removed it from
                          the user-facing body (was diagnostic noise).
    """
    camera_name: str
    captured_at_iso: str
    trajectory: Sequence[str]
    vision_summary: str = ""
    alert_id: str | None = None


def _format_trajectory(trajectory: Sequence[str]) -> str:
    """Render the trajectory as `top-left → center → bottom-right`.

    Empty trajectory returns empty string (header line stays clean).
    A single-element trajectory returns just that element (no arrow).
    """
    if not trajectory:
        return ""
    return " → ".join(str(t) for t in trajectory)


def build_composite_telegram_body(input: CompositeTelegramInput) -> str:
    """Build the Vehicle-In-Motion Telegram body.

    Args:
        input: CompositeTelegramInput with camera + trajectory +
               vision summary.

    Returns:
        The Telegram body as a string. Caller is responsible for
        sending (this function does no I/O).

    Layout (Telegram #2 in the lead motion vehicle-in-motion message stack):
        🚗 <b>Vehicle in motion at {camera_name}</b>
           identified as: {vision_summary}    (omitted if empty)
           trajectory: {top-left → center → bot-right}
                                    (omitted if trajectory is empty)

        {captured_at}                       (omitted if empty)

    The header uses <b>Vehicle in motion at</b> per Note's spec
    (2026-08-21): the legacy "Motion trail at <camera>" framing was
    changed because the composite Telegram is the *vehicle-in-motion*
    alert, not a generic motion-trail visualization. The composite
    motion-trail image is the photo attachment, not the message body.

    Phase.114 (2026-08-25): Note — "the date and time the
    alert is sent at the end of the alert text." removed the
    [alert_id] prefix line (was diagnostic noise to the user) and
    removed the captured_at_iso from the header (was interrupting
    the word-flow) — moved it to a footer line at the end. We use
    the webhook event time, not the actual Telegram-send time, per
    Note's correction ("it is actually fine to leave it as the
    webhook time").
    """
    lines: list[str] = []
    # Phase.167 §13.5 (Commit 13): resolve the camera identifier via
    # the registry so the body reflects the canonical display name. The
    # caller may pass either a code (CAM1/FRONT) or a name; display_name_for
    # returns the spec.name. Falls back to the input string when the
    # identifier isn't in the registry (test fixtures, legacy callers).
    lines.append(f"🚗 <b>Vehicle in motion at {display_name_for(input.camera_name)}</b>")
    if input.vision_summary:
        lines.append(f"   identified as: {input.vision_summary}")
    traj_str = _format_trajectory(input.trajectory)
    if traj_str:
        lines.append(f"   trajectory: {traj_str}")
    if input.captured_at_iso:
        lines.append("")
        lines.append(input.captured_at_iso)
    return "\n".join(lines)


def send_composite_alert(
    alert_id: str,
    camera_name: str,
    frames: list,
    bbox_a: tuple[int, int, int, int] | None,
    bbox_b: tuple[int, int, int, int] | None,
    bot_token: str,
    chat_id: str,
    captured_at: str | None,
    output_dir: str | None = None,
    trajectory: list[str] | None = None,
    vision_summary: str = "",
) -> bool:
    """Render the motion-trail composite + send the Vehicle-In-Motion Telegram.

    Phase.115 (§11.46.6, 2026-08-25):
        - `frames` is a list of 4 in-memory PIL.Image.Image objects from
          the motion gate's verdict. No filesystem read on the hot path.
        - `output_dir` is where composite.jpg gets written (Telegram needs
          a file path; we still write the composite to disk).
        - bbox_a + bbox_b are the motion-gate's diff bboxes in native
          frame coordinates.
        - trajectory is passed separately.

    Skips silently (returns False) if:
      - bbox_a is None AND bbox_b is None (no motion bboxes from gate)
      - render_motion_composite returns '' (render failure)
      - the rendered JPEG doesn't exist on disk
      - bot_token or chat_id is empty (no creds)
      - output_dir is missing (can't write composite)

    Any exception during render or send is caught + logged + returns False.
    The composite Telegram is enrichment — its failure must never block
    or suppress the lead motion Telegram that already went out.

    Args:
        alert_id:           For log lines.
        camera_name:        For body header.
        frames:             List of 4 PIL.Image.Image frames (from gate verdict).
        bbox_a:             Gate's diff(frame_2, frame_3) bbox in native coords,
                            or None if no motion.
        bbox_b:             Gate's diff(frame_3, frame_4) bbox in native coords,
                            or None if no motion.
        bot_token:          Telegram bot token (or empty to skip).
        chat_id:            Telegram chat ID (or empty to skip).
        captured_at:        ISO timestamp for the body (or None).
        output_dir:         Directory to write composite.jpg into (Telegram
                            attachment requires a file path).
        trajectory:         Optional 4-cell trajectory for body.
        vision_summary:     Optional vision summary line.

    Returns:
        True on successful Telegram send, False on any skip or failure.
    """
    # Guard 1: do we have at least one motion bbox from the gate?
    if bbox_a is None and bbox_b is None:
        log.info(
            f"[{alert_id}] composite_alert: skipped "
            f"(no gate bboxes)"
        )
        return False

    # Guard 2: do we have Telegram creds?
    if not bot_token or not chat_id:
        log.warning(
            f"[{alert_id}] composite_alert NOT sent "
            f"(no telegram creds)"
        )
        return False

    # Guard 3: do we have an output_dir for the composite?
    if not output_dir:
        log.warning(
            f"[{alert_id}] composite_alert NOT sent "
            f"(no output_dir)"
        )
        return False

    # Render the composite (lazy import — heavy cv2/numpy dep).
    try:
        from infra.motion_visualization import render_motion_composite
        composite_path = render_motion_composite(
            frames=frames,
            bbox_a=bbox_a,
            bbox_b=bbox_b,
            output_dir=output_dir,
        )
    except Exception as e:  # noqa: BLE001 — defensive catch
        log.warning(
            f"[{alert_id}] composite_alert: render failed {e!r}, "
            f"falling back to no composite"
        )
        return False

    # Guard 3: did the render produce a real file?
    if not composite_path or not os.path.isfile(composite_path):
        log.warning(
            f"[{alert_id}] composite_alert: render returned "
            f"no path, skipping"
        )
        return False

    # Build the body (pure function).
    body = build_composite_telegram_body(CompositeTelegramInput(
        camera_name=camera_name,
        captured_at_iso=captured_at or "",
        trajectory=trajectory or [],
        vision_summary=vision_summary,
        alert_id=alert_id,
    ))

    # Send.
    try:
        from infra.send_telegram import (
            send_photo_with_caption as _tg_send_photo,
        )
        photo_ok = bool(_tg_send_photo(
            bot_token, chat_id, composite_path, body,
            alert_id=alert_id,
            channel="gatekeeper_motion",
            event="vehicle_motion",
        ))
    except Exception as e:  # noqa: BLE001 — defensive catch
        # Telegram HTTP send can raise any of: requests.exceptions.*,
        # urllib3 errors, timeouts, JSON parse errors, transient HTTP
        # failures. Catch broadly so the composite-send failure never
        # escapes and breaks the alert pipeline.
        log.warning(
            f"[{alert_id}] composite_alert: send failed {e!r}"
        )
        return False

    if photo_ok:
        traj_str = " → ".join(trajectory or [])
        log.info(
            f"[{alert_id}] composite_alert: sent "
            f"path={composite_path} size_kb={os.path.getsize(composite_path) // 1024} "
            f"trajectory={traj_str}"
        )
        return True

    log.warning(
        f"[{alert_id}] composite_alert NOT sent (send returned falsy)"
    )
    return False