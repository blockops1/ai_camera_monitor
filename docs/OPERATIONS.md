# Operations — Running ai-camera-monitor

This document covers installation, configuration, and day-to-day operation of
ai-camera-monitor. It does **not** describe internal tooling or operator
workflow; for the visual architecture see [`docs/ARCHITECTURE-V2-CURRENT.html`](docs/ARCHITECTURE-V2-CURRENT.html)
and for the data-flow diagram see [`docs/LOGIC-FLOW-DIAGRAM.html`](docs/LOGIC-FLOW-DIAGRAM.html).

---

## 1. What this is

A webhook-driven IP-camera motion-alert pipeline. Cameras push motion events to
a local listener; the listener captures short frame bursts, runs motion-gate
classification, and forwards high-confidence alerts to a Telegram chat with
annotated images. Inference runs locally on Apple Silicon — no cloud calls.

## 2. Repository layout

```
ai-camera-monitor/
├── README.md                  # project overview + quickstart
├── LICENSE                    # MIT
├── pyproject.toml             # package metadata
├── Makefile                   # common tasks (lint, test, install hooks)
├── .env.example               # env-var template (private values)
├── camera-creds.env.example   # camera credential template
├── telegram-creds.env.example # telegram bot credential template
├── llm-creds.env.example      # LLM service credential template
├── infra/                     # pipeline stages (motion gate, classifier, formatter, …)
├── listener/                  # Flask webhook server (the daemon)
├── telegram_formatter/        # Telegram message rendering + dispatch
├── vehicle_matcher/           # known-vehicle fuzzy matching
├── config/                    # tuning (motion thresholds, defaults)
├── scripts/                   # privacy/jpeg/resize/orphan guardrails
├── tests/                     # smoke tests + guardrail tests
└── docs/                      # architecture + pipeline spec + this file
```

## 3. Pipeline architecture

```
camera webhook ──▶ listener ──▶ frame_capture (RTSP burst)
                              ──▶ motion_gate (YOLO-aware threshold)
                              ──▶ quick_classifier (YOLO vehicle/person/animal)
                              ──▶ vision_llm (Qwen via local llama-server)
                              ──▶ vehicle_matcher (known-vehicle DB lookup)
                              ──▶ telegram_formatter (caption + photos)
                              ──▶ Telegram
```

See [`docs/PIPELINE-SPEC-2026-09-10.md`](docs/PIPELINE-SPEC-2026-09-10.md) for
stage-by-stage detail and [`docs/ARCHITECTURE-V2-CURRENT.html`](docs/ARCHITECTURE-V2-CURRENT.html)
for the visual diagram.

## 4. Install

```bash
git clone https://github.com/<your-username>/ai-camera-monitor.git
cd ai-camera-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

A YOLO model file (e.g. `yolov8n.onnx`) is required at runtime. The model is
**not** bundled with the repo (see `.gitignore`). Download the model of your
choice and place it where `infra/quick_classifier.py` expects it (typically
`models/yolov8n.onnx`).

## 5. Configure

1. Copy every `*.env.example` to its non-`example` counterpart and fill in:
   - `.env` — runtime config (listen port, paths, log level)
   - `camera-creds.env` — RTSP URLs + per-camera credentials
   - `telegram-creds.env` — Telegram bot token + target chat ID
   - `llm-creds.env` — local LLM endpoint
2. Edit `config/motion_gate_thresholds.json` for per-camera sensitivity tuning
   (uses generic `CAMERA_A` / `CAMERA_B` / `CAMERA_C` placeholders — map them
   to your own camera labels).
3. Edit `data/vehicles/known_vehicles.example.json` (copy to
   `data/vehicles/known_vehicles.json`) to add vehicles you want the matcher
   to recognize.

**Never commit real `.env` files or your real `known_vehicles.json`.** They are
gitignored.

## 6. Run

Foreground (development):

```bash
.venv/bin/python -m listener.daemon
```

Background (production) via launchd:

```bash
# Copy a plist template to your launchd agents directory and adjust paths,
# then enable it:
launchctl load <HOME_DIR>/Library/LaunchAgents/<your-plist-label>.plist
```

The listener exposes a single webhook endpoint, `POST /alert`. Configure your
cameras (or your NVR) to push motion events to that endpoint with the
expected JSON payload.

## 7. Verify

```bash
# Smoke test the listener
curl -X POST http://127.0.0.1:<port>/alert \
  -H 'Content-Type: application/json' \
  -d '{"<camera>": "<event>"}'

# Run the guardrail suite (privacy + jpeg + resize + orphans)
make check-all

# Run the test suite
pytest tests/ -v
```

## 8. Privacy

See [`docs/PRIVACY.md`](docs/PRIVACY.md) for the project's privacy convention.
Key points:

- **Camera IPs, RTSP credentials, bot tokens, operator PII** → env files (gitignored).
- **Tuning values, threshold defaults, generic camera labels** → committed configs.
- **No face embeddings or biometric identifiers** are committed; identity data
  lives only on the operator's machine.

## 9. Customisation

Common operator customisations:

- **Add a camera**: append a `CAMERA_X` block to `camera-creds.env` and a
  matching tuning block to `config/motion_gate_thresholds.json`.
- **Change alert delivery target**: edit `TELEGRAM_HOME_CHAT_ID` in `.env`.
- **Tune motion sensitivity**: edit `motion_gate_thresholds.json` per camera.
- **Switch LLM backend**: edit `VISION_LLM_URL` in `.env` and the model name in
  `infra/quick_classifier.py`'s prompt templates.

## 10. License

MIT — see [`LICENSE`](LICENSE).
