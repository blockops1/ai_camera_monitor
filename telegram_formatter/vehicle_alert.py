"""
vehicle_alert.py — Telegram body format helpers for vehicle-alert-specific
sections (Qwen identification lines, detector metadata lines, per-vehicle
motion lines, annotated frame bboxes, Qwen dict-to-lines renderer).

STATUS: stable
THREAD SAFETY: thread-safe (all functions are pure except annotate_frame_bboxes
    which touches the filesystem; the filesystem ops are best-effort and
    failure-isolated)

INPUTS:
    - format_qwen_confidence_line: vision_result dict (optional)
    - format_detector_metadata_lines: motion_result (MotionResult or None)
    - format_motion_alert_vehicle_line: idx (int), vehicle dict, vision_result dict
    - render_qwen_dict_lines: any object (only dicts produce output), indent (int),
      skip_keys (frozenset)
    - annotate_frame_bboxes: frame_paths (list of JPEG paths), moving_object
      (MovingObject or None)

OUTPUTS:
    - return values: strings (single line) or list[str] (multiple lines)
    - side effects (annotate_frame_bboxes only): writes
      `<frame_dir>/annotated_<basename>.jpg` for each successful annotation

PUBLIC API:
    format_qwen_confidence_line(vision_result=None) -> str
        Render the Qwen confidence line for the OFS lead motion Telegram.
        Returns either `   qwen confidence: 0.62` or `   qwen confidence: (empty)`.
    format_detector_metadata_lines(motion_result=None) -> list[str]
        Render the detector metadata section as a list of indented labeled
        strings. One line per non-zero/non-empty MotionResult field.
    format_motion_alert_vehicle_line(idx, vehicle, vision_result) -> str
        Render Qwen's identification verbatim for a single vehicle. Uses the
        free-text `description` if present, falls back to structured fields
        (color, body_style_hint, make, model) — never fabricates.
    render_qwen_dict_lines(obj, indent=6, skip_keys=QWEN_OUTPUT_SKIP_KEYS) -> list[str]
        Generic Qwen-dict-to-lines renderer. Walks every key, emits a body line
        per non-null/non-empty key. Nested dicts expand recursively; lists
        render inline as JSON-ish; long free-text fields wrap at 70 cols.
    annotate_frame_bboxes(frame_paths, moving_object=None) -> list[str]
        Draw the detector's per-frame bboxes onto the captured frames.
        Annotated copies go to `<frame_dir>/annotated_<basename>.jpg`;
        originals untouched. Returns paths parallel to frame_paths (annotated
        or original fallback per-frame).
    MATCHER_OUTPUT_SKIP_KEYS: frozenset
        Keys the matcher injects into vision_result that must NOT leak into
        the Motion Telegram body (they would leak the matcher's verdict).
    QWEN_OUTPUT_SKIP_KEYS: frozenset
        Back-compat alias for MATCHER_OUTPUT_SKIP_KEYS.

DOES NOT DO:
    - Send the Telegram → that's infra.notifier.notify
    - Build the full alert body → that's telegram_formatter.motion_telegram /
      match_telegram / no_match_telegram
    - Load the vision_result → callers pass it in
    - Decide which vehicle fields are "interesting" → emit everything Qwen
      returns (except matcher-injected fields) per Note 2026-08-11

WHY HERE:
    Phase.106 extraction (2026-08-21). These helpers lived at the top of
    listener.py (L1804-L2187) because they were created before the
    module-purity review was applied to listener.py. They were dead code
    in the slim listener.py post-6B.105c (the production pipeline never
    called them), but preserved verbatim through the 6B.105b/105c slims
    because those phases focused on _process_alert, not these helpers.
    Their only live callers are scripts/probe_enriched_alert.py and the
    existing infra/tests/. They belong here because telegram_formatter/
    already owns the other Telegram body builders (motion_telegram,
    match_telegram, no_match_telegram); these are the same kind of work.

    Functions renamed from `_format_*` / `_render_*` / `_annotate_*`
    (underscore-prefixed because they were listener-private symbols) to
    public names (no underscore) because the module is now part of
    telegram_formatter/'s public API. Same rename pattern as 6B.105c's
    `_send_arriving_message` and 6B.108's `_MatcherFailureTracker`.

CALLED BY:
    - scripts/probe_enriched_alert.py — ad-hoc operator probe
    - infra/tests/test_format_qwen_confidence.py
    - infra/tests/test_format_detector_metadata.py
    - infra/tests/test_annotate_frame_bboxes.py

CALLS INTO:
    - json: render_qwen_dict_lines uses json.dumps for list items
    - textwrap: render_qwen_dict_lines wraps long free-text fields
    - os.path: annotate_frame_bboxes builds the annotated path
    - infra.motion_visualization._project_bbox: annotate_frame_bboxes projects
      bbox coordinates to the frame resolution
    - cv2: annotate_frame_bboxes reads/writes JPEGs (lazy import to keep
      module-level surface clean)
"""
from __future__ import annotations

