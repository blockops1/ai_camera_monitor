"""Motion Telegram body.

The first Telegram sent for an alert. Two output formats:

* ``build_motion_telegram_body`` — full diagnostic body with position
  section + every Qwen field. Used by legacy call sites that want the
  complete vision output surfaced in the message body.
* ``build_minimal_motion_telegram_body`` — single-frame, single-line
  identification + confidence. Phase.89 (PLAN.md §11.20) introduced
  this for the OFS lead motion Telegram per Note's "just one picture
  + identification + color + confidence, nothing else" instruction.

Pure functions. No I/O. No timezones inferred.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .render_qwen import render_qwen_dict_lines


@dataclass(frozen=True)
class MotionTelegramInput:
    """All inputs needed to build a Motion Telegram body.

    Attributes:
        camera_name:           e.g. "Outside Front Solar"
        captured_at_iso:       ISO-8601 timestamp string from the camera
        trajectory:            Sequence of position labels (e.g. ["B2","B2","B3"]).
                               Empty list if no motion detected.
        avg_area:              Average area of the primary moving object in pixels.
                               0 if no motion.
        vision_result:         Qwen's full structured response dict (or None
                               if vision was skipped / failed).
        crop_paths:            List of crop image paths (informational only —
                               the body itself doesn't embed images).
        alert_id:              Optional alert identifier, kept ONLY for log
                               correlation. Phase.114 removed it from
                               the user-facing body (was diagnostic noise).
    """
    camera_name: str
    captured_at_iso: str
    trajectory: Sequence[str]
    avg_area: int
    vision_result: dict[str, Any] | None
    crop_paths: list[str] = None  # type: ignore[assignment]
    alert_id: str | None = None

    def __post_init__(self) -> None:
        if self.crop_paths is None:
            # frozen dataclass: need object.__setattr__ to swap default
            object.__setattr__(self, "crop_paths", [])


def _format_position_section(trajectory: Sequence[str], avg_area: int) -> str:
    """Render the position block."""
    if not trajectory or all(t == "absent" for t in trajectory):
        return "Position: (no motion detected)"
    seen = [t for t in trajectory if t and t != "absent"]
    if not seen:
        return "Position: (no motion detected)"
    first = seen[0]
    last = seen[-1]
    if first == last:
        path = first
    else:
        path = f"{first} → {last}"
    return (
        f"Position: {path}\n"
        f"Avg area: {avg_area:,} px"
    )


def _format_header(input: MotionTelegramInput) -> str:
    """Header line: camera name only. No alert_id, no timestamp (Phase.114).

    The captured_at_iso goes in a footer line at the end of the body
    (per Note — "the date and time the alert is sent at the end
    of the alert text"). Putting it in the header interrupted the
    word-flow of the body.
    """
    return f"🚗 Motion — {input.camera_name}"


def build_motion_telegram_body(input: MotionTelegramInput) -> str:
    """Build the Motion Telegram body.

    Args:
        input: MotionTelegramInput with all the structured data.

    Returns:
        The Telegram body as a string. Caller is responsible for
        sending (this function does no I/O).

    Layout:
        <header>                  ← 🚗 Motion — <camera> (<event_time>)
        <blank>
        <position section>
        <blank>
        <identifier section — Qwen's full structured response>
        <blank>
        <event_time>             (omitted if empty)

    Phase.114 (2026-08-25): Removed [alert_id] prefix from header
    (diagnostic noise to the user). Added the captured_at webhook
    time as a footer line at the end of the body so it doesn't
    interrupt the word-flow. Event time, not send time, per Note
    OOB correction ("it is actually fine to leave it as the
    webhook time").
    """
    lines: list[str] = []

    # Header.
    lines.append(_format_header(input))

    # Position section.
    lines.append("")
    lines.append(_format_position_section(
        input.trajectory, input.avg_area,
    ))
    if input.crop_paths:
        lines.append("")
        lines.append(f"Crops ({len(input.crop_paths)}):")
        for p in input.crop_paths:
            lines.append(f"  {p}")

    # Identifier section (Qwen's full structured response).
    lines.append("")
    lines.append("Identifier:")
    if input.vision_result is None:
        lines.append("  (no vision result)")
    else:
        # render_qwen_dict_lines handles nested dicts, lists, wrapping.
        # Indent 2 to set off the identifier section.
        rendered = render_qwen_dict_lines(input.vision_result, indent=2)
        if rendered:
            lines.extend(rendered)
        else:
            lines.append("  (vision result was empty)")

    # Footer — webhook event time (Phase.114).
    if input.captured_at_iso:
        lines.append("")
        lines.append(input.captured_at_iso)

    return "\n".join(lines)


# --- minimal body (Phase.89, PLAN.md §11.20) ---------------------------


def _build_vehicle_description(vehicle: dict[str, Any]) -> str:
    """Compose a single-line vehicle description from Qwen's structured output.

    Priority order matches ``format_motion_alert_vehicle_line`` (Phase.77):
    use the free-text ``description`` field if Qwen returned one; otherwise
    concatenate color + body_style_hint + make + model. No fabrication —
    just emit what Qwen returned.

    Args:
        vehicle: One Qwen ``vision_result["vehicles"][i]`` dict.

    Returns:
        A non-empty description string. Falls back to ``"vehicle"`` if
        every structured field is empty.
    """
    desc = (vehicle.get("description") or "").strip()
    if desc:
        return desc

    color = (vehicle.get("color") or "").strip()
    bsh = (vehicle.get("body_style_hint") or "").strip()
    make = (vehicle.get("make") or "").strip()
    model = (vehicle.get("model") or "").strip()

    segments: list[str] = []
    if color and bsh:
        segments.append(f"{color} {bsh}")
    elif color:
        segments.append(color)
    elif bsh:
        segments.append(bsh)

    if make and model:
        segments.append(f"{make} {model}")
    elif make:
        segments.append(make)
    elif model:
        segments.append(model)

    return ", ".join(segments) if segments else "vehicle"


def _format_qwen_confidence_str(vision_result: dict[str, Any] | None) -> str:
    """Render Qwen's confidence as ``"(confidence: 0.95)"`` or ``"(no confidence)"``.

    Suffix-style (not a header line) so it can be appended to the
    vehicle description on a single line. Distinct from
    ``format_qwen_confidence_line`` which renders an indented
    header-style line — we want a tighter shape for the minimal alert.

    Args:
        vision_result: Full vision result dict, or None when vision was
            skipped / failed.

    Returns:
        Suffix string suitable for appending to the vehicle line.
    """
    if not isinstance(vision_result, dict):
        return "(no confidence)"
    conf = vision_result.get("confidence")
    if isinstance(conf, (int, float)) and conf > 0:
        return f"(confidence: {float(conf):.2f})"
    return "(no confidence)"


def build_minimal_motion_telegram_body(
    input: MotionTelegramInput,
    vehicle_idx: int = 0,
) -> str:
    """Build the minimal OFS Motion Telegram body (Phase.89).

    Three lines, no diagnostics, no full Qwen dump:

    1. ``🚗 Motion — {camera_name}``
    2. ``{captured_at_iso}``
    3. ``{idx}. {vehicle_description} {confidence_suffix}``

    The caller picks which frame of the burst to attach as the
    Telegram photo (default: the 4th frame). This body intentionally
    does NOT mention frame numbers, detector metadata, trajectories,
    crop paths, or the position section — those are operator-debug
    fields that cluttered the alert per Note's 2026-08-18 instruction.

    Args:
        input: MotionTelegramInput. ``vision_result`` is required for
            a useful body — when None the body still renders with
            ``(no vehicle description)`` + ``(no confidence)`` so the
            caller can send the alert without crashing.
        vehicle_idx: Zero-based index into ``vision_result["vehicles"]``.
            Defaults to 0 (first / only vehicle). Out-of-range indices
            render the empty-vehicle fallback.

    Returns:
        The Telegram body as a string.
    """
    lines: list[str] = []
    lines.append(f"🚗 <b>Motion — {input.camera_name}</b>")
    if input.captured_at_iso:
        lines.append(input.captured_at_iso)

    vehicles = (
        input.vision_result.get("vehicles", [])
        if isinstance(input.vision_result, dict)
        else []
    )
    if vehicles and 0 <= vehicle_idx < len(vehicles):
        vehicle = vehicles[vehicle_idx]
        desc = _build_vehicle_description(vehicle)
        conf = _format_qwen_confidence_str(input.vision_result)
        lines.append(f"{vehicle_idx + 1}. {desc} {conf}")
    else:
        lines.append("(no vehicle description)")

    return "\n".join(lines)
