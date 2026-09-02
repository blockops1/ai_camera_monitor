"""
vision_response.py — Qwen3-VL response parsing + validation + error sentinels.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - function arg raw: str — raw response text from llama-server (content
      or reasoning_content field)
    - function arg result: dict — parsed JSON dict to validate
    - function arg reason: str — error description for error sentinel

OUTPUTS:
    - return value (_parse_response): dict | None — validated vision result
        or None on unrecoverable parse failure
    - return value (_parse_vehicle_classify_response): dict | None —
        validated crop-classify result with all required fields defaulted,
        or None on parse failure
    - return value (_validate_vision_result): dict | None — result with
        defaults filled (or None if not a dict)
    - return value (_try_recover_stringified_lists): str | None — repaired
        text or None if no recovery applicable
    - return value (_error_result, _vehicle_classify_error): dict — error
        sentinel with predictable shape

PUBLIC API:
    _parse_response(raw: str) -> dict | None
        Extract JSON from multi-frame analysis. Handles bare JSON, markdown
        code fences, and the stringified-list recovery path. Returns
        validated dict or None.
    _try_recover_stringified_lists(text: str) -> str | None
        Heuristic: when a JSON value is a string that contains commas +
        looks like multiple items, convert it to a JSON list. Returns
        repaired text or None.
    _validate_vision_result(result: dict) -> dict | None
        Validate + fill defaults. Returns the result if valid, None if not.
        Pulled out of _parse_response so both first-try and recovered
        paths can use it.
    _parse_vehicle_classify_response(raw: str) -> dict | None
        Extract JSON from classify_vehicle_crop response. Mirrors
        _parse_response but for the focused crop shape. Fills required
        fields with null defaults so the downstream caller can index
        into the result safely.
    _populate_legacy_fields_from_vehicles(result: dict) -> None
        Phase 6B.129b (§11.52) — copy vehicles[primary_vehicle_index]
        into top-level fields (color, make, model, body_style_hint,
        vehicle_features, description, confidence, type). Idempotent.
        Keeps legacy consumers (_extract_signature in vehicle_event_pipeline,
        alert body builders, _vision_summary_str fall-back) working without
        changes when Qwen returns the new multi-vehicle schema.
    _error_result(reason: str) -> dict
        Standard error sentinel for analyze_frames failures. Shape
        matches VISION_SCHEMA_JSON.
    _vehicle_classify_error(reason: str) -> dict
        Standard error sentinel for classify_vehicle_crop failures. Shape
        matches VEHICLE_CLASSIFY_SCHEMA. Adds "_error" field with the
        reason string.

DOES NOT DO:
    - HTTP transport — infra.vision_client owns that
    - Build prompts — infra.prompt_templates owns that
    - Decide whether to retry — infra.vision_analyzer owns that
    - Persist artifacts — infra.vision_analyzer handles vehicle_artifacts
    - Decide what fields mean — the parser fills defaults for fields
      the schema requires. It does not invent values for fields the
      schema doesn't ask for, and it does not warn when Qwen returns
      extra fields (Phase 6B.78). Motion is not this module's job.

WHY HERE:
    Qwen3-VL responses are unreliable. Phase 6B.66 (2026-08-08) shipped
    the cab_marker_lights "false"-as-string fix; Phase 6B.77 relaxed the
    dict/object requirement after a real production alert silently dropped
    3 crops' identification data. Every parse path needs to:
      1. Strip markdown fences if present
      2. Try json.loads first
      3. Fall back to stringified-list recovery if it fails
      4. Fill missing required fields with sensible defaults
      5. Return a predictable error sentinel if all paths fail

    The two parse functions (_parse_response for analyze_frames,
    _parse_vehicle_classify_response for classify_vehicle_crop) share
    80% of their structure but operate on different schemas. Keeping
    them together avoids duplicating the markdown-strip + json.loads
    pattern, while still letting each function fill the schema-specific
    defaults.

CALLED BY:
    - infra.vision_analyzer.analyze_frames (orchestrator) → _parse_response,
      _error_result
    - infra.vision_analyzer.classify_vehicle_crop (crop orchestrator) →
      _parse_vehicle_classify_response, _vehicle_classify_error

CALLS INTO:
    - json (stdlib)

RELATED:
    - infra/prompt_templates.py — owns VISION_SCHEMA_JSON and
      VEHICLE_CLASSIFY_SCHEMA. The default-filling logic here matches
      those schemas field-by-field. When adding a schema field, update
      _validate_vision_result's setdefault list in the same commit.
    - infra/vision_analyzer.py — orchestrator that calls into this module
"""

