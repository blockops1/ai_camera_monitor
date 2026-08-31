# Qwen3-VL-8B field reliability reference

Reliability data for fields you might add to a vision-extraction JSON
schema for Qwen3-VL-8B-Instruct (UD-Q4_K_XL quantization, served via
llama-server `response_format: json_schema`). Captured 2026-07-27 during
Phase 6B.17 prompt redesign on farm-surveillance.

These findings are from **probes run before shipping the prompt change**
— see `vehicle-enrollment-corrected` skill §"Vision prompt design — probe
before you ship" for the probe pattern. Run your own probes for any
field you intend to add; this table is a starting point, not a guarantee.

## Setup

- Model: `/Users/<user>/llama-models/qwen3-8VL/Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf`
- Endpoint: `http://127.0.0.1:8080/v1/chat/completions`
- Frames tested: Tesla Model Y at Outside Front Solar (gatekeeper) on
  farm-surveillance property. Two frames per field:
  - **Good frame** (`data/test_runs/tesla_loop_20260725_131144/20260725_131623_crossing_point_power_and_solar/Outside_Front_Solar.jpg`)
    — Tesla front 3/4 angle at ~20 feet, partial side view, license
    plate visible, no occlusion.
  - **Bad frame** (`data/frames/c4187bb4-ece3-4006-bf40-06bb5c0e6ab4/frame_004.jpg`)
    — Tesla at ~40+ feet, partially occluded by dirt mound in
    foreground, upper-left corner of frame.
- Schema: `response_format.json_schema.strict=true`, every field required,
  nullable fields use `"type": ["string", "null"]`.

## Reliability table

| Field | Good frame | Bad frame | Reliability | Notes |
|---|---|---|---|---|
| `make` | `Tesla` | `Tesla` | **HIGH** | Survives quality degradation. Strongest single discriminator. |
| `model` | `Model Y` | `Model 3` | MEDIUM | Model-3/Model-Y confusion at distance. Treat as soft signal, not hard discriminator. |
| `body_color_perceived` | `dark blue` | `black` | MEDIUM | Dark blue → black at distance. **Could fix by adding `black` to known vehicle's `colors_alt`, but be careful about bleed (Pitfall 5 in vehicle-enrollment-corrected).** |
| `body_color_self_reported` | `dark blue` | `null` | MEDIUM | Qwen correctly admitted null on the bad frame rather than guess. |
| `roofline_style` | `fastback` | `sedan_traditional` | LOW | Depends heavily on angle/distance. **Model Y is fastback but Qwen reads it as sedan_traditional at distance.** |
| `wheel_style` | `aero_cover` or `alloy` | n/a | **INCONSISTENT** | Same frame returned `aero_cover` once, `alloy` on a re-probe. Don't use as hard discriminator; usable as soft "EV-like" hint. |
| `wheel_color` | `body_color` | `body_color` | HIGH | Color stable when visible. |
| `front_grille_style` | `closed_blank` | n/a (rear/bad frame) | MEDIUM | Only meaningful when front visible. **EV discriminator: closed_blank = Tesla/Rivian/etc.** |
| `headlight_signature` | `slim` / `LED_bar` | n/a | MEDIUM | Varies between probes. Only when front visible. |
| `rear_lights_signature` | `LED_bar` (when rear visible) | n/a | MEDIUM | Symmetric to headlight; prompt should ask for both depending on angle. |
| `tailgate_type` | `liftback` | `null` | LOW | Only sometimes determined even on rear frames. |
| `badge_text_readable` | `Model Y` | `null` | LOW-MEDIUM | Only on good frames. Useful when present. |
| `body_style` | `sedan` | `sedan` | LOW | **Model Y is a fastback SUV but Qwen always reports `sedan`. Mirrors Pitfall 1 in vehicle-enrollment-corrected.** Don't use as a discriminator. |
| `cab_doors_visible` | `2` | `0` | LOW | Too angle-dependent. Not useful. |
| `cab_configuration` | `null` | `null` | LOW | Qwen almost never returns this even on good frames. |
| `ground_clearance_perceived` | `low` | `low` | MEDIUM | Consistent when visible but not discriminating (Tesla IS low, but Model 3 sedan is also low). |
| `hood_shape` | `scooped` / `domed` | `domed` | LOW | Inconsistent between probes; not useful. |
| `windshield_rake` | `moderate` | n/a | MEDIUM | Stable when visible, but not very discriminating. |
| `side_mirrors` | `body_color` | `body_color` | HIGH | Stable but not discriminating. |
| `side_stepboards` | `false` | `false` | HIGH | Reliable boolean. |
| `has_visible_roof_rack` | `false` | `false` | HIGH | Reliable boolean. |
| `has_trailer_hitch_receiver_visible` | `false` | `false` | HIGH | Reliable boolean. |
| `is_towing_a_trailer` | `false` | `false` | HIGH | Reliable boolean. |
| `paint_finish` | `gloss` | `gloss` | HIGH | Reliable when visible. |
| `overall_confidence` | `0.95` | `0.85` | MEDIUM | Qwen's self-reported confidence. Don't trust blindly. |

## What this means for prompt design

