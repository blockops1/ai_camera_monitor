---
name: alert-bucket-cooldown
description: "Use when: an alert pipeline has a UUID-keyed cooldown that fails to suppress repeated alerts with fresh IDs (LLM-driven webhooks, retry storms, sensor re-triggers). The fix is a SECOND cooldown layer keyed on (source, semantic-bucket) where semantic-bucket is the first N chars of the title or a similar stable normalized form."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [alerting, cooldown, dedup, llm-pipeline, telegram, spam-suppression]
    related_skills: [software-development-practices, farm-vision-alert-routing]
---

# Alert Bucket Cooldown — kills ID-churn alert floods

## When to use

Your alert pipeline has a cooldown mechanism keyed on a per-event ID
(UUID, request_id, webhook_id), but the alerts keep firing because each
new event generates a fresh ID. Symptoms:

- Alert count grows linearly with webhook count, not with real events.
- The same physical condition (parked vehicle, stuck sensor) triggers
  dozens of alerts per hour.
- Existing cooldown logs show "Suppressed (cooldown): ..." entries —
  but the suppressed count is tiny compared to the sent count.
- LLM-generated titles vary slightly each time ("Unknown vehicle with
  headlights on at night" vs "Unknown vehicle with headlights on at
  property"), but the underlying event is identical.

This is the **Building Back Solar overnight vehicle flood pattern** on
farm-surveillance — see
`local-ai/farm-vision-alert-routing/references/solar-camera-l1-flood-pattern.md`
for the canonical case study.

## Why the existing cooldown fails

UUID-keyed cooldown works for retry storms (same UUID within seconds =
duplicate) but **fails completely** for ID-churn patterns where each
new event legitimately has a new UUID. The cooldown key needs to be
something stable across runs of the same physical event.

## The fix (two-layer cooldown)

Keep the existing per-ID cooldown (it still handles retry storms).
Add a SECOND layer keyed on `(source, semantic_bucket)` where
`semantic_bucket` is a stable normalized form of the alert's
identifying content — typically the first N chars of the title, lowercased.

```python
DEFAULT_BUCKET_COOLDOWN = 1800  # 30 minutes
TITLE_BUCKET_PREFIX_LEN = 30
_bucket_cooldown: dict[str, float] = {}

def _make_bucket_key(alert: dict) -> str:
    source = alert.get("source", "")  # e.g. camera name, sensor ID
    title = alert.get("title", "")
    if not source or not title:
        return ""  # no bucket = skip the bucket check
    bucket = title[:TITLE_BUCKET_PREFIX_LEN].strip().lower()
    return f"{source}|{bucket}"

def notify(alert, ...):
    # ... existing ID-cooldown check first ...
    if _is_in_cooldown(alert_id, cooldown_seconds):
        return True

    # NEW: bucket cooldown second
    bucket_key = _make_bucket_key(alert)
    if bucket_key and _is_in_bucket_cooldown(bucket_key, bucket_cooldown_seconds):
        log.info(f"Suppressed (bucket cooldown): {alert['title']}")
        return True
    # ... send ...
```

## Design choices and why

| Choice | Why |
|--------|-----|
| **Two layers, not replacement** | Preserves the ID-cooldown safety net for retry storms. New bucket layer is purely additive. |
| **30-minute default** | Long enough to suppress overnight flood (00:00–06:00 = 6h of churn); short enough that a real second event at 00:35 still fires if you set it lower. Tune to your noise pattern. |
| **First 30 chars of title, lowercased** | Captures LLM paraphrasing of the same event ("Unknown vehicle with headlights on at night" vs "Unknown vehicle with headlights on, parked") into one bucket. 30 chars is enough to span natural rephrasings but short enough to discriminate genuinely different categories ("Unknown vehicle..." vs "Person at back door..."). |
| **Source as part of the key** | Prevents cross-source suppression when two sensors legitimately see the same event type. Without `source`, both cameras detecting the same physical event would collide. |
| **Empty key = skip check** | Alerts without `camera` or `title` skip the bucket layer cleanly. They still hit the ID-cooldown. |
| **In-memory state, no disk** | Acceptable for short windows (30min). Restart costs at most one duplicate flood per camera per restart. Don't bother with SQLite/Redis until you need cross-restart persistence. |

## Third layer: global rate-limit on an optional sub-message

Pattern from `<infra>/notifier.py:300-324` (Phase 6B.101,
2026-08-19). When the alert pipeline sends **multiple Telegrams per alert**
(photo + vision-block + body), a single chat can be flooded on a busy day even
though every individual alert is correct. The alert body and photo must always
send. The optional sub-message (e.g. `🔍 VISION_OBSERVATIONS`) is rate-limited
**globally** — one Telegram per N minutes across all alerts and all cameras.

```python
# Third map, single global key. Independent of _alert_cooldown and _bucket_cooldown.
_vision_block_cooldown: dict[str, float] = {"_last_sent": 0.0}

def is_in_vision_block_cooldown(cooldown_seconds: int = 1800) -> bool:
    now = time.time()
    with _cooldown_lock:
        last = _vision_block_cooldown["_last_sent"]
        if last > 0 and (now - last) < cooldown_seconds:
            return True
        _vision_block_cooldown["_last_sent"] = now
        return False

# In notifier.py
if vision_text:
    if is_in_vision_block_cooldown():
        log.info(f"[{alert_id}] vision_block suppressed (global cooldown)")
    else:
        send_message(bot_token, chat_id, f"🔍 {vision_tag}\n\n{vision_text}")
```

| Choice | Why |
|--------|-----|
| **Single global key, not per-camera** | User ask: "I just don't need one more than every 30 minutes" — global is what they want. If you want per-camera, the per-bucket pattern above gives that. |
| **Independent map, same lock** | Reusing the existing `_cooldown_lock` is fine (one lock already covers all maps). Don't introduce a new lock per map — that invites deadlocks. |
| **Silent in the user-facing channel, log at INFO** | Don't surface "suppressed" to the user — that creates its own spam. Audit the suppression in the log so you can confirm it's working post-deploy. |
| **Env-var override the window** | `FARM_VISION_BLOCK_COOLDOWN_SECONDS=300` for 5 min, `=3600` for 1 hour. Lets you tune without a code change. |
| **Only throttle the optional sub-message** | Never throttle the alert body itself — that defeats the alert pipeline. Only throttle nice-to-have extras that duplicate info already in the body. |

**When to reach for this pattern:**
- The user reports "I got too many `[CHANNEL] [SUBTYPE]` messages today."
- The sub-message content is a *duplicate* of the alert body (e.g. vision-block
  restates "person standing near vehicle" which the body already says).
- The user explicitly wants a global, simple rate-limit, not per-camera.
- The cooldown is a "polish" — never use it on critical channels where missing
  a message has consequences.

**When NOT to reach for this pattern:**
- The user is actually being annoyed by the alert body itself. Fix the
  upstream classifier (demote to L0/log-only) instead of muting the channel.
- The "spam" is genuinely different events (one per camera per minute). A
  global rate-limit would silence a real fleet activity spike.

## Layer 0: pre-gate, event-type-keyed cooldown (added 2026-08-28, §11.77)

A **third** sibling layer that fires **before** any other cooldown or work —
right at the top of the alert handler. Key is `(camera, event_type)` not
`(source, semantic_bucket)` and not `alert_id`. Resolves the flood at the
cheapest possible point: no frames captured, no YOLO, no LLM call, no Telegram,
no audit row.

```python
# infra/gate_cooldown.py (Phase 6B.154, 2026-08-28)
def is_in_gate_cooldown(
    camera_name: str, event_type: str, window_seconds: int = 0,
) -> tuple[bool, float]:
    """Check + record (camera, event_type) cooldown at the gate."""
    window = window_seconds or _resolve_window_from_config(camera_name, event_type)
    key = (camera_name, _normalize_event_type(event_type))
    now = time.monotonic()
    with _lock:
        last = _last_seen.get(key, 0.0)
        if window > 0 and last > 0.0 and (now - last) < window:
            return True, last   # hit — clock keeps ticking from first alert
        _last_seen[key] = now
        return False, last     # miss — record timestamp

# listener/listener.py: _process_alert() — very top, BEFORE output_dir creation
in_cooldown, _ = is_in_gate_cooldown(camera_name, event)
if in_cooldown:
    log.info(f"[{alert_id}] gate_cooldown: suppressed ...")
    return  # exit cleanly — no gate, no pipeline, no Telegram
```

**Config schema** (extends `motion_gate_thresholds.json` alongside
`gate_enabled`, per Phase 6B.152):

```json
"Outside Front Garage": {
  "gate_cooldown": {
    "vehicle": 60,
    "person": 120,
    "motion": 180,
    "default": 120
  }
}
```

**Resolution order** (first non-None wins):
1. Explicit arg `window_seconds` (rare; tests)
2. `[camera][gate_cooldown][event_type]` (after normalizing `people` → `person`)
3. `[camera][gate_cooldown][default]`
4. Module default `0` = no cooldown (full backward-compatibility)

| Choice | Why |
|---|---|
| **Pre-gate, not post-pipeline** | The whole point: don't spend frames + YOLO + LLM cost on noise. Bucket cooldown is post-pipeline — too late for the cost-saving benefit. |
| **Key = (camera, event_type), not (source, title)** | Title isn't known yet at this point — it's an LLM output. Event type is in the webhook payload. |
| **Hit doesn't reset the clock** | The cooldown measures from the FIRST alert of the run; subsequent suppressed alerts extend nothing. Reset on listener restart (in-memory map only, by design). |
| **Cache invalidation via listener restart** | Config is cached on first read; tests use `clear_all_gate_cooldowns()` to reset. Production changes need a restart, same as `gate_enabled` (Phase 6B.152). |
| **`people` → `person` normalization** | Reolink payload form vs. motion_gate_pipeline key form. Same convention as `gate_enabled`. |
| **`default` field is the per-camera fallback** | Catches event_types not explicitly listed. Module default `0` is the global fallback when no per-camera config exists. |
| **In-memory only** | Matches `infra/cooldown.py` semantics; no SQLite/Redis until cross-restart persistence is needed. |

**When to reach for this layer:**
- The user reports "I got too many webhooks today" / "we are going to get a lot of notifications today" — and the cost is not just Telegram spam, it's frame + YOLO + LLM cost on every re-trigger.
- Per-camera flood profile varies (OFS vehicle spam ≠ OFG person spam ≠ OBS motion spam) — a global rate-limit would silence legitimate fleet activity spikes.
- The webhook is a known repeating source (Reolink motion-trigger on a windy tree, IR reflection at dawn) — i.e. you want to suppress at the entry point, not at the bucket layer.

**When NOT to reach for this layer:**
- The flood is genuine **different** events happening in quick succession (one alert per camera per minute). A 60s gate_cooldown would silence a real fleet activity spike — use the bucket layer instead, which keys on (source, semantic_bucket) and lets distinct titles through.
- The cost you care about is the Telegram itself, not the upstream work. Bucket cooldown handles that.
- The gate cooldown is a **flood suppressor**, not a deduplicator. For dedup of similar-but-not-identical titles, use the bucket layer.

**Don't conflate the three layers:**

| Layer | Key | Fires at | Suppresses | Cost avoided |
|---|---|---|---|---|
| **0. gate_cooldown** | `(camera, event_type)` | Pre-gate (very top of `_process_alert`) | The whole pipeline | Frames, YOLO, LLM, Telegram |
| 1. ID cooldown | `alert_id` (UUID) | Per-alert check | Retry storms of the same UUID | LLM re-calls on identical webhook retries |
| 2. Bucket cooldown | `(source, semantic_bucket)` | Post-pipeline (in notifier) | Duplicate Telegrams for paraphrased titles | Telegram sends + audit spam |

Use all three. They guard different concerns. None replaces another.

**Tests to write** (same shape as bucket layer):

1. **Pre-gate suppression:** call `is_in_gate_cooldown("OFG", "vehicle")` once → False; call again within 60s → True; log message includes alert_id.
2. **Per-(camera, event_type) independence:** `is_in_gate_cooldown("OFG", "vehicle")` and `is_in_gate_cooldown("OFS", "person")` — different keys, both False.
3. **Window expiry:** set 1s window, call once, sleep 1.1s, call again → False (clock expired).
4. **Caller-arg override:** config says 9999s, caller passes `window_seconds=1` → 1s window applied.
5. **`clear_all_gate_cooldowns()` resets both the map and the config cache** so tests don't leak state across runs.
6. **Thread safety:** 8 concurrent threads × 50 calls → no map corruption, no spurious `(True, 0.0)` pairs (a hit with no previous timestamp).
7. **Malformed config tolerance:** bad JSON, non-dict camera values, non-numeric window values → all return 0 (no cooldown; never crash the listener).

## Tests to write (TDD)

Three required tests, all asserting observable behavior:

1. **Flood suppression:** call notify() 10× with same (camera, title) but different alert_ids → exactly 1 Telegram send.
2. **Cross-source independence:** call notify() with same title across 3 different cameras → exactly 3 Telegram sends (different buckets).
3. **Cross-bucket independence (same source):** call notify() with 2 different titles for the same camera → exactly 2 Telegram sends (different buckets).

Add a rough/pie integration test in addition to unit tests — the wire
between `notify()` and Telegram is where this kind of bug hides. See
`software-development-practices` § "Rough / Pie Integration Tests".

For the **global vision-block sub-message** layer (third layer, § Third layer above):

1. **First-call send:** call notify() once with vision result → vision sub-message is sent.
2. **Subsequent-call suppress within window:** call notify() 5× more in the same window → all suppressions are silent in Telegram, all log at INFO.
3. **Independent of alert/bucket cooldowns:** firing the vision-block cooldown must not affect the ID-cooldown or bucket-cooldown maps (and vice versa). Test all three maps with interleaved calls and assert each map's behavior is unaffected.
4. **Window expiry resets:** set a 1-second window, call once, sleep 1.1s, call again → second call returns False (sends).
5. **`clear_all_cooldowns` resets the new map** along with the existing two.
6. **Global, not per-camera:** simulate two alerts from different cameras in the same window → first sends, second suppresses. The map has only one timestamp, no per-camera key.

## Verification (after deploy)

```bash
# Before fix: 80+ "Sent Level 1 alert" entries with flood title overnight
grep "Sent Level 1 alert" logs/listener.log | grep "$(date +%Y-%m-%d)" \
  | grep -c "Unknown vehicle with headlights on at night"
# Expect: 1 (one real vehicle) or small handful — not 80+
```

If still >5, the bucket regex is too strict — LLM is varying the prefix
beyond 30 chars. Inspect actual titles, widen the prefix, retest.

## Don't do this

- ❌ Replace the ID-cooldown with bucket-cooldown (loses retry-storm dedup).
- ❌ Hash the full title (LLM rephrasings hash differently).
- ❌ Bucket only on `camera` (loses discrimination — two different alert types from same camera collide).
- ❌ Set window too long (real events get suppressed).
- ❌ Set window too short (re-floods immediately).
- ❌ **Use bbox area or pixel-size heuristics for "is this close enough"**. The
  vision LLM already judges closeness in scene_description / bbox_size
  language. Bbox math adds a brittle threshold that can silently suppress
  real threats. Trust the LLM, like `_vision_sees_person` already does.
  (User correction 2026-07-22 on farm-surveillance: explicitly rejected
  bbox-area calculations for distance judgement.)

## Companion patterns

- **vision_analyzer "still parked" detector** — second layer of defense.
  Bucket cooldown stops the duplicate Telegram sends; this stops the
  duplicate LLM calls upstream.
- **Per-camera hour-of-day traffic shaping** — drop alerts at hours when
  you know the camera fires false positives (e.g. dawn/dusk IR
  transitions). Use this if bucket cooldown alone isn't enough.
- **Lower threat level for parked-vehicle-at-night titles** — third
  layer. Demote "parked vehicle" titles from L1 to L0 unless
  inter-frame motion exceeds a threshold.

## Distinguishing from baseline overrides

The bucket cooldown is a **dedup layer** — it suppresses duplicate
sends but preserves the LLM's L1 classification (the alert is logged,
recorded, just not re-sent). For some cameras that's not enough: the
L1 itself is wrong (LLM hallucinated headlights on a parked vehicle,
or elevated a tarp on the ground to "unknown object at night"). For
those, you need a **baseline override** that demotes L1 → L0 — see
`local-ai/farm-vision-alert-routing/references/static-object-baseline-pattern.md`.

**Different fix classes — don't conflate:**
- Bucket cooldown → alert fires once per (camera, title) per window, then suppresses.
- Baseline override → alert is structurally wrong for that camera, demoted to L0.

Use a baseline override when the noise pattern is permanent and the
LLM's L1 verdict is unreliable on that camera (parked vehicles, static
environmental objects, distant county-road traffic). Use bucket
cooldown when the alert is correct but repeated (sensor re-trigger,
LLM paraphrase churn).