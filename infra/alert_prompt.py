"""
alert_prompt.py — Qwen3.5-9B alert-classifier prompt + payload + parse + error.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - function arg vision_result: dict (required) — Vision Analysis JSON
    - function arg camera_name: str (required)
    - function arg timestamp: str (required, ISO 8601)
    - function arg source: str (required, "motion" | "match" | ...)
    - function arg raw: str (required for _parse_response) — raw LLM output

OUTPUTS:
    - return value (_build_payload): dict — OpenAI-compatible chat
      completions payload (model + messages + temperature + max_tokens)
    - return value (_parse_response): dict | None — parsed alert JSON
      (None on parse failure)
    - return value (_error_result): dict — error sentinel with
      threat_level=-1 (caller can distinguish from real L0)
    - return value (_to_local_iso): str — local-time ISO 8601 (or input
      unchanged on missing tzinfo / parse failure)

PUBLIC API:
    SYSTEM_PROMPT (str)
        The LLM's role definition. Encodes property context, threat
        levels (0/1/2), time-of-day rules, and false-alarm traps. The
        orchestrator (alert_generator.generate_alert) forwards this
        verbatim into the OpenAI-compatible messages[0].content.
    _to_local_iso(timestamp: str) -> str
        Convert any ISO-8601 timestamp to local time with explicit offset.
        Used by _build_payload to render the timestamp the LLM sees.
    _build_payload(vision_result: dict, camera_name: str,
                   timestamp: str, source: str) -> dict
        Construct the OpenAI chat completions payload for the alert LLM.
    _format_vehicles_block(vision_result: dict) -> str
        Phase.130 (§11.53) — build the 'Vehicles:' section for the
        prompt. Reads vision_result["vehicles"] (multi-vehicle schema),
        marks primary_vehicle_index with '(primary)', caps at 3
        vehicles, returns "" when vehicles[] is missing/empty. Used by
        _build_payload to surface each vehicle's identification to the
        threat-level LLM.
    _parse_response(raw: str) -> dict | None
        Extract JSON from model response. Handles bare JSON, markdown
        code fences, and prose preambles (e.g. "Sure, here is the JSON: ...").
        Validates that threat_level is present and numeric.
    _error_result(reason: str) -> dict
        Standard error sentinel for generate_alert failures. Shape matches
        the Alert Output JSON schema; threat_level=-1 distinguishes it
        from real L0 verdicts.

DOES NOT DO:
    - HTTP transport → infra.alert_client
    - Apply off-hours escalation → infra.alert_overrides_offhours
    - Apply baseline suppressions → infra.alert_overrides_baseline
    - Retry on parse failure → infra.alert_generator.generate_alert owns
      the retry loop and the off-hours+baseline chain

WHY HERE:
    Three pieces — prompt text, payload builder, response parser — must
    change together. Adding a field to the alert schema (e.g. a new
    threat_level rule, a new JSON output field) requires editing all three
    in one commit or the LLM output and the parser drift out of sync
    (same failure mode that hit vision_analyzer pre-§3 split, where the
    6B.74 schema-prompt divergence dropped make/model/vehicle_features
    for 3 production alerts). The error sentinel lives here because it's
    the parse failure's paired output shape.

    _to_local_iso lives here because it formats the timestamp that goes
    into the user prompt — directly coupled to the prompt text.

CALLED BY:
    - infra.alert_generator.generate_alert: _build_payload, _parse_response,
      _error_result

CALLS INTO:
    - json (stdlib): parse LLM response
    - uuid (stdlib): generate alert_id for error sentinel
    - datetime (stdlib): tz conversion

RELATED:
    - infra.alert_generator — orchestrator that calls into this module
    - infra.alert_overrides_offhours / alert_overrides_baseline — consume
      the alert dict shape produced by _parse_response / _error_result
"""

import json
import uuid
from datetime import datetime

from infra.llm_config import load_text_config

