# ARCHITECTURE.md

> Deep architectural doc — what is working **now**.
>
> For the operator's cheat-sheet + install guide, see [`README.md`](README.md).
> For the module-purity review + cutover plan, see [`PLAN.md`](PLAN.md).
> For the visual module call graph, see [`listener-architecture.html`](listener-architecture.html)
> (regenerate via `./.venv/bin/python scripts/generate_architecture_diagram.py`).

## 1. Purpose

`README.md` answers "what is this repo and how do I run it" in ~5 minutes
and includes the install + config guide for someone setting this up
for themselves.

`ARCHITECTURE.md` answers "how do the pieces fit together, and what do
I need to know to debug or extend them" in ~20 minutes. Every claim
here describes the **current** state of the code on `main`. Historical
reasoning lives in `git log` and commit messages; module contracts
live in each module's header docstring (per `AGENTS.md` §1.5).

**Status note (2026-08-26):** the motion gate (Phase.107–6B.109 +
6B.115 + 6B.116b) is now the dominant pre-filter; ~99% of noise is
suppressed before any LLM call. ARCHITECTURE.md §2.1 below still
describes the **post-gate** flow — for the gate itself see the README
"Module structure" section + `listener/motion_gate_pipeline.py`
docstring, which is the authoritative contract. A full §2 rewrite
covering the gate is tracked in `PLAN.md` open questions.

---

## 2. Data flow & state machines

Three state machines run continuously. They share the Flask app thread
for ingress/egress but own distinct background workers for processing.

### 2.1 Inbound webhook (one per `POST /alert`)

```
                  ┌────────────────────────────┐
                  │ Flask app thread (listener)│
                  │ _normalize_payload         │
                  │ _classify_queue            │
                  └─────────────┬──────────────┘
                                │
                  ┌─────────────▼──────────────┐
                  │ per-camera semaphore       │◄─── infra.camera_queue
                  │ acquire_for_camera         │
                  └─────────────┬──────────────┘
                                │
                  ┌─────────────▼──────────────┐
                  │ _ClassedWebhookExecutor    │◄─── listener._ClassedWebhookExecutor
                  │ N=4 workers, queue=64      │     (replaces _BoundedWebhookExecutor, 6B.16)
                  └─────────────┬──────────────┘           (max_workers matches llama-server --parallel 4)
                                │
                                ▼
                  ┌───────────────────────────┐
                  │ motion_gate (YOLOv8n)     │◄─── listener.motion_gate_pipeline + _motion_gate_dispatch
                  │ runs on captured frames   │     (Phase.107+; gate is sole producer of frames/crops)
                  │ returns GateVerdict       │     (MOTION_GATE_V2=1 enables 6B.109 fixes)
                  └─────────────┬──────────────┘
                                │
                  ┌─────────────▼──────────────┐
                  │ routing decision          │◄─── listener.py 1656-1716
                  │ event + gate verdict      │
                  │ → vehicle pipeline (OFS)  │     6B.129a: promote md→vehicle when gate=vehicle
                  │ → person pipeline  (OFG)  │     6B.145: promote md→people when gate=person
                  └─────────────┬──────────────┘
                                │
        ┌───────────────────────┴─────────────────────────┐
        │                                                 │
   vehicle_event_pipeline                          person_event_pipeline
        │                                                 │
        ▼                                                 ▼
   identify_from_crops                              identify_from_crops
   (3-image Qwen payload:                           (3-image Qwen payload:
    streak_A, streak_B, diff)                        streak_A, streak_B, diff)
        │                                                 │
        ▼                                                 ▼
   match_vehicle_scored                             ArcFace match (if Phase 6A on)
   (15-dim via legacy adapter)                       ├─ match → known person
        │                                              └─ no match → "Unknown person"
        ├─ score ≥ threshold → known vehicle
        └─ score < threshold → unknown vehicle
              │
              ▼
        alert_generator (Qwen3.6)                  alert_generator (Qwen3.6)
              │                                                 │
              ▼                                                 ▼
        Telegram (vehicle_alert)                    Telegram (person_tracker)
        2-image album (streak crops)               6-image album (4 frames + 2 crops)
                                                   + 2s pre-event trail on OFG (6B.143)
```

**Data classes that flow between stages:**