import json
import re


def _parse_response(raw: str) -> dict | None:
    """
    Extract JSON from model response.
    Handles:
      - Bare JSON: {"objects_detected": ...}
      - Markdown code block: ```json\n{...}\n```
    """
    if not raw:
        return None

    # Try stripping markdown code fences
    stripped = raw.strip()
    if stripped.startswith("```"):
        # Strip ```json and trailing ```
        lines = stripped.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        result = json.loads(stripped)
        return _validate_vision_result(result)
    except json.JSONDecodeError:
        pass

    # Fallback: the LLM sometimes puts a comma-separated list of strings
    # inside one quoted value (e.g. "other": "red jugs, white cooler").
    # Try to recover by splitting any string-valued field whose content looks
    # like a list of phrases, then re-parse.
    recovered = _try_recover_stringified_lists(stripped)
    if recovered is not None:
        try:
            result = json.loads(recovered)
            return _validate_vision_result(result)
        except json.JSONDecodeError:
            pass

    return None


def _try_recover_stringified_lists(text: str) -> str | None:
    """
    Heuristic: when a JSON value is a string that contains commas + looks like
    multiple items, convert it to a JSON list. We only touch string values that
    look like the LLM tried to encode a list inline.

    Specifically: "key": "a", "b", "c"   →   "key": ["a", "b", "c"]
    Also:       "key": "a, b, c"          →   "key": ["a", "b", "c"]

    Conservative: only acts when we can detect the obvious pattern without
    destroying real prose.
    """
    # Pattern 1: "key": "str1", "str2", "str3" (multiple quoted strings
    # following a key on the same line — LLM emitted them as separate
    # quoted scalars instead of an array).
    # The "rest" group must be on the same line (no newline) and must contain
    # only quoted strings separated by commas. We refuse to act if the rest
    # contains a closing brace/bracket or another JSON key, because those
    # indicate a single string followed by the next key, not a list.
    def _fix_multi_quoted(match: re.Match) -> str:
        key = match.group(1)
        first = match.group(2)
        rest_block = match.group(3)

        # Refuse if the rest contains any JSON structural char (would mean
        # it's actually the start of the next JSON value/key, not a list).
        if any(c in rest_block for c in "{}[]:"):
            return match.group(0)  # type: ignore[no-any-return]

        # The first string has its quotes stripped by the capture group;
        # the rest block contains the other quoted strings verbatim.
        # Combine: [first] + all quoted strings from rest.
        rest_strings = re.findall(r'"([^"]+)"', rest_block)
        all_strs = [first] + rest_strings

        # Heuristic: only apply if all parts look short (object-list-like).
        if all(0 < len(s) < 60 and ". " not in s for s in all_strs):
            inner = ", ".join(f'"{s}"' for s in all_strs)
            return f'"{key}": [{inner}]'
        return match.group(0)  # type: ignore[no-any-return]

    # Match a key followed by 1+ quoted strings separated by commas on
    # the same line. The lookahead restricts the trailing context to a
    # newline, comma+newline, or closing brace/bracket — NOT a colon
    # (which would mean the next key is starting, not more list items).
    text = re.sub(
        r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:\s*"([^"]+)"((?:\s*,\s*"[^"]+")+?)(?=\s*(?:,?\s*[}\]\n]))',
        _fix_multi_quoted,
        text,
    )

    # Pattern 2: a string value containing comma-separated phrases that look
    # like object names (short, no full sentences). Convert to a list.
    def _fix_string_value(match: re.Match) -> str:
        key = match.group(1)
        value = match.group(2)
        # Only act if the value contains 2+ commas and the parts are short
        # (likely list items, not prose).
        if value.count(",") >= 2:
            parts = [p.strip().strip('"').strip() for p in value.split(",")]
            # Heuristic: if every part is short (< 40 chars) and none
            # contains a period followed by space (sentence marker),
            # treat as a list.
            if all(0 < len(p) < 60 and ". " not in p for p in parts):
                quoted = ", ".join(f'"{p}"' for p in parts)
                return f'"{key}": [{quoted}]'
        return match.group(0)  # type: ignore[no-any-return]

    return re.sub(r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:\s*"([^"]+)"', _fix_string_value, text)