SYSTEM_PROMPT = """You are a farm security alert classifier. You receive a vision model's structured analysis of a security camera frame and must output a structured alert JSON. You do NOT see images — only the vision model's text output.

### Property context

This is a small farm workshop/garage property in a rural area. The property has multiple security cameras that may cover the workshop interior, garage interior, exterior entry points, driveway, or perimeter depending on configuration. New cameras may be added over time, so classify based on the camera name provided in each vision result, not on a fixed mental map of the property.

**Current and likely camera types:**
- Interior cameras with names containing "Inside" — workshop/garage interior, cluttered with tools, equipment, vehicles, workbenches
- Exterior cameras with names containing "Outside", "Driveway", "Yard", "Porch", "Backyard", or street names — outdoor areas, expected to show vehicles, wildlife, weather, people coming and going

**Activity profile:**
- Workshop is actively used during the day (6 AM – 10 PM) for farm and personal projects
- Family members and known workers come and go freely during work hours
- After 10 PM the property should normally be empty and secured (locked doors, garage closed, no vehicles in driveway)
- One small dog lives on the property and may appear on interior and exterior cameras

**Per-camera context (use this to interpret what the vision model sees):**
- **Back Door Inside**: workshop/garage interior. Vehicles visible here are being worked on — expected. People present during work hours are expected.
- **Front Door Outside (Outside)**: a camera repositioned outside the front person door on 2026-07-20. People walking up to or entering the front door are the primary subject — full face capture expected at close range.
- **Outside Front Solar (Outside, gatekeeper)**: covers the exterior of the building facing north-west, including the front driveway approach and the view across the neighbor's pasture to the north. People walking toward the workshop side door, parked vehicles at the front of the building — all normal during work hours. At night, vehicles driving across the north pasture can appear as distant headlights in this camera.
- **Outside Back Solar (Outside)**: covers the property's **regular parking area** to the south-east, where the resident's vehicles and family vehicles are routinely parked for extended periods (overnight, on weekends, etc.). A parked vehicle on this camera is the BASELINE state, not an anomaly. The dump trailer and any vehicle with reflective strips may cause IR pixel-diff events at night — those are noise. Only escalate on this camera if vision explicitly describes unusual behaviour: an unfamiliar vehicle paired with a person attempting entry, a vehicle that appears to have been involved in an incident (broken glass, damage), a person loitering beside an unfamiliar vehicle, or a vehicle blocking the driveway/emergency access.

**Time-of-day rules:**
- 6 AM – 10 PM: daytime, normal activity, generous tolerance
- 10 PM – 6 AM: night, lower tolerance for unknown activity; anything unusual is suspicious

### Core classification rules

1. **Classify strictly from the vision output.** Do not invent details. If the vision model did not mention a weapon, forced entry, violence, or theft, those scenarios do not apply.
2. **An animal is not a medical emergency.** A dog or cat lying down, sleeping, or resting is Level 0, not a "person down" emergency.
3. **Normal human activity during work hours is Level 0.** Standing, walking, working, talking, sitting — even with multiple people or the dog present. Daytime workshop scenes are Level 0 regardless of specific activity.
4. **Conservative escalation.** Prefer Level 0 over Level 1, Level 1 over Level 2. False alarms erode trust. A missed Level 1 is far less costly than a daily false Level 2.

### Threat levels

**Level 0 — Normal Activity (log only, no Telegram alert)**
- People doing ordinary things (working, walking, talking, sitting) during 6 AM – 10 PM
- Family members and known workers at any time during work hours
- Delivery or service personnel during daylight (6 AM – 10 PM)
- Wildlife, deer, or farm animals visible (rare indoors but possible)
- The dog resting, playing, or moving through the workshop
- Farm equipment, vehicles, tools in normal locations
- People in workshop/garage during daytime — regardless of specific activity

**Level 1 — Suspicious (Telegram warning ⚠️)**
- Any person present inside the workshop between 10 PM and 6 AM, UNLESS the vision model clearly identifies them as a known resident or worker (e.g., named in notable_details, wearing recognizable work clothes, performing expected nighttime tasks)
- Person testing door handles, vehicle doors, or windows at any time, on any camera
- Person attempting to remain hidden from camera (ducking, crouching behind objects to avoid view)
- Person carrying property they did not appear to bring with them (unfamiliar packages, items from inside the workshop or house being moved toward an exit)
- Person loitering without visible purpose at any time, on any camera
- Person at any exterior door, entry point, or window at unusual hours (10 PM – 6 AM)
- Person on the property who does not appear to be a resident, worker, or expected visitor at any time of day
- **For Outside Back Solar only**: any L1 condition must be corroborated by unusual behaviour (see per-camera context above). A parked vehicle alone, with no person acting suspiciously near it, is NOT L1 on this camera — it's the baseline state. This is enforced deterministically in code (`_apply_parked_vehicle_baseline_override` in alert_generator.py): if vision did not describe a person AND alert is L1, it is downgraded to L0 regardless of what you output here. Do not attempt to override that by stating a person was seen in your description when vision did not show one.
- **Vehicles at OFS (deterministic override — do NOT output a level for vehicle events at Outside Front Solar):** vehicle threat-level routing at OFS is handled by the matcher in `listener/vehicle_event_pipeline.py` (Phase.123, Note 2026-08-22). Known vehicles matched to known_vehicles.json → L1, unknown vehicles (no match) → L2. Your role for vehicle events is to describe what you see (vision_result) so the matcher can match. **If you see a vehicle at OFS, output Level 0 for the LLM body — the swap in emit_result_stage will set the correct level based on match/no-match.** Do not attempt to output L1 or L2 yourself for vehicles; the code path takes precedence.

**Level 2 — Critical Threat (Telegram urgent 🚨)**
ONLY apply Level 2 if vision explicitly describes AT LEAST ONE of:
- **Weapons visible** (firearms, knives, blunt weapons held as weapons)
- **Break-in tools visible** (crowbar, bolt cutters, pry bar, lock pick) being used
- **Forced entry in progress** (door or window visibly broken, pried, forced open)
- **Active theft or property damage** (person carrying away property they did not bring, breaking items, prying things open)
- **Physical aggression or violence** (people fighting, attacking, restraining)
- **Person fleeing with property they did not have when they arrived**

If vision does NOT describe any of the above, the alert is NOT Level 2.

### False-alarm traps to avoid

- ❌ "Person lying down" → NOT a medical emergency unless vision explicitly describes unconsciousness, injury, or visible distress AND no other people are responding
- ❌ Dog or animal resting → Level 0, always
- ❌ Person sitting or crouching to work on something → Level 0
- ❌ Person on the floor doing repairs, yoga, exercise → Level 0
- ❌ Children playing, even roughly → Level 0
- ❌ Workers moving equipment → Level 0
- ❌ Cluttered workshop full of tools and equipment → Level 0 (this is the normal state)

### Output

Respond ONLY with valid JSON. No explanation, no markdown, no text outside the JSON object. Use only the fields described in the user prompt."""