- `GateVerdict` (`listener/motion_gate_pipeline.py`) — `{decision, class_label, confidence, is_suppress, crop_paths[], bbox, frame_timestamps[], pairwise_diff_path, ...}` (6B.144 added `pairwise_diff_path`; 6B.115 made the gate the sole producer of frames/crops).
- `VisionResult` (`infra/vision_response.py`) — Qwen's structured output: `vehicles[]`, `colors`, `make`, `model`, `confidence`, `vehicle_features{}`, `body_style_hint`. Wrapped by the listener in `{vehicles: [_mv], primary_vehicle_index: 0}` (§11.17.A.1).
- `MotionResult` (`infra/motion_detector.py`) — `bbox_per_frame`, `frame_scores`.
- `Sig` (`vehicle_identifier/signature.py`) — flat dict. Fields: `color`, `type`, `make`, `model`, `badge_text_readable`, `vehicle_features.*` flattened to top level, `description`, `confidence`.
- `MatchDetail` (`infra/vehicle_matcher.py:222`) — frozen-ish dataclass: `kv`, `score`, `gap`, `reasons`, `matched_dim_weights`.
- `Alert` (`infra/alert_generator.py`) — `{threat_level, summary, ...}`. `threat_level=-1` is the error sentinel.

### 2.2 Vision request (one per Qwen call)

```
   analyze_frames_queued
        │
        ▼
   vision_queue.enqueue   ── priority ──►   vehicle > person > animal > motion
        │
        ▼
   vision_client._post_to_vision (single httpx path; pool removed in 6B.147)
        │   Authorization: Bearer *** when VISION_LLM_TOKEN set
        ▼
   vision_client.httpx POST  ──►  Vision LLM at /v1/chat/completions
        │   URL from infra.llm_config (VISION_LLM_URL or default 127.0.0.1:8093)
        ▼
   vision_response.parse_and_recover  (retry on JSON parse failures)
        │
        ▼
   VisionResult → cached in vision_cache (60s TTL, see infra/vision_cache.py)
        │
        ▼
   returned to caller; vision_queue slot freed
```

**Backpressure:** `vision_queue` has a fixed capacity. On overflow, the
matcher stage receives `None` and the alert fires with `threat_level=-1`
(see `infra/alert_overrides_baseline.py` for the override path). The
matcher does not block on vision.

### 2.3 Telegram emit (one per alert that survives the gates)

```
   generate_alert → Alert{threat_level}
        │
        ▼
   alert_overrides_baseline + alert_overrides_offhours
        │ (may demote L1→L0 or suppress entirely)
        ▼
   infra.quiet_hours.in_quiet_hours(now, camera)  ──── 21:00–07:00 ET for outside cameras
        │
        ├── quiet → log "QUIET_HOURS_SUPPRESSED" + audit; no Telegram call
        └── not quiet
              │
              ▼
         infra.notifier.notify(alert)
              │
              ▼
         cooldown_bucket_check (per-camera 120s + per-bucket 300s)
              │
              ├── cooldown active → skip (audit-logged)
              └── ok
                    │
                    ▼
               infra.send_telegram  ──►  up to 3 Telegram messages
                                      (photo, vision report, alert text)
                    │
                    ▼
               audit.jsonl line per send
```

Every outcome (suppressed, cooldown-skip, sent, error) is recorded in
`data/audit.jsonl` for forensics.

---

## 3. The matcher

Three things the matcher does: turns a vision result into a flat `Sig`,
scores every `KnownVehicle` against that `Sig`, returns the top match.
All weights live in `DEFAULT_SPEC` (`infra/matcher_spec.py:67`); no
hardcoded scoring lives in `matcher_scoring.py`.

### 3.1 Signature schema (`Sig = dict[str, Any]`)

| Field | Source | Matcher uses for |
|---|---|---|
| `color` | Qwen `colors`[0] (normalized) | Color match |
| `type` | Qwen `body_style_hint` | Type-group matching |
| `make` | Qwen `make` | Make match |
| `model` | Qwen `model` | Model substring match |
| `badge_text_readable` | Qwen `vehicle_features.badge_text_readable` | Tie-break |
| `vehicle_features.*` | flattened to top level | Per-feature scoring |
| `description` | Qwen `description` | Tie-break |
| `confidence` | Qwen `confidence` | Logging only (not scored) |

Empty `Sig` (no color/type/make/model) → `is_empty_signature(sig)` returns True → matcher skipped.

