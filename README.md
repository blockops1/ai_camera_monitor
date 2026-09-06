# farm-surveillance-v2

Linear-pipeline webhook consumer for farm IP cameras. Receives
Reolink motion alerts, runs an 11-stage processing pipeline, fires
Telegram notifications on subject detection and match results.

> **Status:** Under active development. Pipeline not yet shipping.
> This is a clean rebuild; the prior incarnation's per-class
> pipeline structure was deliberately not carried forward.

## What this is

A small Python service that:

1. Listens for camera webhook alerts (Reolink/Hikvision-compatible)
2. Captures 4 frames around the motion event from the affected camera
3. Runs a YOLO gate to filter noise vs real subjects
4. Verifies subject class via vision-8B (Qwen3-VL) — no class hint
   passed in
5. Asks vision-8B for class-specific detail
6. Sends three Telegram notifications: alert, detail, optional match
7. Throttles per-`(camera, classification)` to avoid alert storms

Pipeline shape in [`docs/PLAN.md`](docs/PLAN.md). Lifted module
inventory in [`docs/V2-LIFT-PLAN.md`](docs/V2-LIFT-PLAN.md).

## Layout

```
infra/                   # shared modules: gate, frame_diff, cooldown, schemas
vehicle_position/        # trajectory + crop extraction
listener/                # webhook receiver + linear pipeline (target)
telegram_formatter/      # three Telegram stage formatters (target)
tests/                   # pytest (target ~50-100 tests)
docs/                    # PLAN.md + V2-LIFT-PLAN.md
*.env.example            # env templates (camera/telegram/llm creds)
```

## Configuration

Three env files at the repo root, mode 600, never committed:

- `camera-creds.env` — 6 cameras × 6 keys (IP / HTTP user+pass / RTSP user+pass+url)
- `telegram-creds.env` — bot token + chat id
- `llm-creds.env` — vision-8B endpoint (used by VM1 + VM2); text-9B and 35B-MTP kept configured for parity

See `*.env.example` at the repo root for the template.

## LLaMA topology

This pipeline uses vision-8B only (the Qwen3-VL `:8080` server).
35B-MTP and text-9B servers are present on the host but unused.

## Development

Requires Python 3.14+, `pip install -r requirements.txt` once
`requirements.txt` is wired (post Stage 6). Today: working tree
imports cleanly under `cd ~/farm-surveillance-v2 && python -c "import
infra.gate"`.

## License

Operator-private for now. License TBD before public squash release.
