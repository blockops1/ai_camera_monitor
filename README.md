# ai-camera-monitor

A self-hosted property-surveillance pipeline that turns IP-camera motion
webhooks into Telegram alerts with on-device classification.

Runs as a single Python service. Listens for camera webhooks on
`:8090/alert`, captures frames from a per-camera RTSP ring buffer, runs a
small YOLOv8n gate to filter motion noise, classifies real subjects with
a local vision-LLM (Qwen3-VL or equivalent), matches vehicles against a
known-vehicles database, and delivers three Telegram messages per event
(TG#1 alert, TG#2 detail, TG#3 match result).

All inference is local. No cloud dependencies, no third-party alert
routing. Camera IPs, RTSP credentials, bot tokens, and chat IDs are
loaded from gitignored env files at runtime — see [Privacy](#privacy).

For the deep architectural doc (data flow, schemas, ops runbook), see
[`docs/PIPELINE-SPEC.md`](docs/PIPELINE-SPEC.md). For the privacy
convention and what counts as private data, see
[`docs/PRIVACY.md`](docs/PRIVACY.md).

---

## What it does, end to end

1. **Webhook arrives** at `POST /alert` (port 8090). The source IP must
   match a configured camera.
2. **Four frames** are captured from the camera's persistent RTSP ring
   buffer (default 180-frame lossless JPEG ring per camera) and saved
   as `frame_001.png` through `frame_004.png` under
   `data/frames/<camera_id>/<alert_id>/`.
3. **One-time image manipulation** produces `crop_a.png`, `crop_b.png`,
   and `pairwise_diff.png` from those four frames. **No other image
   manipulation happens for this webhook.** The crops and the
   differential composite are the canonical artifacts for downstream
   stages.
4. **YOLO gate** classifies `crop_a` / `crop_b` and returns one of
   `{vehicle, person, animal}`. If the gate suppresses, the alert is
   dropped here — the vision-LLM is never called. This filters the
   vast majority of false positives (headlights, shadows, IR flare,
   foliage).
5. **Cooldown check**: if `(camera_id, classification)` is within the
   configured cooldown window, the alert is dropped silently.
6. **VM1** (vision-model pass 1) verifies the class is real and not a
   gate artifact.
7. **TG#1 fires**: caption = camera label + detected class + confidence
   + per-frame bbox positions. Attachments: one full frame + the
   pairwise differential composite.
8. **VM2** (vision-model pass 2) is called with a mode-specific prompt
   for class-specific detail (vehicle → make/model/color/distinctive
   features; person → clothing/height/identity markers;
   animal → species/color/markings).
9. **TG#2 fires**: caption = camera label + mode + class confirmation
   + the mode-specific fields. Attachments: `crop_a.png` + `crop_b.png`.
10. **Match** against `data/vehicles/known_vehicles.json` (or the
    equivalent person / animal database). Returns either an identity
    handle or `no_match`.
11. **TG#3 fires**: caption = camera label + match result + identity
    handle if matched. Attachments: none.
12. **Cooldown counter starts** only after TG#3 successfully
    dispatches. (The cooldown is post-gate and post-delivery — never
    pre-gate.)

Every step logs to `logs/daemon.log`. Every alert is persisted under
`data/frames/<camera_id>/<alert_id>/` with the vision-LLM JSON for
forensics.

---

## Requirements

- macOS or Linux, Python 3.11+
- One or more Reolink (or RTSP-speaking) IP cameras on the LAN
- A Telegram bot token + chat ID for delivery
- A local vision-LLM endpoint (Qwen3-VL via llama.cpp, vLLM, or any
  OpenAI-compatible server). Any 8B+ vision model works.
- For the YOLO gate, an ONNX export of YOLOv8n (download instructions
  in [Installation](#installation))

---

## Installation

```bash
git clone https://github.com/<your-account>/ai-camera-monitor.git
cd ai-camera-monitor
python3.11 -m venv .venv
./.venv/bin/python -m pip install -e .

# Generate the YOLO ONNX model (one-time, ~13 MB)
./.venv/bin/python scripts/export_yolo_dynamic.py --output models/yolov8n.onnx
```

Then create your credential files from the templates:

```bash
# Telegram bot token + home chat ID
cp telegram-creds.env.example telegram-creds.env
chmod 600 telegram-creds.env
$EDITOR telegram-creds.env

# Camera IPs / RTSP credentials (one stanza per camera)
cp camera-creds.env.example camera-creds.env
chmod 600 camera-creds.env
$EDITOR camera-creds.env

# Vision-LLM endpoint
cp llm-creds.env.example llm-creds.env
chmod 600 llm-creds.env
$EDITOR llm-creds.env

# Vehicle matcher database (start with the demo entry, then enroll yours)
cp data/vehicles/known_vehicles.example.json data/vehicles/known_vehicles.json
$EDITOR data/vehicles/known_vehicles.json
```

Configure your cameras in `config/motion_gate_thresholds.json`. The
keys must match the camera IDs you used in `camera-creds.env`.

---

## Running

### As a foreground process (development)

```bash
./.venv/bin/python -m listener.daemon
```

### As a launchd-managed service (macOS, recommended)

A launchd plist template is included in this README's source (the
authoritative install is at `~/Library/LaunchAgents/com.farm.surveillance.v2.plist`
on the author's box; copy and adapt the path for your machine). The plist:

- runs the daemon as the operator user
- sets `KeepAlive: true` so launchd respawns on exit
- routes stdout to `logs/daemon.log`
- does **not** carry Telegram credentials — those load from the
  env file at boot

Bouncing the daemon (requires explicit approval at the moment):

```bash
kill <pid>   # launchd respawns automatically; or:
launchctl unload ~/Library/LaunchAgents/com.farm.surveillance.v2.plist
launchctl load   ~/Library/LaunchAgents/com.farm.surveillance.v2.plist
```

---

## Layout

```
infra/                 # shared modules: gate, frame capture, cooldown, prompts
  frame_capture.py     # persistent RTSP reader + per-camera ring buffer
  frame_diff.py        # motion-difference crops + pairwise composite
  gate.py              # YOLOv8n classifier -> {vehicle, person, animal, suppress}
  quick_classifier.py  # fast gate (heuristic + YOLO)
  vision_analyzer.py   # VM1 + VM2 client against local OpenAI-compatible endpoint
  *_prompt.py          # class-specific prompts for VM2 (vehicle, person, animal)
  alert_artifacts.py   # canonical PNG path resolver
  camera_creds.py      # loads camera-creds.env at boot
  paths.py             # data/ + logs/ + cache/ path resolution
  pipeline_cooldown.py # per-(camera, classification) cooldown windows
  cleanup.py           # retention sweep for data/frames/
  tg_upload_cleanup.py # retention sweep for data/tg_uploads/

listener/              # webhook receiver + linear pipeline
  daemon.py            # Flask app on port 8090, POST /alert + GET /debug/rtsp
  pipeline.py          # 11-stage orchestrator (one method per stage)

telegram_formatter/    # Telegram delivery (TG#1, TG#2, TG#3)
  dispatcher.py        # entrypoint; selects which formatter to call
  alert.py             # TG#1: class + bbox positions
  detail.py            # TG#2: mode-specific detail
  match_alert.py       # TG#3: match result
  codec.py             # PNG -> wire-format JPEG q88 max-dim 1920

vehicle_matcher/       # signature scoring against known_vehicles.json
  match.py             # multi-pass scoring: color+type -> make+model -> features

config/                # static configuration
  motion_gate_thresholds.json  # per-camera gate thresholds

scripts/               # project guardrails + one-shot tooling
  check_no_image_resize.py     # CI: detect forbidden resize/recompress
  check_no_jpeg.py             # CI: detect forbidden JPEG output (except cache)
  check_no_private_data.py     # CI: PII scanner (see docs/PRIVACY.md)
  check_no_orphan_modules.py   # CI: detect unreachable modules
  export_yolo_dynamic.py       # one-time YOLO download + ONNX export

tests/                 # pytest smoke suite
data/                  # runtime artifacts (gitignored in production)
  frames/<camera>/<alert>/     # canonical PNG artifacts per webhook
  tg_uploads/<date>/<alert>/   # wire-format JPEGs cached for re-send
  vehicles/                   # known-vehicles DB (gitignored in production)
    known_vehicles.example.json # public schema example
docs/                  # consumer-facing docs
  PIPELINE-SPEC.md     # canonical 11-stage order of operations
  PRIVACY.md           # what counts as private data + scanner rule
  ARCHITECTURE-V2-CURRENT.html # live architecture diagram
  LOGIC-FLOW-DIAGRAM.html     # live logic-flow diagram
  RECREATION.md        # how to bootstrap this repo from scratch
camera-creds.env.example  # template for camera credentials
telegram-creds.env.example # template for Telegram bot token
llm-creds.env.example     # template for vision-LLM endpoint
data/vehicles/known_vehicles.example.json # schema example
```

---

## RTSP robustness

Each camera has its own `PersistentRTSPReader` (in
`infra/frame_capture.py`) with two defense-in-depth watchdogs:

| Mechanism | Cadence | Source |
|---|---|---|
| `scheduled_reconnect_watchdog` | every `FARMSV_RTSP_RECONNECT_SECONDS` (default 3600s) | proactive close + respawn |
| `max_reconnect_attempts` cap | after `FARMSV_RTSP_MAX_RETRIES` (default 10) consecutive failures | cap-and-defer to watchdog |

Plus the registry-level `_reconnect_loop` for cross-reader recoveries.

---

## Diagnostics

- `GET /debug/rtsp` — per-reader ring size, decoded total, last-frame
  age, container-open status, health flag, error count
- Unified log: `logs/daemon.log`
- Tests: `./.venv/bin/python -m pytest tests/ -x --tb=short`

---

## Privacy

This repo carries **no production-identity data**. All secrets,
identifiers, and personally-identifying strings are stored outside
version control or use neutral placeholders. See
[`docs/PRIVACY.md`](docs/PRIVACY.md) for the full convention:

- **Camera / farm IPs** live in `camera-creds.env` (gitignored). The
  committed example file uses `CAMERA_A`, `CAMERA_B`, `CAMERA_C` as
  placeholders.
- **Telegram chat ID** lives in `telegram-creds.env` (gitignored).
- **Helper / operator handle strings** live in
  `data/private_identifiers.json` (gitignored) at the operator's
  deployment site.
- **Known vehicles** live in `data/vehicles/known_vehicles.json`
  (gitignored). The committed file is `known_vehicles.example.json`
  with one demo entry.

A scanner (`scripts/check_no_private_data.py`) checks tracked files
against three pattern categories before each commit/push:

```bash
python scripts/check_no_private_data.py
# OK: no private data leaks detected.
```

Make it a pre-push hook:

```bash
make check-privacy
```

---

## License

MIT. See `LICENSE`.