### 3.2 `DEFAULT_SPEC` structure (`infra/matcher_spec.py`)

```python
{
    "version": 1,

    # Type-group strategy: matches within a group, never across groups.
    # sedan/coupe/hatchback are SEPARATE from vehicle deliberately —
    # the 2026-07-21 lesson: better unknown than wrong.
    "type_groups": {
        "vehicle":    ["pickup", "suv", "truck", "van"],
        "sedan":      ["sedan", "coupe", "hatchback"],
        "motorcycle": ["motorcycle"],
    },

    # Color normalization: applied to BOTH sig.color and kv.color
    # before any comparison. Aliases normalize to canonical.
    "color_normalization": {
        "blue":  ["blue", "navy", "dark blue", "dark_blue", ...],
        "gray":  ["gray", "grey", "silver", "charcoal", ...],
        "white": ["white", "pearl", "cream"],
        "black": ["black", "ebony"],
        ...
    },

    # Pass definitions, walked in order. Each pass has:
    #   fires_when:    predicate on Sig field presence
    #   scoring:       list of {condition, points, against} tuples
    #   tie_break:     "color_match_first" / "model_first" / etc.
    #   no_fallthrough: True means stop at first match (default)
    "passes": [
        {"name": "make_model", "order": 1, ...},
        {"name": "make_only",  "order": 2, ...},
        {"name": "color_type", "order": 3, ...},
        ...
    ],

    # Per-dimension weights. Color asymmetry (per 2026-08-16 spec):
    # color_match: +0.7 (reward)
    # color_mismatch: -4.0 (penalty)   ← ratio = 5.71x
    # This is intentionally lopsided to dominate the make+model stack
    # for white-truck-vs-white-camper edge cases.
    "dim_weights": {
        "color_match":      +0.7,
        "color_mismatch":   -4.0,
        "type_match":       +0.8,
        "make_match":       +2.0,
        "model_match":      +3.0,
        "model_in_label":   +1.5,
    },

    # Acceptance threshold. Top match must beat runner-up by this gap.
    "min_score_gap": 0.6,
}
```

### 3.3 `KnownVehicle` schema (`data/vehicles/known_vehicles.json`)

```json
{
  "vehicles": [
    {
      "id":              "v_carson_white",
      "label":           "name two's white pickup",
      "owner":           "name two",
      "color":           "white",
      "colors_alt":      ["pearl", "cream"],
      "type":            "pickup",
      "make":            "Chevrolet",
      "model":           "Silverado 1500",
      "model_aliases":   ["Silverado"],
      "distinctive_features": [...],
      "vehicle_features": {
        "wheel_arch":     "outside_flare",
        "wheel_style":    "stock",
        "cab_marker_lights": false
      },
      "match_priority":  "color_type_then_make_model",
      "verified":        false,
      "verify_note":     "..."
    },
    ...
  ],
  "_comment": "Source of truth for vehicle identity. Edited by hand, validated by `known_vehicles/store.py` on load."
}
```

Notes:
- `colors_alt` lets a single enrolled vehicle absorb lighting drift
  (e.g. name two's "white" pickup reading as "pearl" or "cream" depending
  on sun angle).
- `vehicle_features.*` is flattened into the `Sig` at extraction time;
  per-feature scoring keys come from this dict.
- `verified: false` means the enrollment is from a synthetic capture
  and needs physical re-verification before being trusted.

### 3.4 Matcher scoring loop (`infra/matcher_scoring.py`)

For each (Sig, KnownVehicle) pair:
1. Filter by `type_groups` — drop kv if its `type` is in a different group than `sig.type`.
2. Apply `color_normalization` to both `sig.color` and `kv.color`.
3. Walk `passes` in `order`. For each pass that fires (its `fires_when` matches), apply `scoring` rules cumulatively.
4. Add `dim_weights` contributions to produce a final `score`.
5. Track `matched_dim_weights` for `reasons` display.
6. After all passes, compute `gap = top1.score - top2.score`. Accept if `gap >= min_score_gap`.

The first match that hits `score >= min_score` with a sufficient gap
fires; subsequent passes are skipped via `no_fallthrough: True`.

---

## 4. Persistence layout

### 4.1 Gitignored (runtime state, not source of truth)