def _populate_legacy_fields_from_vehicles(result: dict) -> None:
    """Phase 6B.129b (§11.52) — populate top-level legacy fields from
    vehicles[primary_vehicle_index] so legacy consumers keep working.

    The crop prompt returns vehicles[] (one entry per visible vehicle).
    Legacy consumers (slim match_stage _extract_signature, alert body
    builders, _vision_summary_str fall-back) read top-level fields:
    color, body_style_hint, make, model, vehicle_features, description,
    confidence, type.

    We copy from vehicles[primary_vehicle_index] into those top-level
    fields here. When vehicles[] is empty (parse error, no vehicles
    visible), no copy happens — downstream falls back to legacy
    colors.vehicle handling.

    Idempotent: only writes if the top-level field is missing or None.
    """
    vehicles = result.get("vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        return
    pvi = result.get("primary_vehicle_index", 0)
    if not isinstance(pvi, int) or pvi < 0 or pvi >= len(vehicles):
        pvi = 0
    primary = vehicles[pvi]
    if not isinstance(primary, dict):
        return
    # Top-level fields legacy consumers read.
    for field in (
        "color", "body_style_hint", "make", "model",
        "vehicle_features", "description", "confidence",
    ):
        if field in primary and result.get(field) is None:
            result[field] = primary[field]
    # Slim match_stage _extract_signature reads `type` (not
    # body_style_hint). Map body_style → type for that consumer.
    if result.get("type") is None and isinstance(primary.get("body_style_hint"), str):
        result["type"] = primary["body_style_hint"]

    # Phase 6B.130 (§11.53) — also populate the legacy `colors.vehicle`
    # + `objects_detected` shapes so consumers that read those (e.g.
    # infra.alert_prompt._build_payload) get useful data on multi-vehicle
    # responses instead of all-null/empty defaults.
    primary_color = primary.get("color") if isinstance(primary, dict) else None
    if isinstance(primary_color, str) and primary_color.strip():
        colors = result.get("colors")
        if not isinstance(colors, dict):
            colors = {}
            result["colors"] = colors
        if not colors.get("vehicle"):
            colors["vehicle"] = primary_color.strip()

    # Build objects_detected from EACH vehicle in vehicles[] so the
    # threat-level LLM (which reads objects_detected) sees the full
    # picture, not just the primary. Format: "<body_style_hint>: <make>
    # <model>" or just "<body_style_hint>" when make/model is unknown.
    if not result.get("objects_detected"):
        objs: list[str] = []
        for v in vehicles:
            if not isinstance(v, dict):
                continue
            body = (v.get("body_style_hint") or v.get("type") or "").strip()
            make = (v.get("make") or "").strip()
            model = (v.get("model") or "").strip()
            label = body or "vehicle"
            if make or model:
                label = f"{label}: {make} {model}".strip()
            objs.append(label)
        if objs:
            result["objects_detected"] = objs


def _validate_vision_result(result: dict) -> dict | None:
    """
    Validate + fill defaults. Returns the result if valid, None if not.
    Pulled out of _parse_response so both first-try and recovered paths
    can use it.

    Phase 6B.66 (2026-08-08): relaxed the dict/object requirement. Vision
    (Qwen3-VL) sometimes returns a perfectly valid response WITHOUT the
    legacy `objects_detected` top-level field (it's not part of the
    `vehicles[]`-driven schema the vehicle-motion/static prompts describe).
    Downstream signature extraction in vehicle_state.py only needs
    `objects_detected` as a FALLBACK for `vehicles[]`-less responses —
    when `vehicles[]` is present and non-empty (the normal case for
    vehicle events), `objects_detected` is never read. So we accept any
    dict and fill defaults; we only reject if `result` isn't a dict.
    Previously: required `"objects_detected" in result` → returned None
    silently for the 09:26 OFS Silverado arrival (alert 653ba844), which
    dropped all 3 crops' Chevrolet/Silverado evidence before the matcher
    saw it.
    """
    if not isinstance(result, dict):
        return None
    # Always fill defaults — covers both legacy responses (with
    # objects_detected) and modern vehicles[]-driven responses (without).
    result.setdefault("primary_subject", "unknown")
    result.setdefault("actions", [])
    result.setdefault("scene_description", "")
    result.setdefault("confidence", 0.0)
    result.setdefault("notable_details", [])
    result.setdefault(
        "colors",
        {
            "vehicle": None,
            "clothing_primary": None,
            "clothing_secondary": None,
            "other": None,
        },
    )
    result.setdefault("species", None)
    # Phase 6B.6 — new structured vehicle list. Defaults: empty list
    # + 0 index. Backward-compat: signature extractor in vehicle_state
    # falls back to legacy colors.vehicle when vehicles[] is empty.
    result.setdefault("vehicles", [])
    result.setdefault("primary_vehicle_index", 0)
    # Phase 6B.129b (§11.52) — BACKWARD-COMPAT population. The crop
    # prompt now returns vehicles[] with full per-vehicle identification.
    # Legacy consumers (slim match_stage _extract_signature,
    # _vision_summary_str fall-back, alert body builders) read top-level
    # fields: color, body_style_hint, make, model, vehicle_features,
    # description, confidence, type. The schema marks those as required,
    # but Qwen only emits them via vehicles[]. So we copy them from
    # vehicles[primary_vehicle_index] into the top level here. When
    # vehicles[] is empty (parse error / no vehicles visible), top-level
    # fields stay at their defaults and downstream code falls back to
    # legacy colors.vehicle handling — no breakage.
    _populate_legacy_fields_from_vehicles(result)
    # Phase 6B.78 (2026-08-14) — REMOVED vehicle_motion and
    # moving_vehicle_indices defaults. The schema no longer asks for
    # these fields. The parser's job is to fill defaults for fields the
    # schema requires, not to invent values for fields the schema
    # doesn't request. Motion comes from the motion gate
    # (see infra/frame_diff + listener/motion_gate_pipeline); the
    # MovingObject / MotionResult dataclasses live in infra.motion_types
    # (Phase §11.90, 2026-09-01).
    # face_visibility: Qwen tells us whether InsightFace is worth running,
    # AND which frame has the best face (best_frame_index + per_frame).
    # Defaults to "no face visible" so a missing, explicit-null, or
    # malformed field means skip face recognition rather than crashing
    # after a valid vehicle-only response. Qwen returned null here on a
    # real 6-frame Tesla departure replay (2026-07-28).
    default_face_visibility = {
        "any_face_visible": False,
        "best_frame_index": 1,
        "best_frame_face_fraction": 0.0,
        "front_facing": False,
        "per_frame": [],
        "notes": "missing from vision response",
    }
    if not isinstance(result.get("face_visibility"), dict):
        result["face_visibility"] = default_face_visibility
    # Phase 6B.27 (2026-07-30): top-level best_frame_index for vehicle
    # bursts. Qwen picks which frame has the most visible moving vehicle.
    # Default to 1 (first frame) if Qwen doesn't return it — preserves
    # legacy behavior for non-vehicle callers.
    if not isinstance(result.get("best_frame_index"), (int, float)):
        result["best_frame_index"] = 1
    # Backward compat: older prompt returned "largest_face_fraction"
    # instead of "best_frame_face_fraction". Map it.
    fv = result["face_visibility"]
    if "largest_face_fraction" in fv and "best_frame_face_fraction" not in fv:
        fv["best_frame_face_fraction"] = fv.pop("largest_face_fraction")
    # Ensure each per_frame entry has a bbox field (null if missing).
    # Downstream code reads entry["bbox"] directly; absent key would
    # raise KeyError. 2026-07-20 design change: Qwen now reports the
    # bbox so the pipeline can crop a 640x640 region from the 4K frame.
    for entry in fv.get("per_frame", []):
        if isinstance(entry, dict):
            entry.setdefault("bbox", None)
    return result


def _error_result(reason: str) -> dict:
    # Phase 6B.78 (2026-08-14) — REMOVED moving_vehicle_indices and
    # vehicle_motion. Schema no longer requires them. The error
    # result mirrors the schema's required fields, nothing more.
    return {
        "objects_detected": ["error"],
        "primary_subject": "error",
        "actions": [],
        "scene_description": f"Analysis failed: {reason}",
        "confidence": 0.0,
        "notable_details": [f"Error: {reason}"],
        "colors": {
            "vehicle": None,
            "clothing_primary": None,
            "clothing_secondary": None,
            "other": None,
        },
        "species": None,
        "vehicles": [],
        "primary_vehicle_index": 0,
    }


def _parse_vehicle_classify_response(raw: str) -> dict | None:
    """Extract JSON from classify_vehicle_crop response. Mirrors _parse_response."""
    if not raw:
        return None
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(result, dict) and "make" in result:
        # Fill any missing required fields with null/0 so the result is always
        # safe to access by the downstream caller.
        for k, default in [
            ("make", None), ("model", None), ("body_style", None),
            ("trim_level", None), ("year_range", None),
            ("visible_modifications", None), ("distinctive_features", None),
            ("confidence", 0.0),
        ]:
            result.setdefault(k, default)
        return result
    return None


def _vehicle_classify_error(reason: str) -> dict:
    return {
        "make": None, "model": None, "body_style": None,
        "trim_level": None, "year_range": None,
        "visible_modifications": None, "distinctive_features": None,
        "confidence": 0.0,
        "_error": reason,
    }