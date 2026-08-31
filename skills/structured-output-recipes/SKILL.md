---
name: structured-output-recipes
description: Reliable structured/JSON output from local LLMs — the structural-vs-semantic constraint gap, schema authoring pitfalls, runtime enforcement, and semantic consistency gates with targeted retry. Triggers include 'structured output', 'json schema', 'response_format', 'json_object', 'schema adherence', 'force JSON', 'constrained generation', 'grammar-constrained', 'GBNF', 'XGrammar', 'regex-constrained', 'Pydantic validation', 'Instructor retry', 'vision extraction schema', 'VLM JSON output', 'Qwen3-VL JSON', and 'LLaVA structured output'. Covers schema/prompt drift (adding fields to prose without updating the JSON schema = silent stripping at grammar-parse time).
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [structured-output, json-schema, response-format, grammar-constrained-decoding, gbnf, xgrammar, pydantic, instructor, vision-llm, vlm, qwen3-vl, llava, internvl, validation-retry]
---

# Structured-output recipes for local LLMs

The runtime enforces **shape**. You enforce **semantics**. Combine them — never trust either alone.

This skill is the class-level playbook for getting reliable JSON/structured output from local (or API) LLMs. It applies whenever you need machine-parseable output: extraction, classification-as-data, agent tool calls, vision-to-JSON, log/event normalization, code-generation scaffolding, or any time a downstream consumer cannot tolerate format drift.

The umbrella covers: schema design pitfalls that bite both grammar-based and prompt-based enforcement, the structural-vs-semantic gap and how to close it with consistency gates plus targeted retry, and how each runtime (llama.cpp, Outlines, Guidance, Instructor, vLLM/XGrammar, OpenAI/Anthropic APIs) exposes structured output and what it actually guarantees.

## When to use

- Building or debugging any LLM extraction pipeline that returns JSON/XML/code
- Designing a JSON Schema to pass to a constrained-decoding backend
- Seeing "valid JSON, wrong values" — schema passes, content hallucinates
- Picking between llama.cpp `response_format`, Outlines, Guidance, Instructor, XGrammar
- Combining image inputs with structured output (VLM → JSON)
- Picking temperature/sampling for deterministic extraction
- Tuning retry/validation loops for tool-calling agents

## The core lesson: shape ≠ semantics

Constrained decoding (GBNF, XGrammar, regex-constrained, JSON mode, JSON Schema strict mode) **guarantees the output parses as the right shape** — required keys present, values of the right types, enums enforced. It does **not** guarantee the values are factual or consistent.

Concrete failure mode seen in production: a VLM is prompted to return `{"vehicle_present": true, "colors": {"vehicle": "blue"}, "description": "a red truck parked on the street"}`. A grammar enforces the keys and enum, but allows the contradiction. The fix is not a stricter schema — it's a **semantic consistency gate** plus a targeted retry.

**Default recipe**: enforce shape with the strongest mechanism your runtime offers, then validate semantics in code, then retry with corrective feedback on failure.

## Schema authoring pitfalls

These pitfalls apply across runtimes (GBNF, XGrammar, OpenAI strict mode).

1. **Use explicit enum strings, not `null`, for absence/uncertainty.** `"none"` (not present), `"unknown"` (present but not determinable), vs. `null` (collapses both cases). `null` makes semantic validation ambiguous.
2. **Set `additionalProperties: false` and make every field `required`.** Otherwise the model may silently omit fields, and the grammar may not flag it.
3. **Avoid `if/then/else`, nested `$ref`, complex `anyOf`.** GBNF converts only a subset; unsupported keywords can be silently dropped, producing grammars that don't enforce what you wrote. **Test the compiled grammar** against representative inputs.
4. **Don't ask for free-form chain-of-thought before JSON.** It can leak outside the structured envelope (markdown fences, leading prose). Either inline reasoning into a structured `observations` / `reasoning` field, or do a silent processing-order instruction in the system prompt.
5. **Describing fields in prose is mandatory, even with a schema.** Most runtimes (including llama.cpp) do **not** inject the schema into the prompt — only use it to mask tokens. Always tell the model what each field means.
6. **Allow enough `max_tokens` for the full object.** Truncation produces partial JSON that fails parsing for the wrong reason.
6a. **Don't type boolean fields too tightly — VLMs serialize booleans as strings.** (2026-08-08 burn) Declaring `cab_marker_lights: { "type": "boolean" }` in a JSON Schema looks safe, but Qwen3-VL (and other VLMs) will frequently emit `"cab_marker_lights": "false"` (lowercase string) when given a prompt like "is the cab marker light on?" — the schema strict-mode rejects the response, the parser fails, and the correct LLM answer is lost in a parse-error string. **Two fixes:** (a) widen the schema type to `["boolean", "string"]` and accept either, OR (b) add a normalizer in the response parser that coerces `"true"`/`"false"` strings to booleans before schema validation. The first is preferred — it preserves the model's intent even when the type drifts. This is a *third* failure mode beyond the existing Mode 1 / Mode 2 schema-drift pitfalls: the schema and prompt are in sync, the field IS in both, but the declared type is narrower than what the LLM emits. **Verification:** whenever a vision-extraction schema declares a boolean field, also run the schema through the LLM with a probe prompt and inspect the raw response — if the LLM returns the field as a string, widen the schema. Treat this as part of the schema-authoring checklist, not as a separate concern.
7. **For deterministic extraction, set `temperature: 0` (or 0.1–0.2 max).** Constrained decoding guarantees shape; sampling variance still affects factual content.