**Tier 1 — keep these (HIGH reliability):**
- `make` — the single most useful field. Survives bad frames.
- `wheel_color`, `paint_finish`, `side_mirrors`, `side_stepboards`,
  `has_visible_roof_rack`, `has_trailer_hitch_receiver_visible`,
  `is_towing_a_trailer` — reliable booleans/colors.

**Tier 2 — add as soft signals (MEDIUM reliability):**
- `model` — soft signal, not hard discriminator (Model 3 vs Y confusion).
- `body_color_perceived` — useful but unreliable at distance.
- `roofline_style` — useful when good, but angle-dependent.
- `front_grille_style` / `headlight_signature` / `rear_lights_signature` /
  `tailgate_type` — front/back symmetry; prompt should ask for the
  set appropriate to the camera angle.
- `badge_text_readable` — strong signal when present, often null.

**Tier 3 — skip these (LOW or INCONSISTENT reliability):**
- `body_style` — fastback SUVs always misreported as `sedan`.
- `cab_doors_visible`, `cab_configuration` — too unreliable.
- `wheel_style` — inconsistent on the same frame.
- `ground_clearance_perceived` — not discriminating.
- `hood_shape` — varies between probes.

## Per-vehicle motion — Qwen3-VL-8B reliability (2026-07-27, Phase 6B.19)

Captured during the multi-vehicle-scene fix where parked Tesla was
dominating the bbox and the moving F150 was missed. Tested on Outside
Front Solar gatekeeper frames at 720p, 4 frames downsized from 4K.

| Field | Reliability | Notes |
|---|---|---|
| `motion` per vehicle | **HIGH** | Given per-vehicle fields in prompt + `motion_justification`, Qwen reliably classifies each vehicle. Without `motion_justification`, defaults to scene-level "stationary". |
| `motion_justification` | **HIGH** | Forces frame-by-frame reasoning. Required to break the parked-car dominance trap. |
| `moving_vehicle_indices` (top-level) | **HIGH** | Qwen correctly populates this when per-vehicle motion is required. Empty list is meaningful (no movers). |
| All vehicles in scene | **MEDIUM** | Qwen may still omit secondary vehicles when they are <2% of frame area. Mitigation: ask for "every distinct vehicle" explicitly + require `bbox` in schema so small vehicles still get listed (Qwen has to draw a box for them). |

**The trigger anchor sentence is critical.** Adding
*"The trigger is known to be caused by at least one moving vehicle;
therefore at least one entry must receive motion='moving' unless the
entire scene is static"*
prevents the failure mode where Qwen returns `motion: "stationary"` for
all vehicles when the camera's on-device AI clearly classified the
event as VEHICLE in motion.

Full prompt + schema + matcher details:
`~/.hermes/skills/farm-vision-6b19-multi-vehicle-motion/SKILL.md`

## The probe pattern (recap)

```python
import base64, json, urllib.request, sys

frame_path = "/abs/path/to/test_frame.jpg"
with open(frame_path, "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

probe_prompt = """Inspect the vehicle in this frame. For each field,
answer ONLY what you can actually see. If undeterminable, return null —
do not guess.

Return JSON: {
  "make": "..." | null,
  "model": "..." | null,
  "wheel_style": "alloy" | "steel" | "aero_cover" | ... | null,
  "...": ...
}"""

req = urllib.request.Request(
    "http://127.0.0.1:8080/v1/chat/completions",
    data=json.dumps({
        "model": "/Users/<user>/llama-models/qwen3-8VL/Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": probe_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            ]
        }],
        "max_tokens": 2000,
        "temperature": 0.1,
    }).encode(),
    headers={"Content-Type": "application/json"},
)
result = json.loads(urllib.request.urlopen(req, timeout=180).read())
print(result["choices"][0]["message"]["content"])
```

## When to re-run this probe

- New camera angle on the property (e.g. a new gatekeeper camera added)
- New vehicle class (truck vs sedan vs motorcycle) — reliability
  varies by class
- Qwen model upgrade (Qwen3-VL-9B or quant change may shift reliability)
- Lighting conditions change (winter daylight vs summer sun)

Probes cost ~30-40 seconds per frame on a 4K Tesla frame. Run 3-4
probes per session that adds a new field. Don't trust the first
answer — re-run on the same frame to detect inconsistency (like
`wheel_style` returning different values on the same image).

## Multi-frame token budget — before shipping any prompt change

This is a separate concern from per-field reliability: the **whole
multi-frame request** must fit in `n_ctx` (default 8192). Verify before
shipping a prompt change. See `farm-vision-6b19-multi-vehicle-motion`
§7 for the verification recipe.

Empirical data points (Qwen3-VL-8B, llama-server, 720p frames):

| Configuration | Prompt tokens | Status |
|---|---|---|
| 2×720p + old prompt (~1,500 chars) | ~2,100 | OK |
| 4×720p + new prompt (~4,500 chars) | ~3,055 | OK |
| 6×720p + new prompt (~4,500 chars) | ~3,979 | OK |
| 6×4K (3840×2160) + new prompt | **13,459** | **OVER BUDGET** |

**Gotcha**: production code calls `downscale_for_qwen` (720p) so the
"6×4K" case never happens in production. But the new prompt pushed
the total from a comfortable ~3,200 to ~3,979 tokens, leaving only
~4K tokens of headroom for response and KV cache. Any future prompt
additions should be measured against this.