import json
import logging
import os
import textwrap
from typing import Any

# Module-specific logger so log lines tag as [vehicle_alert] not [listener].
log = logging.getLogger(__name__)


def format_qwen_confidence_line(vision_result=None) -> str:
    """
    Phase.81 (PLAN.md §11.14.3.B): render the Qwen confidence line for
    the OFS lead motion Telegram. Reads `vision_result["confidence"]`
    (a float 0.0-1.0) and formats it as either `   qwen confidence: 0.62`
    (2-decimal float) or `   qwen confidence: (empty)` (when missing).

    The empty case is explicit so Note can distinguish "Qwen returned
    no confidence value" from "Qwen is confident the scene is empty" —
    two operationally different states.

    Per §11.14.3.B this is a SCOPED change to the lead motion alert only;
    it does NOT modify `_render_qwen_dict_lines` so other call sites
    (match, no-match alerts) keep their existing behavior.

    Args:
        vision_result: the vision result dict from Qwen's
            `analyze_frames_queued` or `identify_from_crops`. May be None.

    Returns:
        A single indented string, ready to append to the alert body.
    """
    if not isinstance(vision_result, dict):
        return "   qwen confidence: (empty)"
    _conf = vision_result.get("confidence")
    if isinstance(_conf, (int, float)) and _conf > 0:
        return f"   qwen confidence: {float(_conf):.2f}"
    return "   qwen confidence: (empty)"


def format_detector_metadata_lines(motion_result=None) -> list:
    """
    Phase.81 (PLAN.md §11.14): render the detector metadata section of
    the OFS lead motion Telegram. Surfaces the motion detector's structured
    output as labeled lines so the alert body still has useful information
    when Qwen's vision call returns empty fields.

    Lines are emitted only for non-zero / non-empty fields so the section
    is silent when the detector found nothing. The leading 3-space indent
    matches the existing alert body lines emitted by `_send_motion_alert`.

    Args:
        motion_result: a `MotionResult` from `infra.motion_detector`. May
            be None (e.g., older code paths or tests).

    Returns:
        A list of indented labeled strings, one per non-zero field. Empty
        list when motion_result is None or has no populated fields.
    """
    # Phase.81: the burst is 6 frames (motion_detector docstring
    # line 10: "one capture batch (typically 6 frames)"). motion_detector
    # does not export a BURST_FRAME_COUNT constant; this local matches
    # its hardcoded `present_in_frame` length.
    _BURST_FRAME_COUNT = 6
    lines: list[str] = []
    if motion_result is None:
        return lines

    # MotionResult-level fields
    if motion_result.total_motion_pixels:
        lines.append(f"   detector total_motion_px: {motion_result.total_motion_pixels}")
    if motion_result.reference_method:
        lines.append(f"   detector reference_method: {motion_result.reference_method}")
    if motion_result.elapsed_ms:
        lines.append(f"   detector elapsed_ms: {motion_result.elapsed_ms}")

    # Primary MovingObject fields (when present)
    primary = motion_result.primary_moving_object
    if primary is not None:
        if primary.avg_area:
            lines.append(f"   detector object avg_area: {primary.avg_area}")
        if primary.frames_seen:
            lines.append(
                f"   detector object frames_seen: {primary.frames_seen}/{_BURST_FRAME_COUNT}"
            )
        if primary.position_change_max:
            lines.append(
                f"   detector object position_change_max: {primary.position_change_max} px"
            )
    return lines


