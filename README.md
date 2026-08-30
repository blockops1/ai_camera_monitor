# AI Camera Monitor

**A security system that does exactly what you want on rural property.**

It tells you who's moving, whether they're known or unknown, and
minimizes the local compute and AI services needed by stacking
multiple layers of action to stop false-positive alerts before they
ever reach a vision model.

A self-hosted property-surveillance pipeline that turns Reolink camera
webhooks into Telegram alerts. Local-first, MIT-licensed, no cloud
dependency.

```
Reolink webhook
      │
      ▼
listener (Flask :8090)
      │
      ├─▶ motion gate (YOLOv8n CNN)  ──  ~99% of false positives dropped here
      │
      ├─▶ vision LLM (local Qwen3-VL :8093)  ──  make/model/color + body style
      │
      ├─▶ matcher (data/vehicles/known_vehicles.json)  ──  known or unknown?
      │
      └─▶ text LLM (local :8081)  ──  human-readable summary
              │
              ▼
        Telegram alert (bot API)
```

## What it does, end to end

- A **Reolink camera** detects motion and fires an HTTP webhook at the listener.
- The **listener** (Flask on port 8090) peels frames from the camera's
  RTSP ring buffer and routes by camera + event type.
- The **motion gate** runs a small YOLOv8n CNN on each frame's motion
  crop. If there's no real object, the alert is dropped here — the
  vision LLM is never called.
- The **vision LLM** receives a 3-image payload (two streak crops plus
  the pairwise differential) and returns make, model, color, body
  style, and confidence.
- The **matcher** scores the signature against
  `data/vehicles/known_vehicles.json` and decides known vs unknown.
- The **text LLM** writes the alert body. The **notifier** sends the
  alert to Telegram.

## Install

Tested on macOS 14+ (Apple Silicon). Linux x86_64 should work in
principle; Windows is untested.

```bash
brew install python@3.14 ffmpeg
xcode-select --install

git clone https://github.com/blockops1/ai_camera_monitor.git
cd ai_camera_monitor
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

Two local LLM endpoints (OpenAI-compatible HTTP API):

| Endpoint | Default port | Model (suggested) |
|---|---|---|
| Vision LLM | `8093` | `qwen3-vl` (or any small local vision LLM) |
| Text LLM   | `8081` | `qwen3.5` (or any small local chat model) |

Copy `llm-creds.env.example` to `llm-creds.env` and fill in the URLs.
If you only have one model server, point both URLs at the same place —
the prompts are independently written.

Recommended: **run a small local vision model** (Qwen3-VL 4B, LLaVA 1.6
7B, or similar). This is where the cost dominates if you don't — a
cloud vision API will burn money fast on 24/7 motion traffic.

Camera credentials go in `camera-creds.env`. Never commit these files.

## Run

```bash
python -m listener.listener
```

## Compare to Frigate

Frigate is the obvious comparator if you've heard of either of us:

- **Frigate** runs motion detection + object classification on every
  frame, in-process, in a single Docker container, with a built-in
  web UI. Tightly integrated with Home Assistant. Mature.
- **ai_camera_monitor** assumes the camera already does motion
  classification (Reolink's `event=md`, `event=vehicle`, `event=person`
  webhooks), and adds a **matcher** on top: it decides whether the
  vehicle in the alert is "known" or "unknown" by scoring the vision
  signature against an enrolled store. It's a thin pipeline, not a
  NVR.

If you want one container that handles everything end to end and you
don't need vehicle/person re-identification, use Frigate. If you want
a separate Python service you can read top-to-bottom and extend with
your own matcher logic, this repo is for you.

## License

MIT. See [LICENSE](LICENSE).