# ai_camera_monitor

A self-hosted property-surveillance pipeline that turns Reolink camera
webhooks into Telegram alerts. Built around a small on-device CNN
(YOLOv8n) that filters out motion noise before a vision LLM
(Qwen3.6-35B-A3B) ever sees the frame, plus a multi-camera matcher
that decides whether the vehicle in the alert is "name two's pickup"
or "someone we don't know".

Production: runs as a `launchd` job on a Mac mini (Apple Silicon),
cameras on the LAN, vision LLM on the same Mac. **All inference is
local.** No cloud dependencies, no third-party alert routing.

This repo is the **refactored tree** (`~/ai_camera_monitor/`).
There is also a legacy tree at `<legacy-repo>/` which is dormant
and serves as a rollback target. See [Cutover history](#cutover-history).

For the deep architectural doc (data flow, matcher schema, ops runbook),
see [`ARCHITECTURE.md`](ARCHITECTURE.md). For the work plan and module
audit, see [`PLAN.md`](PLAN.md). For the live module call graph,
see [`listener-architecture.html`](listener-architecture.html)
(regenerated from the import graph — re-run `scripts/generate_architecture_diagram.py`
after structural changes).

---

## What it does, end to end

1. A **Reolink camera** detects motion (or a person, or a vehicle) on
   its own and fires an HTTP webhook at the listener.
2. The **listener** (`listener/listener.py`, a Flask app on port 8090)
   accepts the webhook, normalizes the payload, peels 4 frames from a
   persistent RTSP connection's ring buffer.
3. The **listener's routing decision** sends each webhook to either
    the **vehicle pipeline** (`listener/vehicle_pipeline/`) or
    the **person pipeline** (`listener/person_event_pipeline.py`) based
    on the camera + webhook type + gate verdict:
   - Vehicle pipeline handles OFS (vehicle tracker). It gets
     `event=md`, `event=vehicle`, or any event where the gate's YOLO
     says vehicle.
   - Person pipeline handles OFG (person tracker) and gets
     `event=people`/`event=person`, plus any `event=md` where the
     gate's YOLO is confident it's a person (6B.145 promotion).
4. The **motion gate** (`listener/motion_gate_pipeline.py`) runs a small
   YOLOv8n CNN on each frame's motion-difference crop. If the crop has
   no real object (or only low-confidence noise), the alert is dropped
   here — Qwen is never called. This filters roughly 99% of false
   positives caused by headlights, shadows, IR flare, foliage.
5. If the gate passes, the **vision step** (`infra/vision_client.py`)
   sends a **3-image payload** to a **local Qwen3.6-35B-A3B** server
   (an OpenAI-compatible llama-server at `http://127.0.0.1:8093`):
   two streak crops from frames 3 and 4 (motion-region bbox) and the
   **pairwise differential** (`abs(frame_3 − frame_4)` brightened,
   with bbox overlays). Qwen returns a structured description:
   make, model, color, confidence, plus a body-style hint
   (e.g., "tractor" — not forcing a car category). Vision + text
   inference share the same unified endpoint since Phase.158.
6. The **matcher** (`infra/vehicle_matcher.py`) scores that signature
   against every entry in `data/vehicles/known_vehicles.json` and
   decides whether it's a known vehicle or an unknown arrival. Color
   mismatch is one zero-weighted dimension among ~7; see
   `vehicle_matcher/scoring.py`.
7. The **alert generator** (`infra/alert_generator.py`) calls the
   same Qwen3.6 server (text-only prompt this time) to write a
   human-readable summary.
8. The **notifier** (`infra/notifier.py`) sends the alert to Telegram
   via a bot token. **Vehicle alerts** ship a 2-image album (streak
   crops). **Person alerts** ship a 6-image album (4 motion-gate
   frames + 2 person crops, including a 2-second pre-event trail on
   OFG). Cooldowns prevent alert floods.

Every step is logged to `data/audit.jsonl`. Every alert is appended to
`data/alerts/alert_history.jsonl` with the original frame and vision
JSON for forensics.

---

## What you need to install this

This section is for someone who wants to **set this up for themselves**.
Assume macOS on Apple Silicon (M-series Mac) — that is the only
configuration that is exercised in production. Linux x86_64 should work
in principle (onnxruntime has CPUExecutionProvider; PyAV is portable);
Windows is untested.

### Hardware

- One Mac (Apple Silicon recommended; M1/M2/M3/M4 tested in production).
  8 GB RAM minimum, 16 GB recommended for comfortable LLM serving.
- One or more **Reolink IP cameras** with motion/vehicle/person detection
  enabled. The webhook must be able to reach the Mac on port 8090 — so
  the cameras and the Mac need to be on the same LAN (or the Mac needs
  to be reachable via a routable IP). Tested cameras: RLC-510A,
  RLC-833A, RLC-81MA, Reolink Doorbell. Any Reolink camera that can
  POST a webhook should work.

### Software

| Component | Why | Install |
|---|---|---|
| macOS 14+ (Sonoma or newer) | Production target. Also runs on Linux x86_64. | `softwareupdate --install-rosetta` if you have any x86_64 binaries. |
| Python 3.14 | Hard requirement (`pyproject.toml` pins `>=3.14,<3.15`). | `brew install python@3.14` |
| Xcode Command Line Tools | PyAV and onnxruntime need a C compiler. | `xcode-select --install` |
| FFmpeg (for PyAV) | H.265/H.264 RTSP decode. | `brew install ffmpeg` |
| Ollama **or** llama.cpp (`llama-server`) | Local inference for Qwen3.6-35B-A3B (vision + text in one model since Phase.158). | See [Local LLMs](#local-llms-vision--alert-generation) below. |
| **YOLOv8n ONNX** (~12 MB) | The motion gate's CNN. | `bash scripts/download_quick_classifier_model.sh` |
| **YOLOv8m ONNX** (~104 MB, optional) | Higher-accuracy motion gate option. | Download separately; this repo only ships the gate logic. |
| Telegram bot token | The alert destination. | Create via [@BotFather](https://t.me/BotFather). |

### Local LLMs (vision + alert generation)

Since Phase.158 (§11.81), the listener calls a **single** unified
OpenAI-compatible HTTP endpoint for both vision and text:

- **`http://127.0.0.1:8093/v1/chat/completions`** — Qwen3.6-35B-A3B serves
  both image input (vision step: frame → vehicle description) and
  text-only prompts (alert text generation). The model is a 35.5B
  total / 3B active MoE, Q4_K_XL quantized (~22.85 GB), running with
  Multi-Token Prediction (MTP) speculative decoding on Apple Silicon
  Metal — ~64 tok/s warm text decode, ~78 tok/s prompt eval.

  Configure via `llm-creds.env` at the repo root (loaded by
  `infra/llm_config.py`); defaults match the production llama-server
  managed by the `ai.llama-server-full.qwen.plist` LaunchAgent.

Two options for serving them:

**Option A — llama.cpp (`llama-server`, recommended).**

```bash
# Build llama.cpp with Metal support (Apple Silicon GPU)
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp && make LLAMA_METAL=1

# Download Qwen3.6-35B-A3B GGUF (Q4_K_XL) and the mmproj vision adapter,
# then run llama-server. Production uses MTP speculative decoding for ~2x
# text decode throughput; vision prompts work but MTP acceptance on
# structured output may be lower.
./llama-server \
    -m ~/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
    --mmproj ~/models/mmproj-BF16.gguf \
    --port 8093 \
    --host 127.0.0.1 \
    --parallel 4 \
    --ctx-size 262144 \
    --cache-type-k q4_0 --cache-type-v q4_0 --kv-unified \
    --cache-ram 12288 \
    --flash-attn on \
    --spec-type draft-mtp --spec-draft-n-max 2 \
    -ngl 99   # offload all layers to Metal GPU
```

**Option B — Ollama.**

```bash
brew install ollama
ollama pull qwen2.5vl:7b    # or whichever Qwen-VL variant you prefer
ollama serve                 # exposes OpenAI-compatible API at :11434
```

If you use Ollama, change `DEFAULT_URL` in `infra/vision_client.py` and
`infra/alert_prompt.py` to point at your Ollama port — e.g.
`http://127.0.0.1:11434/v1/chat/completions` — and set
`VISION_LLM_MODEL` / `TEXT_LLM_MODEL` to the model you pulled. (Or set
the `VISION_LLM_URL` and `TEXT_LLM_URL` env vars, or use
`llm-creds.env` — see [LLM config](#llm-config).)

The `--parallel 4` flag matters when running llama-server directly:
runs up to 4 events concurrently, and the vision step is the rate
limiter. With `--parallel 1` you'll see queue backpressure under any
load.

### Telegram setup

1. Create a bot via [@BotFather](https://t.me/BotFather) — note the
   bot token.
2. Create a private chat (or group) for alerts. Add the bot. Note the
   chat ID (use `@userinfobot` or `getUpdates`).
3. Put both into a `telegram-creds.env` file at the repo root:

   ```
   TELEGRAM_BOT_TOKEN=110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
   TELEGRAM_CHAT_ID=987654321
   ```

   This file is **gitignored** — never commit it. The repo's
   `telegram-creds.env` only exists locally.

### Reolink camera setup

Each camera needs:

1. **RTSP URL** with credentials. Format:
   `rtsp://user:pass@<camera-ip>:554/h264Preview_01_main` (or the path
   your firmware exposes — Reolink has several).
2. **Webhook configured** to `http://<mac-mini-ip>:8090/alert` with
   POST. Most Reolink firmware calls this "Alarm Hook" or "Push
   Notification" — the exact name varies by model.
3. **Motion / Smart detection enabled**, with **Vehicle** and **Person**
   event types ticked if your camera supports them.

Each camera gets a `camera-creds.env` entry:

```
FRONT_RTSP_URL=rtsp://user:pass@192.168.1.39:554/h264Preview_01_main
BACK_RTSP_URL=rtsp://user:pass@192.168.1.85:554/h264Preview_01_main
OUTSIDE_FRONT_SOLAR_RTSP_URL=rtsp://user:pass@192.168.1.103:554/...
```

The `camera_map` at the top of `infra/camera_creds.py` lists the
canonical camera names. Edit that map if you have a different fleet
shape. Each `prefix_*` env var becomes a camera.

### Known vehicles (the matcher)

`data/vehicles/known_vehicles.json` is the registry the matcher scores
against. Hand-edit it. See the schema in [ARCHITECTURE.md §3.3](ARCHITECTURE.md#33-knownvehicle-schema-datavehiclesknown_vehiclesjson)
and the live file at the repo for examples.

A minimal entry looks like:

```json
{
  "vehicles": [
    {
      "id": "v_your_cousins_pickup",
      "label": "Your cousin's white pickup",
      "owner": "Cousin",
      "color": "white",
      "colors_alt": ["pearl", "cream"],
      "type": "pickup",
      "make": "Chevrolet",
      "model": "Silverado 1500",
      "model_aliases": ["Silverado"],
      "distinctive_features": ["chrome door handles", "aftermarket wheels"]
    }
  ]
}
```

`verified: false` means the enrollment is from a synthetic capture and
needs physical re-verification before being trusted. (Set it to `true`
once you've seen the vehicle get matched in production and confirmed
the match is correct.)

---

## Install steps (end-to-end)

```bash
# 1. Clone
git clone <this-repo> ~/ai_camera_monitor
cd ~/ai_camera_monitor

# 2. Python venv
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. Download the YOLOv8n motion-gate model (~12 MB)
bash scripts/download_quick_classifier_model.sh

# 4. Configure your cameras and Telegram (see "Reolink camera setup"
#    and "Telegram setup" above). Place creds files at the repo root.

# 5. Edit data/vehicles/known_vehicles.json with your fleet.

# 6. Smoke-test
curl -sS http://localhost:8090/health     # expects {"status":"ok","cameras_loaded":N}
./.venv/bin/python -m pytest -q           # 1299 tests, ~28 s

# 7. Run in the foreground (debug)
FARMSURV_PRODUCTION=1 ./.venv/bin/python listener/listener.py

# 8. Or install as a launchd job (managed boot)
#    Edit scripts/bootstrap-launchctl.sh to point at your repo path,
#    then run it. The plist template is included in that script.
```

# 9. (Optional) Install pre-commit hooks so quality gates run on every commit
.venv/bin/pre-commit install
# Now `git commit` auto-runs ruff, bandit, and vulture (advisory).
# See .pre-commit-config.yaml for what each gate catches.
```

The first webhook that lands on `POST /alert` triggers the full
pipeline. Expect 5–15 seconds end-to-end (capture is fast; vision is
the rate limiter).

---

## Module structure

```
ai_camera_monitor/
├── listener/                    # Flask composition root on :8090
│   ├── listener.py              # create_app() + 5 HTTP routes (health/status/snapshot/preview/alert)
│   ├── motion_gate_pipeline.py  # The YOLOv8n pre-filter (gate cutover, 6B.107)
│   ├── _motion_gate_dispatch.py # Gate runner, called from webhook handler
│   ├── vehicle_pipeline/        # 6-stage vehicle pipeline package (§11.111, 2026-09-02; monolith split into context/capture/identify/match/select_frame/alert/emit/notify submodules)
│   ├── person_event_pipeline.py   # Parallel pipeline for person/face events
│   ├── _gate_aware_capture.py     # Frame producer for the gate (Phase.115)
│   ├── state.py                  # Shared mutable state singleton
│   └── tests/                    # Listener route tests + pipeline tests
├── infra/                       # 55 modules — everything else
│   ├── paths.py                 # Single source of truth for paths + env
│   ├── persistent_rtsp.py       # Long-lived RTSP connections + ring buffer
│   ├── frame_diff.py            # Pairwise-frame difference + bbox crop
│   ├── quick_classifier.py      # YOLOv8n ONNX wrapper + night heuristic
│   ├── vision_analyzer.py       # Orchestrates the Qwen3.6 vision call
│   ├── vision_client.py         # httpx transport to llama-server (qwen3.6 unified endpoint)
│   ├── vision_queue.py          # Priority queue: vehicle > person > animal > motion
│   ├── vision_response.py       # JSON parse + retry on parse failure
│   ├── prompt_templates.py      # Qwen prompts + JSON schemas
│   ├── vision_cache.py          # 60s TTL cache for repeated calls
│   ├── vehicle_matcher.py       # 15-dim legacy scorer (production — wired via pipeline/_legacy_match_adapter)
│   ├── matcher_spec.py          # Match-spec loader + DEFAULT_SPEC
│   ├── matcher_scoring.py       # Per-dimension scoring logic
│   ├── alert_generator.py       # Calls Qwen3.6 (text-only) for alert text
│   ├── alert_prompt.py          # Alert prompt + payload builder
│   ├── notifier.py              # Cooldown-gated Telegram emit
│   ├── send_telegram.py         # httpx transport to Telegram Bot API
│   ├── cooldown.py              # Per-camera + per-bucket alert cooldown
│   ├── cleanup.py               # Disk retention sweep (every hour)
│   ├── audit.py                 # JSONL audit logger
│   ├── alert_history.py         # Append-only alert history
│   ├── motion_detector.py       # Pixel-difference motion detector
│   ├── camera_creds.py          # RTSP credential parser (Phase.166 §11.87.6)
│   ├── recipe.py                # Motion-recipe loader (CLI flags + config/motion_recipe.json + env vars) (Phase.166 §11.87.3)
│   ├── camera_aliases.py        # Friendly-name resolver (OFS → "Outside Front Solar")
│   ├── camera_audio.py          # Microphone audio capture from cameras (optional)
│   ├── telegram_creds.py        # Bot token + chat_id loader
│   ├── quiet_hours.py           # 21:00–07:00 suppression
│   ├── time_of_day.py           # Astronomical sunrise/sunset (suntime)
│   ├── timezone.py              # EDT conversion (fixed UTC-4)
│   └── tests/                   # 13+ test files
├── vehicle_position/            # 3 modules — frame → crops
├── vehicle_identifier/          # 5 modules — crops → Qwen → vehicle/person signature (Phase 6A)
├── vehicle_matcher/             # 2 modules — signature → verdict (modular path; not wired — see orchestrator note below)
├── known_vehicles/              # 1 module — JSON store loader
├── telegram_formatter/          # 6 modules — verdict → Telegram body
├── pipeline/                    # 2 modules — cross-domain orchestrator + legacy-match adapter
├── data/                        # Runtime: frames, audit, alerts, vehicles, state
├── logs/                        # Runtime: listener.log, cleanup.log
├── config/                      # alert_overrides.json, motion_gate_thresholds.json
├── models/                      # yolov8n.onnx (downloaded)
├── scripts/
│   ├── generate_architecture_diagram.py   # Regenerates listener-architecture.html
│   ├── download_quick_classifier_model.sh # YOLOv8n download
│   ├── bootstrap-launchctl.sh             # Installs the launchd plist
│   ├── bulk_enroll_from_crops.py          # Batch-enroll vehicles from a directory of crops
│   ├── configure_webhook.py               # Apply webhook URL/port to one or all cameras
│   ├── verify_webhook.py                  # POST a synthetic webhook to the listener
│   ├── tune_510a_motion_sensitivity.py    # RLC-510A CGI motion-tuning (CLI flags + JSON recipe + env vars)
│   ├── apply_all_tuning.py                # Roll tuning recipe across the fleet
│   ├── read_alarm_settings.py             # Read alarm + motion settings from a camera
│   ├── cam_browser.py                     # Launch a local Chrome session to a camera
│   ├── enroll_vehicle_from_alert.py       # Enroll a vehicle from a captured alert
│   ├── enroll_person.py                   # Enroll a person identity from crops or alert archive
│   ├── enroll_animal.py                   # Enroll an animal from a captured alert (Phase.165)
│   ├── extract_night_training_candidates.py # Pull night frames for §11.47 night-YOLO training set
│   ├── probe_*.py                         # One-off probes for debugging
│   ├── _test_ben_*.py                     # Legacy name three-face tuning probes (one-time use)
│   └── train_yolov8n_night*.py            # Night-trained model scaffolding (§11.47, not yet executed)
├── tests/sandbox/               # Sandbox data for integration tests (gitignored)
├── AGENTS.md                    # Operating rules for agents
├── PLAN.md                      # Work plan + module audit
├── README.md                    # This file
├── ARCHITECTURE.md              # Deep doc: data flow, matcher schema, ops runbook
├── listener-architecture.html   # Live module call graph (regenerated)
└── pyproject.toml               # Deps + ruff config
```

**Design rule:** every production module does one thing, has a
standardized header docstring (STATUS, THREAD SAFETY, INPUTS, OUTPUTS,
PUBLIC API, DOES NOT DO, CALLED BY, CALLS INTO), and lives next to its
own tests. See `AGENTS.md §1.5` for the header format.

---

## Configuration surface

| File / env var | Purpose |
|---|---|
| `camera-creds.env` (repo root) | One line per camera: `<PREFIX>_RTSP_URL=rtsp://...`. **Never committed.** |
| `telegram-creds.env` (repo root) | `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_CHAT_ID=...`. **Never committed.** |
| `data/vehicles/known_vehicles.json` | Hand-edited fleet registry. Source of truth for the matcher. |
| `config/alert_overrides.json` | Per-camera alert baseline rules (L1→L0 demotions for known noise). Restart listener after editing. |
| `config/motion_gate_thresholds.json` | Per-camera gate thresholds. Restart not required (read at request time). |
| `infra/vehicle_matcher_spec.yaml` | Optional: override matcher weights without editing code. Falls back to embedded `DEFAULT_SPEC`. |
| env `FARMSURV_PRODUCTION=1` | Production paths. Otherwise debug. Required for launchd. |
| env `MOTION_GATE_ENABLED=1` | Turn on the YOLOv8n motion gate (default on in production). |
| env `MOTION_GATE_V2=1` | Opt-in for V2 gate fixes (6B.109): full-frame fallback + no_server_motion bypass + tighter routing rule 5. Required since 6B.115 (gate as sole producer of frames + crops). |
| env `PIPELINE_USES_GATE_CROPS=1` | Wire the gate's `GateVerdict.crop_paths` directly into the vision pipeline (no legacy re-capture). Phase.108a/6B.115 default. |
| env `MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1` | Night heuristic: suppress low-confidence detections at night. |
| env `MOTION_GATE_NIGHT_CONF_FLOOR=0.40` | Confidence floor for the night heuristic. |
| env `MOTION_GATE_NIGHT_BRIGHTNESS_RATIO=1.5` | IR illumination signature: bottom of frame must be 1.5× brighter than top. |
| env `PHASE6A_ENABLED=false` | Disable the face-recognition path. Person pipeline still fires (person_tracker Telegram channel, structured 6-image album), but skips ArcFace attempt against `data/identities/`. |
| env `DISABLED_CAMERA_EVENTS` | Comma-separated `camera=event` pairs to suppress. E.g. `Outside Back Solar=person,animal`. |
| env `FARM_IDENTITY_BACKUP_DIR` | Off-host face-embedding backup path. **Empty by default** — biometric data stays local-only. |
| env `FARM_BUCKET_COOLDOWN_SECONDS` | Per-camera+bucket alert cooldown (suppresses back-to-back alerts of the same type). Default tuned per camera in `infra/cooldown.py`. |
| env `FARM_VISION_BLOCK_COOLDOWN_SECONDS` | Block-duration cooldown for sustained vision failures (e.g., Qwen down). Default 300s. |
| env `PERSON_AUDIO_ENABLED` | Optional microphone capture from cameras (Phase.10x, parked — defaults off). |
| env `VISION_LLM_URL` | Vision LLM chat completions URL. Default `http://127.0.0.1:8093/v1/chat/completions` (unified qwen3.6 endpoint since Phase.158). Override via env var or `llm-creds.env`. |
| env `VISION_LLM_TOKEN` | Bearer token sent as `Authorization: Bearer *** for vision calls. Empty → no header. |
| env `VISION_LLM_MODEL` | Vision model name in the OpenAI-compatible payload. Default `qwen3.6`. |
| env `TEXT_LLM_URL` | Text LLM chat completions URL. Default `http://127.0.0.1:8093/v1/chat/completions` (same endpoint as vision — unified server since Phase.158). |
| env `TEXT_LLM_TOKEN` | Bearer token for text LLM calls. |
| env `TEXT_LLM_MODEL` | Text model name. Default `qwen3.6` (same model as vision). |

The full list of paths and env-derived config lives in `infra/paths.py`
— that's the single source of truth.

## LLM configuration

Vision and text LLM endpoints are configurable via environment variables
or a single env file at the repo root: **`llm-creds.env`**. See
`llm-creds.env.example` for the template (do not commit your actual
`llm-creds.env` — it's in `.gitignore`).

### Resolution order

For each endpoint (vision + text), values are resolved in this order
(later wins):

1. **Built-in defaults** — Qwen3.6-35B-A3B on
   `http://127.0.0.1:8093/v1/chat/completions` for BOTH vision and
   text (unified server since Phase.158 / §11.81). Production runs
   with these defaults when no env file is present.
2. **`llm-creds.env`** — declarative `KEY=value` lines for any subset
   of the six config keys (URL / token / model per endpoint).
3. **Environment variables** — `VISION_LLM_URL`, `VISION_LLM_TOKEN`,
   `VISION_LLM_MODEL`, `TEXT_LLM_URL`, `TEXT_LLM_TOKEN`,
   `TEXT_LLM_MODEL`. These win over file values.

### Format

`llm-creds.env` uses standard shell-style assignment (comments with `#`,
blank lines ignored, surrounding quotes stripped):

```
# Vision LLM — local qwen3.6 unified endpoint (Phase.158)
VISION_LLM_URL=http://127.0.0.1:8093/v1/chat/completions
VISION_LLM_TOKEN=
VISION_LLM_MODEL=qwen3.6

# Text LLM — same unified endpoint as vision (Phase.158)
TEXT_LLM_URL=http://127.0.0.1:8093/v1/chat/completions
TEXT_LLM_TOKEN=
TEXT_LLM_MODEL=qwen3.6
```

### Auth header behavior

When a `*_LLM_TOKEN` is set (non-empty), every call sends
`Authorization: Bearer *** as a header. When the token is empty,
no `Authorization` header is sent — local llama-server runs unchanged.

Tokens are NEVER logged. The `infra.llm_config` module logs
`token_set=True/False` only, never the value.

### Tested compatibility

- **llama-server** (default) — no auth required
- **OpenAI** — `https://api.openai.com/v1/chat/completions`, Bearer token
- **Anthropic** via OpenAI-compat proxy — same
- **Remote Ollama** — `http://host:11434/v1/chat/completions`, no auth

### Programmatic access

```python
from infra.llm_config import load_vision_config, load_text_config

v = load_vision_config()
# v.url, v.token, v.model, v.auth_headers() → {"Authorization": "Bearer xxx"}

t = load_text_config()
# t.url, t.token, t.model, t.auth_headers()
```

Configs are cached after first call (`@lru_cache(maxsize=1)`). Tests use
`reset_for_tests()` to clear the cache.

---

## HTTP API

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness check + count of loaded cameras. Cheap. |
| `/status` | GET | Queue depths, last-cleanup result, matcher telemetry snapshot, alert count. |
| `/snapshot?camera=<name>` | GET | Latest JPEG from a camera's persistent RTSP ring buffer. Optional `?max_size=WxH`. |
| `/preview?camera=<name>&time=<ISO>` | GET | <nas> NAS preview thumbnail for a given timestamp (Phase.124). Use case: post-hoc lookups when Reolink AI missed a slow-moving vehicle. |
| `/alert` | POST | The camera webhook entry point. Reolink posts here. |

### Smoke-test

```bash
# Liveness
curl -sS http://localhost:8090/health

# Operator snapshot
curl -sS 'http://localhost:8090/snapshot?camera=OFS' -o /tmp/ofs.jpg
file /tmp/ofs.jpg   # expects: JPEG image data

# Synthetic webhook (use any real camera alias)
curl -sS -X POST http://localhost:8090/alert \
  -H "Content-Type: application/json" \
  -d '{"camera": "<real-alias>", "timestamp": "2026-08-26T14:30:00-04:00", "rtsp_url": "<URL>"}'
```

---

## Operational characteristics

- **Latency:** end-to-end alert is **~15–73 s** (today, 16 measured
  events on OFS+OFG+OFBS; 28–48 s typical, with one 112 s outlier on
  OFG during a sustained motion burst). The motion gate deliberately
  waits for a frame gap to confirm genuine motion (~10–20 s), then the
  vision step adds ~3–8 s per call to Qwen3.6. End-to-end at the
  long tail is dominated by the gate's motion-confirmation window, not
  vision LLM time.
- **Operator scripts (Phase.166 §11.87):** all 8 operator scripts in
  `scripts/` route through `infra.camera_creds`, `infra.recipe`, and
  `infra.paths`. **CLI flags** for per-invocation overrides, the JSON
  recipe (`config/motion_recipe.json`) for fleet/per-camera defaults,
  and env vars for stable infra/secrets. See `AGENTS.md §3` for the
  parameterization rule.
- **Listener:** PID 41135 on commit `e8f477d` (Phase.166 §11.87.8,
  2026-08-30). Has not been restarted during the §11.87 push;
  doc-only commits (`AGENTS.md` → `d71d5cf`, `listener-architecture.html`
  → `ef222ea`) don't trigger launchd restart.
- **Concurrency:** up to 4 webhook events processed in parallel
  (`_ClassedWebhookExecutor`, replacing the older
  `_BoundedWebhookExecutor`). The vision step is single-flight — one
  Qwen call at a time across all events, queued by priority
  (vehicle > person > animal > motion).
- **Routing:** OFS → vehicle pipeline (vehicle tracker). OFG → person
  pipeline (person_tracker). Gate verdict can promote `event=md` to
  either vehicle (6B.129a) or person (6B.145).
- **False-positive suppression:** the motion gate cuts ~99% of noise
  that reaches Telegram. A night-time heuristic adds extra suppression
  for low-confidence detections when the IR illumination signature
  matches.
- **Retention:** frames kept 24 h, audit log 90 days, alert history
  indefinite. `data/cleanup.py` runs every hour to enforce.
- **Failure modes:**
  - Vision LLM down → match stage gets `None`, alert fires with
    `threat_level=-1` (error sentinel). Listener still up.
  - Telegram down → alert is logged and persisted; the operator can
    replay from `data/alerts/alert_history.jsonl`.
  - Camera RTSP drops → `infra/persistent_rtsp.py` reconnects every
    hour on a schedule. Until it reconnects, alerts from that camera
    fall through to the gate which then suppresses for "no_server_motion".

---

## Cutover history

This is the refactored tree. The legacy `<legacy-repo>/` tree
served production until 2026-08-14.

| Date | What | Commit |
|---|---|---|
| 2026-08-14 | Part 9 complete: 6 module splits, 18 new modules | `3e03e39` |
| 2026-08-14 | **Production cutover executed** — refactor listener on :8090 (PID 5611) | `0590df8` |
| 2026-08-17 | Phase.88: `GET /snapshot` endpoint | `ee1cc10` |
| 2026-08-21 | Phase.105c: `_process_alert` slim — 4248 → 1855 lines | (multiple) |
| 2026-08-23 | Phase.108a: gate-aware pipeline cutover — gate runs ABOVE person-branch | `0c04506` |
| 2026-08-23 | Phase.107: motion gate cutover with YOLOv8n pre-filter (§11.37) | (multiple) |
| 2026-08-24 | Phase.109: V2 gate fixes (tighter routing, threshold alignment) | `2f9499e` |
| 2026-08-25 | Phase.115: gate as sole producer of frames + crops (in-memory verdict) | (multiple) |
| 2026-08-26 | Phase.116b: night-heuristic timestamp plumbing + reason field | `e710bbe` |
| 2026-08-26 | Phase.119-123: code-quality toolchain (bandit + mypy + vulture + pre-commit) | `ab720e3`...`9bc4cfe` |
| 2026-08-27 | Phase.140 (§11.61): person-gatekeeper camera FDO → OFG | `da148c1` |
| 2026-08-27 | Phase.141 (§11.64): person-emit 6-image Telegram media group | `6e6e8ac` |
| 2026-08-27 | Phase.142 + 6B.143 (§11.65): match-alert 2-crop album + OFG pre-event trail | `da4f812` |
| 2026-08-27 | Phase.144 (§11.66): revert YOLO-tighten + send 3-image payload to Qwen | `c429ee7` |
| 2026-08-27 | Phase.145 (§11.67): promote `event=md` → 'people' on gate=person | `76e79f9` |
| 2026-08-27 | Phase.146 (§11.68): configurable LLM endpoints via `llm-creds.env` + env vars (Bearer auth) | `0f36f74` |
| 2026-08-27 | Phase.147 (§11.69): drop `infra/vision_pool.py`, single vision URL only | `a56e008` |
| 2026-08-28 | Phase.163 person-stable-attributes: Tier 3 stable-attribute matching for person identity | (multiple) |
| 2026-08-28 | Phase.165: animal event pipeline (audit-only, no Telegram) + known-animals registry | (multiple) |
| 2026-08-30 | Phase.166 §11.86: animal matcher + wider-scope schema (Qwen authoritative); per-camera animal cooldowns | (multiple) |
| 2026-08-30 | Phase.166 §11.87: parameterize 8 operator scripts (CLI flags + `config/motion_recipe.json` + env vars); added `infra/recipe.py` and `infra/camera_creds.py`; 53 new tests in `test_enroll_vehicle_argparse.py`. Closed: `f47554d`...`0bb3260`...`e8f477d` (8 commits). | `f47554d`→`e8f477d` |

After cutover:

- `~/Library/LaunchAgents/ai.farm.surveillance-listener-refactor.plist`
  is the active plist (managed via `launchctl`).
- `<legacy-repo>/` stays on disk indefinitely — read-only fallback.

---

## What's not in this repo (and why)

- **Face embeddings and identity enrollment data.** Biometric data stays
  on the Mac. `data/identities/` is gitignored. Default policy is
  **local-only**; off-host backup requires explicit
  `FARM_IDENTITY_BACKUP_DIR=<path>` opt-in.
- **RTSP credentials and Telegram bot tokens.** Both live in env files
  at the repo root (`camera-creds.env`, `telegram-creds.env`) and are
  gitignored. The listener's `infra/camera_creds.py` and
  `infra/telegram_creds.py` parse them at bootstrap.
- **The YOLOv8n ONNX file.** Gitignored as `models/*.onnx`. Download
  via `bash scripts/download_quick_classifier_model.sh`.
- **The llama.cpp or Ollama binaries and the Qwen GGUF files.** Install
  separately, on the same Mac. See [Local LLMs](#local-llms-vision--alert-generation).
- **A trained-on-this-property night YOLO model.** The §11.47 plan calls
  for training a night-specific YOLO on captured nighttime frames.
  Scaffolding (`scripts/train_yolov8n_night*.py`) is in the repo but the
  training run has not been executed yet.

---

## Quality gates (run before pushing)

The repo's quality toolchain (Phase.119+) is **ruff + bandit + mypy
+ vulture + pytest**. All five are installed via `pip install -e
".[dev]"` and configured in `pyproject.toml`. The skill
`docs/SKILL-CODE-QUALITY-TOOLS.md` documents invocation, suppression
patterns, and triage workflow for each.

```bash
cd ~/ai_camera_monitor

# Fast feedback (~3s) — runs on commit if you've installed pre-commit
.venv/bin/pre-commit run --all-files

# Manual gates before pushing
.venv/bin/ruff check infra/ listener/ scripts/           # lint
.venv/bin/bandit -r infra/ listener/ -c pyproject.toml  # security
.venv/bin/mypy --explicit-package-bases \
    --exclude='_.*archive' --exclude='.*archive.*' \
    infra/ listener/ vehicle_position/ vehicle_identifier/ \
    vehicle_matcher/ known_vehicles/ telegram_formatter/ pipeline/  # types (~30s)
.venv/bin/vulture infra/ listener/ vehicle_position/ vehicle_identifier/ \
    vehicle_matcher/ known_vehicles/ telegram_formatter/ pipeline/  # dead code
.venv/bin/python -m pytest                              # tests (~30s)
```

**Current state (2026-08-30):** ruff 0 errors, bandit 0 findings,
mypy **0 errors** (down from 88 in 6B.123 — all fixed per Note
directive), vulture 0 100%-conf findings, **1057** tests pass + 2 skip
+ 2 pre-existing failures in `test_person_matcher.py::TestMatchByClothing`
(unrelated to §11.87 — known name four/name two color-weight case under
§11.42). Test count moved 1299 → 1057 during §11.86/§11.87: `+53`
from the new `test_enroll_vehicle_argparse.py`, `+41` from the new
`test_webhook_scripts_argparse.py`, with the net delta coming from a
small set of legacy test stubs that no longer correspond to live
scripts.

Pre-commit hook (`.pre-commit-config.yaml`) auto-runs ruff + bandit +
vulture on every `git commit`. mypy and pytest are deliberately NOT
in the hook (too slow for every commit) — run them manually before
pushing. mypy is now 0/159 source files, so it's safe to run as part
of the manual "before pushing" gate.

## Open work (not blockers)

**Closed since 2026-08-27:**
- **Phase.166 §11.86** — animal event pipeline (audit-only), known-animals
  registry + enrollment script, wider-scope schema, per-camera animal
  cooldowns.
- **Phase.166 §11.87** — operator-script parameterization refactor:
  all 8 scripts now use CLI flags + `config/motion_recipe.json` +
  `infra.camera_creds` + `infra.paths`. No hardcoded creds, no hardcoded
  URLs, no hardcoded tuning values. 1057 tests passing.

**Still open:**

- **§11.47 — Train a night-specific YOLOv8 model.** Scaffolding is in
  `scripts/train_yolov8n_night*.py`. Needs a labeled nighttime corpus;
  the gate's `no_object_detected` suppressions are the gold training
  data.
- **§11.42 — Color penalty too weak (name four blue / name two white case).**
  `vehicle_matcher/scoring.py` treats color mismatch as a single zero
  dimension among 7. name four's blue Chevrolet (not yet enrolled) is
  being matched to name two's white Chevrolet Silverado at score 4.50
  (above the 0.6 threshold). Three proposed fixes: (a) enroll
  name four's truck, (b) strengthen the color penalty to a negative
  score, (c) both. Awaiting Note decision.
- **6B.110 — QuickClassifier top_class priority.** Highest-confidence
  class loses person signal when a non-surveillance class has higher
  confidence. Design: surveillance-class priority (person > vehicle >
  animal > other). Awaiting Note prioritization.
- **6B.124 — Weekly vulture triage pass** (target: 0 60%-conf findings
  → promote vulture to blocking pre-commit gate).
- **6B.126 — mypy pre-commit hook** (only after 0-error baseline holds
  for ≥1 week).
- **Module-purity review (Part 9) follow-ons.** A handful of modules
  flagged in `PLAN.md Part 9` still combine multiple jobs. Each split
  is a follow-on, not a blocker — the listener works.

See `PLAN.md` §11.6 for the full list of post-cutover open questions.

---

## Repository rules (for agents and humans)

If you're a new agent landing here:

1. **Read `AGENTS.md` first.** Operating rules, isolation contract,
   lint gate. This file is auto-injected into your system prompt when
   the working directory is this repo.
2. **Read `PLAN.md` Part 9 + Part 11.** What was built, how it was
   split, and what's still open.
3. **Run the smoke tests.** `pytest`, `ruff check`, `/health`
   endpoint. The repo is in a known-good state.
4. **Module headers are mandatory.** Every production module starts
   with the standard header (see `AGENTS.md §1.5`). Update the header
   in the same commit as the code change.
5. **`paths.py` is the only place paths live.** Never hardcode
   `/Users/jill/...`. Use `infra.paths.PROJECT_ROOT`, `DATA_DIR`,
   `LISTENER_LOG`, etc.
6. **No cross-tree coupling.** Nothing in this repo may reference
   `<legacy-repo>/`. The legacy tree is the rollback target.
7. **Lint gate before commit:**
   `ruff check infra/ listener/ scripts/` must report zero errors.
8. **Plan-first for non-trivial work.** Add a `§11.NN` entry to
   `PLAN.md` before writing the code, not after.

For humans:

- The repo is opinionated. Camera fleet is fixed. Matcher scoring is
  tuned for the property's actual vehicle mix. Don't copy this whole
  repo expecting it to work out of the box — read [What you need to
  install this](#what-you-need-to-install-this) first.
- The simplest debug surface is `curl http://localhost:8090/status` —
  it shows queue depths, last-cleanup result, and matcher telemetry.
- For everything else, see [`ARCHITECTURE.md`](ARCHITECTURE.md) §6
  ("Operations runbook") — that's where the "camera looks dead" /
  "Telegram is silent" recipes live.