def format_motion_alert_vehicle_line(
    idx: int,
    vehicle: dict,
    vision_result: dict,
) -> str:
    """Phase.77 (2026-08-11) — render Qwen's identification verbatim.

    Replaces the 6B.76 implementation that read
    `vision_result["identified_label"]` (which is the MATCHER's output
    and produced wrong labels like "Jayco Jay Feather travel trailer"
    for name two's white pickup truck when the matcher wrongly scored
    it against v_jayco_camper at 3.20). The Motion Telegram body
    must NEVER read the matcher's label — that's the 2nd Telegram's
    job. This helper renders Qwen's identification output verbatim:
    the free-text `description` field on the "1. ..." line, then the
    structured fields Qwen returned, then the full vehicle_features
    dict.

    Source: Note 2026-08-11: "what I want sent in the motion
    telegram is the actual vision model output on the identification
    of the vehicle".

    Args:
        idx: 1-based vehicle index in the alert.
        vehicle: One Qwen vision_result["vehicles"][i] dict.
        vision_result: Full vision result (kept for signature
            compatibility, not read here).

    Returns:
        A single formatted line, e.g.
        "   1. A white pickup truck with chrome horizontal grille
              bars and black steel wheels, mid-frame."
        or, when Qwen didn't return a description (fallback):
        "   1. white pickup, GMC Sierra 1500"
    """
    # Use the vehicle dict Qwen returned, NOT the matcher's label.
    # The Motion Telegram body is the vision model's output, not the
    # matcher's interpretation.
    motion = (vehicle.get("motion") or "").strip()
    motion_suffix = f" — {motion}" if motion else ""

    desc = (vehicle.get("description") or "").strip()

    if desc:
        return f"   {idx}. {desc}{motion_suffix}"

    # Fallback: Qwen didn't return a description. Build from the
    # structured fields so we never render an empty "1. ..." line.
    # No fabrication — just concatenation of what Qwen returned.
    color = (vehicle.get("color") or "").strip()
    bsh = (vehicle.get("body_style_hint") or "").strip()
    make = (vehicle.get("make") or "").strip()
    model = (vehicle.get("model") or "").strip()

    # Build segments in priority order. The natural English
    # description starts with color + body style, then make/model.
    # Without color/bsh, we say the make/model alone. Without
    # anything, "vehicle" alone.
    segments: list = []
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

    if not segments:
        base = "vehicle"
    else:
        base = ", ".join(segments)

    return f"   {idx}. {base}{motion_suffix}"