| Path | Writer | Format | Purpose |
|---|---|---|---|
| `data/audit.jsonl` | `infra/audit.py` | one JSON object per line | Every webhook + every stage outcome. Forensics. |
| `data/alerts/alert_history.jsonl` | `infra/alert_history.py` | one JSON object per line | Append-only alert history. Replay source. |
| `data/alerts/<alert_id>/` | `infra/vehicle_artifacts.py` | directory | Per-alert frames: cropped + raw vision JSON, bbox data. |
| `data/matcher_telemetry.json` | `infra/matcher_telemetry.py` | single JSON snapshot | Periodic (300s) dump of matcher state. |
| `data/vehicles/known_vehicles.json` | hand-edited | JSON | Fleet registry. **Read-only at runtime.** |
| `data/known_vehicles.json` (if exists) | hand-edited | JSON | Legacy snapshot. May be a symlink target. |
| `logs/listener.log` | `infra/logging_setup.py` | plain text | Operational log. |
| `logs/cleanup.log` | `infra/cleanup.py` | plain text | Retention sweep log. |
| `data/last_person_seen.json`, `data/last_vision.json` | Phase 6A (disabled) | JSON | Caches for the disabled face-recognition path. |

### 4.2 Tracked (source of truth, committed)

| Path | Owner |
|---|---|
| `known_vehicles.json` (if at repo root, not under data/) | Fleet registry snapshot |
| `config/alert_overrides.json` | Per-camera alert baseline rules |
| `infra/vehicle_matcher.py` + `infra/matcher_spec.py` | Matcher code + `DEFAULT_SPEC` |
| `infra/vehicle_matcher_spec.yaml` (optional, falls back to `DEFAULT_SPEC`) | Override spec if you want spec outside the code |

### 4.3 Retention policy (`infra/cleanup.py`)

- Frames on disk: 7 days.
- `data/audit.jsonl`: 30 days.
- `data/alerts/alert_history.jsonl`: indefinite (forensics).
- `data/matcher_telemetry.json`: latest only (overwritten every 300s).

### 4.4 Replay procedure

To re-run vision offline against a past alert:

```bash
# 1. Find the alert_id in alert_history.jsonl
grep '"alert_id": "68a5fb1e-..."' data/alerts/alert_history.jsonl

# 2. Locate the alert directory
ls data/alerts/68a5fb1e-.../

# 3. Frames to feed back into Qwen
ls data/alerts/68a5fb1e-.../raw_frame_*.jpg

# 4. Existing vision result (for comparison)
cat data/alerts/68a5fb1e-.../raw_vision_crop_2.json
```

To re-run the matcher offline against a past `VisionResult`:

```python
from infra.vehicle_matcher import match_vehicle_scored
from vehicle_identifier.signature import extract_signature
from known_vehicles.store import load_known_vehicles

sig = extract_signature(vision_result)  # phase §11.17.A.1 wrap-unwrap handled inside
known = load_known_vehicles()
result = match_vehicle_scored(sig, known)
print(result)  # (kv, score, gap, all_breakdowns) or None
```

---

## 5. Configuration surface