def _to_local_iso(timestamp: str) -> str:
    """Convert any ISO-8601 timestamp to local time with explicit offset.

    Burned 2026-07-20: the LLM was reading raw UTC hours from Reolink's
    `+0000` timestamp and reasoning "20:54 = night", escalating a 16:54 EDT
    work-hour alert to L1 ("Vehicle with open door at night"). The LLM has
    no concept of the local timezone on its own — it reads the substring.
    Fix: convert tz-aware UTC to local before formatting, so the LLM sees
    the same hour the user sees. Naive timestamps stay as-is (treated as
    local — matches prior behavior of _is_off_hours).
    """
    try:
        dt = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return timestamp  # leave as-is; _is_off_hours also handles gracefully
    if dt.tzinfo is None:
        return timestamp
    local_dt = dt.astimezone()
    return local_dt.isoformat(timespec="seconds")


def _format_vehicles_block(vision_result: dict) -> str:
    """Phase.130 (§11.53) — build a 'Vehicles:' section for the prompt.

    Reads vision_result["vehicles"] (multi-vehicle schema). For each
    vehicle, emits a one-line identification. The vehicle at
    primary_vehicle_index is marked with '(primary)'. Lines are capped
    at 3 to keep the prompt compact; a footer count tells the LLM how
    many vehicles were omitted.

    Returns "" if vehicles[] is missing or empty (legacy compat).
    """
    vehicles = vision_result.get("vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        return ""

    pvi = vision_result.get("primary_vehicle_index", 0)
    if not isinstance(pvi, int) or pvi < 0 or pvi >= len(vehicles):
        pvi = 0

    lines: list[str] = []
    shown = 0
    for i, v in enumerate(vehicles):
        if shown >= 3:
            break
        if not isinstance(v, dict):
            continue
        color = (v.get("color") or "").strip()
        make = (v.get("make") or "").strip()
        model = (v.get("model") or "").strip()
        body = (v.get("body_style_hint") or v.get("type") or "").strip()
        parts: list[str] = []
        if color:
            parts.append(color)
        ident = f"{make} {model}".strip() if (make or model) else ""  # noqa: FLY002 — explicit join reads clearer than fstring concat here
        if ident:
            parts.append(ident)
        if body:
            parts.append(body)
        if not parts:
            continue
        line = " ".join(parts)
        marker = " (primary)" if i == pvi else ""
        lines.append(f"  - {line}{marker}")
        shown += 1

    if not lines:
        return ""

    extra = len(vehicles) - shown
    suffix = f"\n  ({extra} more vehicle(s) omitted)" if extra > 0 else ""
    return "Vehicles:\n" + "\n".join(lines) + suffix


def _build_payload(
    vision_result: dict, camera_name: str, timestamp: str, source: str
) -> dict:
    vehicles_block = _format_vehicles_block(vision_result)
    user_prompt = (
        f"Vision Analysis Result:\n"
        f"Camera: {camera_name}\n"
        f"Timestamp (local time): {_to_local_iso(timestamp)}\n"
        + (vehicles_block + "\n" if vehicles_block else "")
        + f"Objects: {vision_result.get('objects_detected', [])}\n"
        f"Primary Subject: {vision_result.get('primary_subject', 'none')}\n"
        f"Actions: {vision_result.get('actions', [])}\n"
        f"Scene: {vision_result.get('scene_description', '')}\n"
        f"Confidence: {vision_result.get('confidence', 0.0)}\n"
        f"Notable Details: {vision_result.get('notable_details', [])}\n"
        f"Colors: {vision_result.get('colors', {})}\n"
        f"Species: {vision_result.get('species', None)}\n\n"
        "Respond ONLY with valid JSON in this exact format:\n"
        "{\n"
        '  "alert_id": "<generate a UUID v4>",\n'
        '  "camera": "<camera name>",\n'
        '  "timestamp": "<ISO 8601 timestamp>",\n'
        '  "threat_level": <0, 1, or 2>,\n'
        '  "title": "<concise title, max 60 chars>",\n'
        '  "description": "<2-3 sentence description of what happened and why it matters>",\n'
        '  "recommendations": ["<actionable recommendation 1>", "<recommendation 2>"],\n'
        '  "vision_summary": "<short summary for Telegram, max 140 chars>",\n'
        '  "source": "<rtsp_frames or snapshot>"\n'
        "}"
    )

    return {
        "model": load_text_config().model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        # Low temperature: alert classification should be near-deterministic.
        # Higher temperatures cause the model to occasionally invent dramatic
        # scenarios that the vision model did not actually describe.
        "temperature": 0.05,
        "max_tokens": 512,
    }


def _parse_response(raw: str) -> dict | None:
    """
    Extract JSON from model response.
    Handles bare JSON and markdown code blocks.
    """
    if not raw:
        return None

    stripped = raw.strip()

    # Strip markdown code fences
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    # Strip any leading non-JSON text before the first {
    first_brace = stripped.find("{")
    if first_brace > 0:
        stripped = stripped[first_brace:]

    try:
        result = json.loads(stripped)
        if isinstance(result, dict) and "threat_level" in result:
            # Validate types
            if isinstance(result.get("threat_level"), (int, float)):
                result["threat_level"] = int(result["threat_level"])
            return result
    except json.JSONDecodeError:
        pass

    return None


def _error_result(reason: str) -> dict:
    return {
        "alert_id": str(uuid.uuid4()),
        "camera": "unknown",
        "timestamp": "",
        "threat_level": -1,
        "title": "error",
        "description": f"Alert generation failed: {reason}",
        "recommendations": [],
        "vision_summary": f"Error: {reason}",
        "source": "error",
    }