# Phase.78 (2026-08-11) — generic Qwen-dict renderer.
#
# Note 2026-08-11: "when I tell you that I want all the output of
# the identifier vision model output sent to me in the telegram that's
# actually what I mean. It's not up to you to interpret my request
# when I tell you I want something very specific."
#
# Bug: previous code curated a hardcoded key list for the Motion
# Telegram body (color/body_style_hint/make/model + a hardcoded 12-key
# vehicle_features list). Anything Qwen returned BEYOND that curated
# set was silently dropped. Same problem in _send_no_match_alert.
#
# Fix: this helper walks whatever dict Qwen produced and emits a line
# per non-null/non-empty key. Nested dicts (like vehicle_features) get
# expanded recursively with extra indent. Lists render as one item per
# line. No key is ever filtered out — if Qwen added a field tomorrow,
# it shows up.
#
# Fields the user explicitly does NOT want rendered:
#   - frame_positions: trajectory lives in the Motion Telegram body
#     at the top level already ("frame trajectory: T1 → ..."), not as
#     a per-vehicle row. Keeping it out avoids double-rendering.
#   - motion: already rendered into the "1. ..." line via
# Phase.78 (2026-08-11) — generic Qwen-dict renderer.
#
# Note 2026-08-11: "when I tell you that I want all the output
# of the identifier vision model output sent to me in the telegram
# that's actually what I mean. It's not up to you to interpret my
# request when I tell you I want something very specific."
#
# Walks every key Qwen returned and emits a body line for each.
# No curated whitelist, no truncation. The only keys skipped are
# fields that the matcher injects into vision_result (which would
# leak the matcher's verdict into the Motion Telegram body) — those
# are NOT Qwen's output and must never appear here.
#
# Earlier 6B.78 draft incorrectly added "confidence" to the skip list
# because I conflated Qwen's confidence (make/model certainty) with
# the matcher's confidence (signature-vs-known-vehicle score). They
# are different fields with different sources. Qwen's confidence is
# legitimate output and must render.
_MATCHER_OUTPUT_SKIP_KEYS = frozenset({
    # Fields the matcher injects into vision_result. NOT Qwen output —
    # never leak the matcher's verdict into the Motion Telegram body.
    "identified_label", "identified_owner", "identified",
    "identification_confidence", "identification_crops_used",
    "identification_fallback",
    # Matcher-internal bookkeeping attached to the vehicle dict.
    "kv_id", "label", "owner",
    "signature", "breakdown",
    "vision_classification",
    # Internal pipeline bookkeeping.
    "best_crop_path", "crops_used", "fallback_used", "elapsed_ms",
    "frame_positions", "motion",
})
# Back-compat alias for older imports / tests.
_QWEN_OUTPUT_SKIP_KEYS = _MATCHER_OUTPUT_SKIP_KEYS


def render_qwen_dict_lines(
    obj: Any,
    indent: int = 6,
    skip_keys: frozenset = _QWEN_OUTPUT_SKIP_KEYS,
) -> list[str]:
    """Render any Qwen dict as Telegram body lines.

    Walks every key in dict-insertion order. For each value:
      - None or empty string or "null" → skipped
      - dict → recursive: each child key on its own indented line
      - list → one item per line, "key: [item]" (or "key:" + newline
        then each item on its own indented line for clarity)
      - bool → "key: true" / "key: false"
      - number → "key: N"
      - string → "key: value"

    Returns a list of lines (no trailing newline).
    """
    lines: list[str] = []
    if not isinstance(obj, dict):
        return lines
    pad = " " * indent
    for k, v in obj.items():
        if k in skip_keys:
            continue
        if v is None or v == "" or v == "null":
            continue
        if isinstance(v, dict):
            if not v:
                continue
            lines.append(f"{pad}{k}:")
            sub = render_qwen_dict_lines(v, indent=indent + 3, skip_keys=skip_keys)
            lines.extend(sub)
        elif isinstance(v, list):
            if not v:
                continue
            # Show list inline as a single line so the Telegram stays compact.
            # JSON-ish: key: [a, b, c] for short lists; multi-line for long.
            try:
                items_repr = ", ".join(
                    json.dumps(x) if not isinstance(x, (str, int, float, bool)) else (
                        f'"{x}"' if isinstance(x, str) else str(x)
                    )
                    for x in v
                )
                lines.append(f"{pad}{k}: [{items_repr}]")
            except (TypeError, ValueError):
                # List item wasn't JSON-serializable (custom object, bytes,
                # etc.) — fall back to a placeholder so the alert body still
                # ships. The actual exception is logged so operators can
                # investigate which Qwen output triggered it.
                log.warning(
                    "render_qwen_dict_lines: could not format list value for "
                    "key=%r (len=%d); using placeholder", k, len(v)
                )
                lines.append(f"{pad}{k}: <list len={len(v)}>")
        elif isinstance(v, bool):
            lines.append(f"{pad}{k}: {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{pad}{k}: {v}")
        else:
            s = str(v).strip()
            if s:
                # Wrap long free-text values so they stay readable on phone screens.
                if len(s) > 80 and k in ("description", "scene_description", "primary_subject"):
                    wrapped = textwrap.wrap(s, width=70)
                    lines.append(f"{pad}{k}:")
                    for chunk in wrapped:
                        lines.append(f"{' ' * (indent + 3)}{chunk}")
                else:
                    lines.append(f"{pad}{k}: {s}")
    return lines