## The retry pattern that actually works

Naive retry on parse error is weak. **Targeted retry with corrective feedback** is strong:

1. Generate structured output.
2. Validate against JSON Schema (Pydantic, jsonschema, etc.).
3. Validate semantic rules (cross-field consistency, business logic).
4. If either fails, build a short feedback message: "colors.vehicle was 'unknown' but description says 'blue car'. Re-inspect the image and correct all fields; output JSON only."
5. Retry with the feedback appended to the prior assistant turn.
6. Cap retries (1–2 is usually enough; 3+ wastes tokens without fixing whatever is causing the inconsistency).

Include the **failed output** in the retry context so the model can self-correct — Instructor does this automatically when you pass a Pydantic model and `max_retries`.

## Runtime comparison

| Runtime | Mechanism | Strength | Limitation |
|---|---|---|---|
| **OpenAI strict JSON Schema** (`response_format.type=json_schema`, `strict:true`) | Constrained decoding + schema adherence | Strongest schema adherence; SDKs (Pydantic, Zod) | Closed API only |
| **llama.cpp `response_format`** (`type:json_schema` / `type:json_object`) | JSON Schema → GBNF | Local; supports vision; OpenAI-compatible endpoint | Subset of JSON Schema; some keywords silently dropped |
| **vLLM XGrammar / Outlines backend** | Grammar compilation, fast | High throughput; broad schema coverage | Requires vLLM server; vision depends on the model registration |
| **Outlines** | JSON Schema / Pydantic / regex / context-free grammars | Most flexible constraint types; can target llama-cpp-python directly | Direct llama.cpp vision adapters may need custom image preprocessing |
| **Guidance (MS Research)** | Regex, select(), grammars, token-level control | Best for complex multi-step constrained flows; char-level interleaving | API style is its own DSL; vision support depends on the model |
| **Instructor** | Pydantic validation + automatic retry with feedback | Best-in-class validation/retry ergonomics; multi-provider | Doesn't enforce shape at decode time on its own; pairs with a backend |
| **Anthropic tool use** (`input_schema` + `tool_choice`) | Tool-use as structured-output vehicle | Strong for tool-calling agents; vision in tool inputs | Schema enforced but content can still be wrong |

**Choice guidance**:
- API + need strict schema → OpenAI Structured Outputs (`strict: true`)
- Local + vision → `llama-server` `response_format` is the lowest friction
- Need regex/select/multi-step → Guidance
- Need Pydantic-driven validation/retry → Instructor (pairs with llama-cpp-python, OpenAI, etc.)
- Need max throughput + structured → vLLM with XGrammar backend
- VLM extraction → `llama-server` + custom validation/retry is usually enough before reaching for Outlines/Guidance

## Vision + structured output

VLMs (Qwen3-VL, LLaVA-NeXT, InternVL, CogVLM) do **not** implement native structured-output modes — enforcement happens in the serving runtime. Working recipe:

1. Send image + prompt + `response_format` JSON Schema to a multimodal-capable server (llama.cpp with `mmproj-*.gguf`, vLLM with `--limit-mm-per-prompt image=...`, etc.).
2. Make every key `required`, set `additionalProperties: false`, prefer enum over `null`.
3. Use an observation-first prompt: "Inspect all visible vehicles and determine each color. Write description from the same observations. Silently verify cross-field consistency before answering."
4. Set `temperature: 0` for deterministic extraction.
5. Validate with Pydantic + semantic consistency rules (e.g., `objects_detected contains "car"` ⇒ `vehicle_present is true` and `colors.vehicle != "none"`).
6. On failure, retry once with the failed output + a corrective message.
7. Split into two calls (visual facts → final object) only if one-call consistency is inadequate.
8. Include 2–4 image-independent few-shot examples covering positive, ambiguous, and negative cases.

