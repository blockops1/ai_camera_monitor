"""
_format_helpers_archive_6B106.py — verbatim snapshot of 5 Telegram body
formatting helpers and 2 module-level constants, extracted from
listener.py on 2026-08-21 (Phase.106) before they were moved to
telegram_formatter/vehicle_alert.py:

  - _format_qwen_confidence_line      → telegram_formatter.vehicle_alert.format_qwen_confidence_line
  - _format_detector_metadata_lines   → telegram_formatter.vehicle_alert.format_detector_metadata_lines
  - _format_motion_alert_vehicle_line → telegram_formatter.vehicle_alert.format_motion_alert_vehicle_line
  - _render_qwen_dict_lines           → telegram_formatter.vehicle_alert.render_qwen_dict_lines
  - _annotate_frame_bboxes            → telegram_formatter.vehicle_alert.annotate_frame_bboxes
  - _MATCHER_OUTPUT_SKIP_KEYS         → telegram_formatter.vehicle_alert.MATCHER_OUTPUT_SKIP_KEYS
  - _QWEN_OUTPUT_SKIP_KEYS           → telegram_formatter.vehicle_alert.QWEN_OUTPUT_SKIP_KEYS
                                         (kept as back-compat alias)

These helpers were dead code in the slim listener.py post-6B.105c — the
production pipeline never called them. They lived at the top of listener.py
for historical reasons (pre-6B.105b the in-line _process_alert called them)
and were preserved verbatim through the 6B.105b/105c slims because the
slims focused on _process_alert, not on these helpers. 6B.106 is the
proper extraction.

The archive preserves them as a verbatim snapshot for rollback / diff
archaeology. Per archive-first-workflow, the archive must be written BEFORE
the slim is committed.

Used by:
  - scripts/probe_enriched_alert.py (operator ad-hoc probe)
  - infra/tests/test_format_qwen_confidence.py
  - infra/tests/test_format_detector_metadata.py
  - infra/tests/test_annotate_frame_bboxes.py

=== ORIGINAL listener.py lines 1804-2187 (Phase.106 archive) ===
def _format_qwen_confidence_line(vision_result=None) -> str:
    """
    Phase.81 (PLAN.md §11.14.3.B): render the Qwen confidence line for
    the CAM1 lead motion Telegram. Reads `vision_result["confidence"]`
    (a float 0.0-1.0) and formats it as either `   qwen confidence: 0.62`
    (2-decimal float) or `   qwen confidence: (empty)` (when missing).

    The empty case is explicit so the operator can distinguish "Qwen returned
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


def _format_detector_metadata_lines(motion_result=None) -> list:
    """
    Phase.81 (PLAN.md §11.14): render the detector metadata section of
    the CAM1 lead motion Telegram. Surfaces the motion detector's structured
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


def _format_motion_alert_vehicle_line(
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


def _render_qwen_dict_lines(
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
            sub = _render_qwen_dict_lines(v, indent=indent + 3, skip_keys=skip_keys)
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
            except Exception:
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


def _annotate_frame_bboxes(
    frame_paths: list,
    moving_object=None,
) -> list:
    """
    Phase.81 (PLAN.md §11.14): draw the detector's per-frame bboxes onto
    the captured frames that accompany the CAM1 lead motion Telegram. The
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
        except Exception as err:
            log.warning(
                f"_annotate_frame_bboxes: unexpected error annotating "
                f"frame {i} ({fp}): {err}; using original frame"
            )
            out_paths.append(fp)
    return out_paths