def annotate_frame_bboxes(
    frame_paths: list,
    moving_object=None,
) -> list:
    """
    Phase.81 (PLAN.md §11.14): draw the detector's per-frame bboxes onto
    the captured frames that accompany the OFS lead motion Telegram. The
    annotated copies go into `<frame_dir>/annotated_<basename>.jpg`; the
    originals are left untouched. The lead motion Telegram's
    `sendMediaGroup` call is updated to send the annotated paths so the
    green outlines land in the user's Telegram.

    Per-frame fallback: if `moving_object` is None, or the bbox for a given
    frame is empty/inverse, or cv2 cannot read or write the frame, the
    ORIGINAL frame path is returned for that index and a WARNING is logged.
    Telegram delivery never fails because of an annotation problem.

    Args:
        frame_paths: list of JPEG file paths (the 6 captured frames).
        moving_object: the primary `MovingObject` whose `bbox_per_frame`
            list aligns 1:1 with `frame_paths`. May be None.

    Returns:
        A list of paths parallel to `frame_paths`. Each entry is either
        the annotated copy (success) or the original path (fallback).
    """
    # Lazy imports — keep module-level surface clean and avoid importing
    # cv2 at every listener boot. Matches the existing pattern at line
    # 2933 for `infra.motion_visualization`.
    import cv2 as _cv2

    from infra.motion_visualization import _project_bbox as _mv_project_bbox

    out_paths: list[str] = []
    for i, fp in enumerate(frame_paths):
        annotated = fp  # default fallback
        try:
            # Phase.86 (PLAN.md §11.18): bbox_per_frame[i+1] describes
            # the diff between frames i and i+1; the "departure" region
            # of that diff is the moving object's position on frame_paths[i].
            # So we draw bbox_per_frame[i+1] on frame_paths[i] (the
            # previous image, kept on disk).
            bbox = None
            if (
                moving_object is not None
                and moving_object.bbox_per_frame
                and (i + 1) < len(moving_object.bbox_per_frame)
            ):
                bbox = moving_object.bbox_per_frame[i + 1]
            if not bbox or len(bbox) != 4:
                out_paths.append(annotated)
                continue

            img = _cv2.imread(fp)
            if img is None:
                log.warning(
                    f"_annotate_frame_bboxes: cv2.imread returned None "
                    f"for {fp} (frame {i}); using original frame"
                )
                out_paths.append(annotated)
                continue

            orig_h, orig_w = img.shape[:2]
            projected = _mv_project_bbox(bbox, orig_w, orig_h)
            if projected is None:
                out_paths.append(annotated)
                continue

            px0, py0, px1, py1 = projected
            thickness = max(2, orig_h // 600)
            # BGR green outline (0, 255, 0). Matches the bbox style used by
            # infra.motion_visualization.render_motion_composite.
            _cv2.rectangle(img, (px0, py0), (px1, py1), (0, 255, 0), thickness)

            annotated_dir = os.path.dirname(fp) or "."
            annotated_basename = "annotated_" + os.path.basename(fp)
            annotated_path = os.path.join(annotated_dir, annotated_basename)
            ok = _cv2.imwrite(
                annotated_path, img, [_cv2.IMWRITE_JPEG_QUALITY, 90]
            )
            if not ok:
                log.warning(
                    f"_annotate_frame_bboxes: cv2.imwrite failed for "
                    f"{annotated_path} (frame {i}); using original frame"
                )
                out_paths.append(fp)
                continue

            out_paths.append(annotated_path)
        except Exception as err:  # noqa: BLE001
            # Defensive: annotation must never break the alert pipeline.
            # If anything fails (cv2 read/write, filesystem ops, projection
            # math, unexpected value shapes, cv2.error which doesn't derive
            # from OSError), fall back to the original frame and log the
            # specific error class + message. The Telegram still sends
            # with the un-annotated frame.
            log.warning(
                f"annotate_frame_bboxes: unexpected error annotating "
                f"frame {i} ({fp}): {type(err).__name__}: {err}; "
                f"using original frame"
            )
            out_paths.append(fp)
    return out_paths