Reference: [references/vision-extraction.md](references/vision-extraction.md) for a complete Qwen3-VL + llama.cpp example with tested schema, prompt, validator, and retry loop.

## Pitfalls

- **Trusting constrained decoding to mean correct.** It only means shape-conformant. Always add semantic validation.
- **Using `null` to mean either "absent" or "unknown".** It collapses two semantically different states; replace with explicit enum values.
- **Asking for JSON without telling the model the field semantics.** The schema isn't auto-injected into the prompt by most runtimes.
- **Free-form chain-of-thought before the JSON object.** It leaks outside the envelope (markdown fences, leading prose). Use a structured `reasoning`/`observations` field if you want reasoning.
- **Naive parse-error retry without corrective feedback.** Models repeat the same mistake. Pass the failed output + a targeted error message.
- **Over-iterating retries.** 1–2 retries is the sweet spot. Beyond that, fix the prompt or schema.
- **Forgetting `max_tokens`.** Truncation silently produces partial JSON.
- **Sampling variance on extraction.** `temperature: 0` (or ≤0.2). Don't use the model's chat defaults for deterministic extraction.
- **Asking one VLM pass for everything in a complex scene.** Empirically (Qwen3-VL on 4K surveillance frames), asking one call to return *all* per-object attributes (make, model, color, body_style, plate, plate_state) plus scene-level facts (people, animals, lighting) degrades per-object accuracy significantly. The model's attention is split across the whole frame. **Two-pass cascade**: first pass returns lightweight per-object metadata (bbox + color + rough body_style_hint only, NOT make/model). Second pass takes the cropped subimage of one vehicle and runs a tighter focused schema/prompt for the high-precision attributes (make, model, plate). Net cost is slightly higher (one extra inference per object) but per-object accuracy on the high-precision attributes is markedly better. Pitfall: do NOT skip the bbox in the first pass — without it, the second pass has no way to crop.
- **Trusting freeform condensation of structured upstream output.** A common pipeline shape: stage 1 enforces `response_format: json_schema` (extracts facts), stage 2 calls an LLM without schema to "summarize" stage 1 into human-readable prose (title, description, recommendations). **Stage 2 can hallucinate details not present in stage 1.** The hallucination is invisible to stage 1's schema (stage 1 is correct) and invisible to any post-hoc prompt guardrail (the LLM rationalizes its own output before the guardrail can engage). Observed in production (farm-surveillance, 2026-07-21): stage-1 vision correctly described "parked vehicles, no human activity, no movement"; stage-2 alert condenser invented "Dark vehicle with headlights on parked near building" because night-time + parked-vehicle + camera-name context suggested a parking lot to the LLM. Mitigation options, in order of effort: (a) deterministic post-filter on the output fields using structured facts from stage 1 (e.g. if stage-1 vision saw no person and output text contains "headlight" with no corroborating evidence, force field to null/L0); (b) force stage-2 condensation to be a verbatim quote of stage-1 fields plus minimal connective tissue (very brittle); (c) skip stage 2 entirely and render Telegram messages from stage-1 JSON directly (best long-term but loses narrative UX). Option (a) is the lowest-risk first move. The pattern generalizes: any time a freeform LLM call sits between a structured source and a user-facing message, treat the condensation as untrusted output and validate against the source.

