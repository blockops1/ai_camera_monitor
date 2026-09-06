"""
classify_schema.py — §11.115.2 shared classify schema.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - none (module defines constants + enum)

OUTPUTS:
  - ClassLabel enum: the 4 possible classify responses
  - CLASSIFY_SCHEMA_JSON: the JSON schema literal embedded in the prompt
  - VALID_CLASSES: set of valid string values

PUBLIC API:
  - ClassLabel                       enum (VEHICLE | PERSON | ANIMAL | OTHER)
  - CLASSIFY_SCHEMA_JSON             str, JSON schema literal (embedded in the prompt)
  - CLASSIFY_SCHEMA_JSON_DICT        dict, real json_schema spec (used by
                                     response_format in llama-server). §11.116
                                     PR 1 — distinct from the prompt literal
                                     above because response_format requires a
                                     dict, not the TypeScript-style example
                                     string.
  - VALID_CLASSES                    frozenset[str]

DOES NOT DO:
  - Does NOT call Qwen. (See infra.classify_prompt for the prompt.)
  - Does NOT validate Qwen's response. (See infra.classify_validator.)

CALLED BY:
  - infra.classify_prompt (embed schema in prompt)
  - infra.classify_validator (parse + validate response)
  - listener.single_pipeline (route to class-specific Qwen call 2)

RELATED:
  - PLAN.md §11.115 — design rationale (shared classify, then diverge)

Design notes:
  - 4 classes only. Adding a 5th would require a schema + validator +
    pipeline update; that's deliberate. §11.115 collapses "person vs
    animal vs vehicle" into a single first-call, then a class-specific
    second call.
  - `other` is the safe default. Validator falls back to OTHER on any
    parse failure or unknown class.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


# ============================================================================
# ClassLabel enum
# ============================================================================
# The 4 classes that Qwen call 1 can return. Diverge on this in
# listener.single_pipeline.
class ClassLabel(Enum):
    VEHICLE = "vehicle"
    PERSON = "person"
    ANIMAL = "animal"
    OTHER = "other"


VALID_CLASSES: frozenset[str] = frozenset(c.value for c in ClassLabel)


# ============================================================================
# Schema literal — embedded in the prompt so Qwen emits this exact shape.
# ============================================================================
# Mirrors the "report every field, return null if unsure" discipline used
# in person/animal/vehicle prompts (Phase 6B.106+), but minimal: just one
# classification + one confidence + one short reasoning.
CLASSIFY_SCHEMA_JSON = """\
{
  "class":      "vehicle" | "person" | "animal" | "other",
  "confidence": 0.0-1.0,
  "reasoning":  "1-2 short phrases describing what you see"
}"""


# ============================================================================
# CLASSIFY_SCHEMA_JSON_DICT — §11.116 PR 1 (Phase A, schema-only)
# ============================================================================
# Real json_schema spec used by llama-server's `response_format` field.
# The string form above is what the prompt embeds — Qwen uses it as an
# example of what the response should look like. The dict form below is
# what llama-server uses to enforce the response shape via strict
# json_schema. They MUST agree on the field names and types; if you
# change one, change the other.
#
# `additionalProperties: False` makes any extra field a loud schema
# violation rather than silent corruption (Operator 2026-09-05:
# "fail loud not quiet").
# ============================================================================
CLASSIFY_SCHEMA_JSON_DICT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "class": {
            "type": "string",
            "enum": ["vehicle", "person", "animal", "other"],
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "reasoning": {"type": "string"},
    },
    "required": ["class", "confidence", "reasoning"],
    "additionalProperties": False,
}
