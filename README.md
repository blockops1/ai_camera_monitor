# ai_camera_monitor

A self-hosted property-surveillance pipeline that turns Reolink camera
webhooks into Telegram alerts. Designed to run on a single Apple Silicon
Mac (no GPU server, no cloud). All inference is local.

## What it does

1. A **Reolink camera** detects motion (or a person, or a vehicle) on its
   own and fires an HTTP webhook at the listener.
2. The **listener** accepts the webhook, peels 4 frames from a persistent
   RTSP connection's ring buffer.
3. A small on-device **YOLOv8n motion gate** filters out noise (headlights,
   shadows, IR flare, foliage) — roughly 99% of false positives are
   dropped here. The vision LLM is never called on noise.
4. If the gate passes, a **local Qwen3 vision LLM** is asked to identify
   the person, vehicle, or animal in the frame.
5. A **multi-camera matcher** decides whether the detected vehicle is in
   the known fleet (`known_vehicles.json`) — outputting either a named
   match with confidence, or "unknown" with the detected make/model/color.
6. The **Telegram formatter** writes a concise alert with the cropped
   vehicle, the confidence score, and a link to the alert history.

## Why this exists

Most off-the-shelf camera-alert systems are either cloud-dependent
(privacy + subscription cost), or per-camera-on-device (no cross-camera
correlation, no "is this the same truck that pulled in yesterday?").
This project sits in the middle: **local everything**, **cross-camera
correlation**, **tunable scoring**.

## Architecture overview

```
  Reolink cameras ──webhook──>  listener (Flask, port 8090)
                                      │
                                      ▼
                                motion gate (YOLOv8n)
                                      │ (filtered ~99% of noise)
                                      ▼
                              vision LLM (Qwen3 local)
                                      │
                                      ▼
                              matcher (stable attributes + face)
                                      │
                                      ▼
                              Telegram formatter
```

See `skills/` for the underlying Hermes Agent skills this project relies
on (alert routing, vision prompt design, image cropping, etc.) and
`docs/AGENT-MATCHER-TUNING-LESSONS.md` for the matcher-tuning field
guide that came out of building this.

## Hardware (one configuration that works)

- **Apple Silicon Mac** (M1/M2/M3/M4, 16 GB+ unified memory)
- **Reolink PoE cameras** on the LAN (RTSP + webhook support)
- **macOS** host (tested on macOS 26.x)

Inference is small enough to share the GPU with the developer's daily
workload — the vision LLM is quantized to Q4_K_XL (~20 GB) and the
motion-gate CNN is 12 MB.

## Software (the stack)

| Component | Version | Role |
|-----------|---------|------|
| Python | 3.12 | listener + scripts |
| llama.cpp | latest | local GGUF inference (Metal GPU) |
| Qwen3-VL | 30B-A3B Q4_K_XL | vision LLM |
| YOLOv8n | 12 MB | motion-gate CNN |
| Reolink firmware | varies | camera-side detection + webhook |
| Telegram Bot API | v6 | outbound alerts |

## Quick start (TL;DR)

```bash
# 1. Clone
git clone https://github.com/blockops1/ai_camera_monitor.git
cd ai_camera_monitor

# 2. Python venv
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. Download the YOLOv8n motion-gate model (~12 MB)
#    (URL in scripts/download_models.sh)

# 4. Configure cameras + Telegram
#    See "Reolink camera setup" and "Telegram setup" below.
#    Place creds files at the repo root.

# 5. Edit your known vehicles
cp known_vehicles.example.json known_vehicles.json
# fill in your fleet

# 6. Smoke-test
pytest tests/ infra/tests/ -q
curl http://localhost:8090/health
```

## Reolink camera setup

Your camera needs:

1. **Motion detection enabled** with a "person" / "vehicle" smart-detect
   option if your model supports it.
2. **Webhook** pointed at `http://<your-mac-ip>:8090/webhook` with the
   JSON payload format this listener expects.
3. **RTSP** reachable for the listener to peel frames from.

See the `skills/reolink-camera-config/` and `skills/reolink-new-firmware-automation/`
Hermes skills for camera-side configuration playbooks.

## Telegram setup

1. Create a bot via `@BotFather`.
2. Set the bot token in `telegram-creds.env` (gitignored).
3. Set the chat ID for the destination channel.
4. The listener sends alerts to that chat with cropped images and
   confidence scores.

## Known vehicles (the matcher)

The matcher compares each detected vehicle's stable attributes
(make, model, color, body type) against `known_vehicles.json` entries.
The score combines stable-attribute distance and a face-recognition
cross-check (if a person is visible near the vehicle).

Edit `known_vehicles.json` with your fleet — typical format:

```json
{
  "my-truck": {
    "label": "the operator's truck",
    "stable_attributes": {
      "make": "Ford",
      "model": "F-150",
      "color": "white",
      "body_type": "pickup"
    }
  }
}
```

## Tests

Run the public test subset:

```bash
pytest tests/ infra/tests/ -v
```

The full private test suite is not shipped (it contains operator
fixtures and integration tests against private infrastructure). The
shipped subset covers the core scoring, cooldown, motion, prompt-template,
camera-credentials, and config-format modules.

## License

MIT — see `LICENSE`.

## Contributing

Issues and PRs welcome. The `skills/` subtree contains the underlying
Hermes Agent skills; if you write a new one that this project uses,
add it under `skills/<your-skill>/` and update the import path in
the module that uses it.

## See also

- `docs/AGENT-MATCHER-TUNING-LESSONS.md` — the matcher-tuning field guide
- `skills/` — the Hermes Agent skill library this project is built on
- `CHANGELOG.md` — release history

---

**Status: 0.3.0** (full overwrite of 0.1.x; v0.1.x public history is
intentionally not preserved in this repository — see CHANGELOG).