| File / Var | Owner | Purpose |
|---|---|---|
| `camera-creds.env` (repo root) | Operator | RTSP URLs + per-camera usernames/passwords. **Never committed.** Loaded via `infra/camera_creds.py`. |
| `telegram-creds.env` (repo root) | Operator | Bot token + chat_id. **Never committed.** Loaded via `infra/telegram_creds.py`. |
| `llm-creds.env` (repo root) | Operator | Vision + text LLM endpoint URLs, optional Bearer tokens, model names. **Never committed.** Loaded via `infra/llm_config.py` (Phase.146). Defaults match the pre-6B.146 hard-coded values when absent. |
| `config/alert_overrides.json` | Operator (hand-edited) | Per-camera L1→L0 demotions for known noise patterns. See file's `_comment` for the four rule types. **Restart listener after editing.** |
| `config/motion_gate_thresholds.json` | Operator (hand-edited) | Per-camera gate thresholds. Restart not required (read at request time). |
| `data/vehicles/known_vehicles.json` | Operator (hand-edited) | Source of truth for the matcher. Hand-edit; schema in §3.3 below. |
| `infra/vehicle_matcher_spec.yaml` | Operator (optional) | Override matcher weights without editing code. Falls back to embedded `DEFAULT_SPEC` if absent. |
| `infra/paths.py` | Code | Single source of truth for all paths + env defaults. **Never hardcode `/Users/jill/...` elsewhere.** |
| Env flag `FARMSURV_PRODUCTION` | Code | `"1"` — production paths. Anything else — debug. |
| Env flag `MOTION_GATE_ENABLED` | Code | `"1"` (default in production) — YOLOv8n motion gate runs. `"0"` — disabled, alerts flow through legacy path. |
| Env flag `MOTION_GATE_V2` | Code | `"1"` — V2 gate fixes (6B.109). Required since 6B.115. |
| Env flag `PIPELINE_USES_GATE_CROPS` | Code | `"1"` — wire the gate's `GateVerdict.crop_paths` directly into the vision pipeline. |
| Env flag `MOTION_GATE_NIGHT_SUPPRESS_ENABLED` | Code | `"1"` (default) — night-time IR-illumination suppression heuristic. |
| Env flag `PHASE6A_ENABLED` | Code | `"true"` (default) — Phase 6A ArcFace attempt runs on person events. `"false"` — disabled; person pipeline still emits 6-image albums but skips face matching. |
| Env flag `DISABLED_CAMERA_EVENTS` | Code | Comma-separated `camera=event` pairs to suppress. E.g. `Outside Back Solar=person,animal` keeps vehicle alerts only. |
| Env flag `FARM_BUCKET_COOLDOWN_SECONDS` | Code | Per-camera+bucket alert cooldown override. Default tuned per camera in `infra/cooldown.py`. |
| Env flag `FARM_VISION_BLOCK_COOLDOWN_SECONDS` | Code | Block-duration cooldown for sustained vision failures. Default 300 s. |
| Env flag `VISION_LLM_URL` | Code | Vision LLM chat completions URL. Default `http://127.0.0.1:8093/v1/chat/completions`. Phase.146 (URL), Phase.158 (default → qwen3.6 unified endpoint). |
| Env flag `VISION_LLM_TOKEN` | Code | Bearer token for vision calls. Empty → no Authorization header. Phase.146. |
| Env flag `VISION_LLM_MODEL` | Code | Vision model name in payload. Default `qwen3.6`. Phase.146 (model), Phase.158 (qwen3-vl → qwen3.6). |
| Env flag `TEXT_LLM_URL` | Code | Text LLM chat completions URL. Default `http://127.0.0.1:8093/v1/chat/completions` (same as vision — unified server since Phase.158). |
| Env flag `TEXT_LLM_TOKEN` | Code | Bearer token for text calls. Phase.146. |
| Env flag `TEXT_LLM_MODEL` | Code | Text model name in payload. Default `qwen3.6` (unified server since Phase.158). |

### 5.5 Sidecar wiring — qwen3.6 as a selectable Hermes provider (Phase.158 + §11.82)

The unified Qwen3.6-35B-A3B llama-server (`com.llama.qwen36-35b`,
port 8093) is consumed by **two** independent systems. Both must
agree on URL + model name; they are NOT managed by the same config.

| Consumer | Config file | What it reads |
|----------|-------------|---------------|
| Surveillance listener | `llm-creds.env` (repo root) → `infra/llm_config.py` | `VISION_LLM_URL`, `VISION_LLM_MODEL`, `TEXT_LLM_URL`, `TEXT_LLM_MODEL`. Defaults: `http://127.0.0.1:8093/v1/chat/completions`, model `qwen3.6`. |
| `hermes-cli` runtime | `~/.hermes/config.yaml` `providers:` block | `qwen-local` provider entry (added §11.82). Default Hermes model stays `minimax/MiniMax-M3`; qwen-local is a selectable option in `/model`. |

To pick the local server in an active Hermes session: `/model
Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --provider qwen-local`. To revert:
`/model minimax/MiniMax-M2.7-highspeed`. The listener is unaffected
by `/model` switches — it has its own `infra/llm_config.py` defaults
and never reads `~/.hermes/config.yaml`.

