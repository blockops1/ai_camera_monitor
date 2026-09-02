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
  - CLASSIFY_SCHEMA_JSON             str, JSON schema literal
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
