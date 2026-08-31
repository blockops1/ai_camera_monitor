# Schema design for constrained decoding

Cross-runtime pitfalls and patterns for JSON Schemas passed to GBNF, XGrammar, OpenAI strict mode, Outlines, Guidance, or Instructor backends.

## The shared subset

These JSON Schema features are reliably supported across GBNF, XGrammar, OpenAI strict mode, and Outlines:

- `type` (string, number, integer, boolean, array, object, null)
- `enum` (closed list of allowed values)
- `properties` + `required` (object shape)
- `items` + `minItems` / `maxItems` (array shape)
- `minLength` / `maxLength` / `pattern` (string shape)
- `minimum` / `maximum` (numeric shape)
- `additionalProperties: false` (prevent surprises)

Use only these when you need cross-runtime portability.

## Features that bite

### `if/then/else` — silent drops in GBNF

GBNF converts only a subset. `if/then/else` can be silently dropped, producing a grammar that doesn't enforce what you wrote. Test the compiled grammar against representative inputs. If you need conditional structure, model it explicitly with `oneOf` / discriminator fields instead.

### Nested `$ref` — fragile

GBNF and some other compilers struggle with `$ref` chains. Inline the types when portability matters.

### Complex `anyOf` with discriminator ambiguity

When `anyOf` branches have overlapping shapes, the grammar may fail to compile or produce ambiguous output. Prefer closed `oneOf` with a discriminator enum:

```json
{
  "oneOf": [
    {"properties": {"type": {"const": "vehicle"}, "color": {"type": "string"}}, "required": ["type","color"]},
    {"properties": {"type": {"const": "person"},   "name": {"type": "string"}}, "required": ["type","name"]}
  ]
}
```

### `nullable: true` vs `"type": ["string", "null"]`

OpenAI strict mode accepts the latter. Avoid `nullable` (old OpenAPI v3 keyword) — not standard JSON Schema and not supported by most compilers.

## Modeling absence vs. uncertainty

This is the single most common schema bug for extraction pipelines.

**Bad**: `{"vehicle_color": null}` is overloaded — does it mean "no vehicle" or "vehicle present but color unknown"? Semantic validation can't distinguish the cases.

**Good**: explicit enum values for the disambiguation you care about.

```json
{
  "vehicle_present": {"type": "boolean"},
  "vehicle_color": {
    "type": "string",
    "enum": [
      "red", "blue", "green", "yellow", "white", "black", "gray",
      "silver", "orange", "brown", "purple", "pink", "other",
      "unknown",   // present but not determinable
      "none"       // no vehicle
    ]
  }
}
```

Then semantic validation can reject `vehicle_present=true && vehicle_color="none"` as a contradiction.

## Required vs. optional fields

Default every field to `required`. Optional fields invite silent omission. If a field truly is optional, prefer a sentinel string (`"unknown"`, `"not_provided"`) over `null` to keep semantics checkable.

## `additionalProperties: false`

Always set this for strict extraction schemas. Without it, models may add free-form keys that downstream consumers can't predict, and the grammar may not flag them.

## Top-level array vs. wrapper object

```json
// Allowed: top-level array
{"type": "array", "items": {"type": "string"}}

// Better: wrapper object — easier to extend
{
  "type": "object",
  "properties": {
    "items": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["items"],
  "additionalProperties": false
}
```

OpenAI strict mode requires the top level to be an object.

## Free-form reasoning inside a structured envelope

If you want the model to reason before answering, give it a structured field:

```json
{
  "observations": {"type": "string", "description": "Step-by-step visual observations before answering"},
  "answer": {"type": "object", "properties": { /* ... */ }, "required": [...], "additionalProperties": false}
}
```

Do **not** ask for "think step by step" before the JSON object — the reasoning leaks outside the envelope as markdown fences or leading prose.

## Field descriptions

Always add `description` strings to fields. Most compilers don't auto-inject the schema into the prompt, so the descriptions in your natural-language prompt must agree with the schema. Keep them short and unambiguous:

```json
{
  "vehicle_present": {
    "type": "boolean",
    "description": "True if any vehicle (car, truck, van, bus, motorcycle) is visible."
  },
  "colors": {
    "type": "object",
    "description": "Per-category dominant colors. Use 'none' for absence and 'unknown' for not determinable.",
    ...
  }
}
```

## Testing the compiled grammar

Before shipping, run the schema through the actual runtime with representative prompts and:

1. Confirm every output is valid JSON.
2. Confirm every `required` key is present in every output (test omission cases explicitly).
3. Confirm `enum` enforcement — send prompts that would naturally produce a value not in the enum; verify the model either uses the closest enum value or picks `"other"` / `"unknown"`.
4. Confirm `additionalProperties: false` — send prompts that would naturally add extra fields; verify the output doesn't include them.

If a grammar feature is silently dropped, the failure mode is usually "model produces output that violates the schema" — the grammar compiles but doesn't constrain what you thought it did.

## Cross-runtime schema portability checklist

- [ ] No `if/then/else`
- [ ] No deep `$ref` chains
- [ ] No `nullable: true` (use `"type": [..., "null"]` instead)
- [ ] All fields `required` (or explicitly optional with sentinel)
- [ ] `additionalProperties: false` on every object
- [ ] Top level is an object (for OpenAI strict mode)
- [ ] `description` on every field
- [ ] Absence vs. uncertainty modeled with explicit enum values
- [ ] No free-form "think first" instructions; reasoning is a structured field if needed
- [ ] `max_tokens` allows the full object
- [ ] Schema tested against representative prompts in the actual runtime