- **Schema / prompt drift — two failure modes that both leave fields silently missing.** The prompt template (`VEHICLE_MOTION_PROMPT_TEMPLATE`-style) and the JSON schema (`response_format.json_schema.schema`) are TWO separate places that must stay in sync. The template tells the model what to *produce*; the schema tells the runtime what to *allow*. There are two distinct silent-failure modes:

  **Mode 1 — prompt asks, schema doesn't have it (existing pitfall).** If you add a field to the template prose but forget to add it to the schema, llama-server's grammar-constrained decoder will **silently strip** the field at grammar-parse time — no warning, no error, just an incomplete JSON response. Observed in production (farm-surveillance, 2026-07-27 morning Tesla incident): prompt template was edited to ask for `vehicle_features: { make, model, wheel_style, ... }`, JSON schema was NOT updated to include it, every Qwen call returned valid JSON but with `vehicle_features` absent from every vehicle entry. The test that caught it was running `analyze_frames()` through the **production code path** (not a direct `curl` to the chat-completions endpoint — that path bypasses `response_format` enforcement entirely and WILL return the new field even when the schema rejects it). Always test structured-output changes through the production path that includes `response_format`, not the bare API. **Verification protocol**: edit prose + schema in the same change; restart the runtime; call the production function and assert the new field appears in the parsed response.

  **Mode 2 — schema has it, prompt doesn't ask (the mirror image).** The complement is equally silent and harder to spot because the runtime is happy: the field appears in the schema, the model returns the field but with `null` (or omit it entirely when `additionalProperties: false` lets it), and downstream consumers keep working with empty data. Observed in production (farm-surveillance, 2026-08-01 Phase 6B.46): `VEHICLE_MOTION_PROMPT_TEMPLATE` listed only `color, body_style_hint, make, model, motion, motion_justification`. The schema (`VISION_SCHEMA_JSON`) declared `vehicle_features: { wheel_style, roofline_style, ... }` as required (string-or-null). The model — no instruction to the contrary — returned `vehicle_features: null` on every call. The scored matcher had nothing to score on for months. **Verification protocol**: for every field in the schema, `grep -F "<field_name>" src/vision_analyzer.py` (or wherever the prompt lives); if the field appears in the schema but not in the prompt, the model will return null. The audit recipe in `farm-vision-6b19-multi-vehicle-motion`'s 2026-08-01 reference formalizes this.

  **Why both modes are dangerous**: silent. No log line, no error. Either mode breaks downstream features with no observable signal. The fix in BOTH cases is the same discipline — keep prompt and schema in lockstep — but the verification direction differs: for Mode 1, run production path and assert field appears; for Mode 2, grep prompt for every schema field.

| Schema has field? | Prompt asks? | Outcome |
|---|---|---|
| Yes | Yes | ✅ Model populates |
| Yes | **No** | ❌ **Model returns null** (Mode 2) |
| No | Yes | ❌ Runtime grammar strips (Mode 1) |
| No | No | ❌ Field absent |

  **Compounding pitfall — duplicate extractor (caught in 6B.46 too):** even after fixing Mode 2, you may still have a Bug B hiding: a downstream function that re-extracts the field from the JSON response and silently drops it. Always test the **end-to-end** production path: prompt → LLM → response dict → extraction function → matcher. Skipping the extraction layer in the test hides the duplicate-extractor drift.

- **Multi-frame token budget — the silent regression.** When the prompt grows (new rules, per-vehicle schema, motion justification) it can push the multimodal request over `n_ctx` on llama-server (default 8192). The HTTP response is `400 "exceeds the available context size"`. Client code that catches `HTTPError` and falls back to a single-frame call sees a *successful* parse of a *different* (legacy) prompt — no warning, no error, just a regression to the prior behavior. This is **silent**: the listener log shows `multi-frame analysis using VEHICLE_MOTION_PROMPT` followed by `multi-frame failed, falling back to single-frame first pass`, but no error. Observed in production (farm-surveillance, 2026-07-27 F150 arrival, Phase 6B.19 prompt): new prompt was ~4,500 chars vs old ~1,500 chars; 6×720p frames + new prompt = 13,459 prompt tokens, exceeded 8192 budget, listener fell back to old single-frame path, F150 was missed. **Mitigation**: before shipping any prompt change, run the verification recipe in `farm-vision-6b19-multi-vehicle-motion` §7 — build the multimodal payload, hit `llama-server`, and assert `prompt_tokens < n_ctx`. If the budget is tight, trim the prompt (terse enum lists, no narrative examples), reduce `n_frames`, or further downsize images. **Unit tests of the downstream matcher do NOT catch this** — they assert matcher behavior given a vision_result, not that the vision_result was produced by the right prompt. End-to-end smoke test (drive a vehicle past the gatekeeper, verify log shows no fall-back warning) is required.

## References

- **[references/vision-extraction.md](references/vision-extraction.md)** — Worked Qwen3-VL + llama.cpp example: schema, prompt, validator, retry loop, sampling
- **[references/schema-design.md](references/schema-design.md)** — JSON Schema pitfalls across GBNF/XGrammar/OpenAI strict, plus tested-safe patterns
- **[references/qwen3-vl-field-reliability.md](references/qwen3-vl-field-reliability.md)** — Reliability table for fields you might add to a vision-extraction schema (good/bad frame probes from farm-surveillance 2026-07-27). Use this BEFORE adding a field to decide whether the field is worth the prompt tokens.

## Related skills

- `llama-cpp` — local GGUF inference + multimodal server with `response_format`
- `outlines` — JSON Schema/Pydantic/regex constrained generation
- `guidance` — regex/select grammars, multi-step constrained flows
- `instructor` — Pydantic validation + automatic retry
- `serving-llms-vllm` — high-throughput serving with XGrammar backend
- `llava` — LLaVA-NeXT model setup