The pickled-knowledge capture for the next person is in two skills:
`hermes-self-configuration` (Section 5.2 "Local OpenAI-compatible
endpoint workflow") and `llama-cpp-apple-silicon` (section "Wiring
a local llama-server into Hermes as a selectable provider"). Both
contain the verified config snippet, the canonical schema field
mapping, the slash-prefix/bare-ID pitfalls with reproduction
transcript, and the backup-before-edit pattern.

---

## 6. Operations runbook

### 6.1 "Camera looks dead"

1. `curl -sS http://localhost:8090/health` → check camera count.
2. `curl -sS 'http://localhost:8090/snapshot?camera=OFS' -o /tmp/test.jpg` → does the file exist and is it a valid JPEG? (`file /tmp/test.jpg` should report `JPEG image data, 2304x1296`.)
3. `tail -50 logs/listener.log | grep PersistentRTSPReader` → reader health + frame count.
4. `grep "Identity check failed\|reader_disconnected" logs/listener.log` → connection-level errors.
5. If reader is up but no events fire → check camera-side Motion/Smart sensitivity via Reolink web UI. Reference: 2026-08-17 OFS Motion=30 + Smart P/V/P=50/30/30 + Delay P/V/P=2/2/2.

### 6.2 "Matcher's gone haywire"

1. `tail -100 logs/listener.log | grep -E "matcher_top|Top 3 candidates"` → see if all scores are 0.00 (signature empty?) or all near max (threshold too low?).
2. If all 0.00 → check `extract_signature` flow. Phase.87.A.1 fixed the listener `vehicles[]` wrap-unwrap. If that regression came back, look at `vehicle_identifier/signature.py` first.
3. If all near max → `infra/vehicle_matcher_spec.yaml` may have been edited with relaxed thresholds. Roll back: `git log -- infra/vehicle_matcher_spec.yaml`, then `git checkout <good-commit> -- infra/vehicle_matcher_spec.yaml` and restart.
4. If single bad match → `data/vehicles/known_vehicles.json` enrollment issue. Check the matched `KnownVehicle`'s `colors_alt` and `vehicle_features` for over-broad matching.

### 6.3 "Telegram is silent"

1. `grep "QUIET_HOURS_SUPPRESSED" logs/listener.log | tail -5` → is the alert being suppressed by quiet hours?
2. `grep "cooldown_skip\|bucket_cooldown" logs/listener.log | tail -5` → is the cooldown bucket active?
3. `curl -sS http://localhost:8090/status | python3 -m json.tool | grep -i telegram` → outbound transport health.
4. If none of the above → `telegram-creds.env` (at repo root) may
   have a stale token. Re-issue via @BotFather, update the file,
   restart listener.
5. `grep "send_telegram\|telegram_api_error" logs/listener.log | tail -10` → httpx error details.

### 6.4 "Listener won't boot"

1. `launchctl list | grep surveillance` → is the plist loaded?
2. `lsof -iTCP:8090 -sTCP:LISTEN` → is port 8090 in use? If yes, `lsof -iTCP:8090 -sTCP:LISTEN -t | xargs kill -9` then reload.
3. `tail -30 logs/listener.log` → startup error. Most common: missing `camera-creds.env` → `infra/camera_creds.py` raises before the Flask app binds. A missing `llm-creds.env` is NOT fatal — `infra/llm_config.py` falls back to the localhost defaults (127.0.0.1:8093 with model `qwen3.6` since Phase.158; the unified Qwen3.6-35B-A3B server replaced the split Qwen3-VL-8B / Qwen3.5-9B ports 8080/8081/8082 in §11.81).
4. `./.venv/bin/python -c "import infra.paths; print(infra.paths.PROJECT_ROOT)"` → must print `<install-path>/ai_camera_monitor`. If not, venv is wrong.
5. `./.venv/bin/python -m pytest -q infra/tests/test_persistent_rtsp.py` → reader registry self-test (catches set/get identity bugs introduced during refactors).

---

## 7. Threading

Six threads. The Flask app thread handles ingress/egress only — all
business logic runs in the bounded executor or one of the named
background threads.

| Thread | Owner | Role |
|---|---|---|
| Flask app thread | `listener.py` | HTTP request handling. No business logic. |
| Classed webhook executor | `_ClassedWebhookExecutor` | N=4 workers, queue=64. Replaces `_BoundedWebhookExecutor` (6B.16). Runs the per-event pipeline. |
| Vision client | `infra/vision_client.py` | Single httpx POST per call. Pool removed in Phase.147. URL/token/model from `infra/llm_config`. |
| Matcher telemetry | `infra/matcher_telemetry.py` | Periodic (300 s) `data/matcher_telemetry.json` snapshot. |
| Cleanup | `infra/cleanup.py` | Hourly retention sweep. |

`infra/camera_queue.acquire_for_camera` provides per-camera fairness so
a flood on OFS can't starve OFG.
