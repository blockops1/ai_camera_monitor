# farm-surveillance-v2

Linear-pipeline webhook consumer for farm IP cameras. Receives
Reolink motion alerts, runs an 11-stage processing pipeline, fires
Telegram notifications on subject detection and match results.

> **Status:** v2 daemon live on port 8090; delivering real alerts.
> Current PRD ([PHASE-V2-017](docs/PHASE-V2-017-PRD-rtsp-robustness-and-delivery.json))
> complete as of 2026-09-08 — registry prefix key, telegram env loading,
> scheduled_reconnect_watchdog, max_reconnect_attempts cap.

## What this is

A small Python service that:

1. Listens on `:8090/alert` for Reolink motion webhooks (IP-validated)
2. Captures 4 frames around the motion event from a continuous per-camera
   RTSP ring buffer (180-frame lossless-ish JPEG ring per camera)
3. Runs a YOLO gate to filter noise vs real subjects
4. Verifies subject class via vision-LLM (Qwen3-VL on `:8080`)
5. Asks vision-LLM for class-specific detail
6. Sends three Telegram notifications: alert, detail, optional match
7. Throttles per-`(camera, classification)` to avoid alert storms

Pipeline shape in [`docs/PLAN.md`](docs/PLAN.md). PRDs by phase in
[`docs/PHASE-V2-*.json`](docs/).

## RTSP robustness

Each camera has its own `PersistentRTSPReader` (in
[`infra/frame_capture.py`](infra/frame_capture.py)) with two defense-in-depth
watchdogs (US-017d / US-017e):

| Mechanism | Cadence | Source |
|---|---|---|
| `scheduled_reconnect_watchdog` | every `FARMSV_RTSP_RECONNECT_SECONDS` (default 3600s) | proactive close+respawn |
| `max_reconnect_attempts` cap | after `FARMSV_RTSP_MAX_RETRIES` (default 10) consecutive failures | cap-and-defer to watchdog |

Plus the existing `CameraCaptureRegistry._reconnect_loop` for
cross-reader registry-level recoveries. Ported from v1 refactor `<V1_REPO_PATH>/infra/persistent_rtsp.py`.

## Diagnostics

- `GET /debug/rtsp` — per-reader ring size, decoded total, last-frame age,
  container-open, health flag, error count
- Unified log: `logs/daemon.log` (single stream, plist routes stdout here)
- `pytest tests/ -x --tb=short` — 122 tests passing

## Layout

```
infra/                   # shared modules: gate, frame_capture, cooldown, schemas
listener/                # webhook receiver + linear pipeline (live, port 8090)
vehicle_position/        # trajectory + crop extraction
telegram_formatter/      # Telegram stage formatters
tests/                   # pytest (122 tests)
docs/                    # PLAN.md, V2-LIFT-PLAN.md, PHASE-V2-*.json
camera-creds.env.example # template for camera credentials
telegram-creds.env.example # template for Telegram credentials
llm-creds.env.example    # template for vision-LLM endpoint
```

## Configuration

Env files are mode 600 and **never committed**. Daemon reads them at
boot from these locations:

| File | What | Loaded by |
|---|---|---|
| `camera-creds.env` (repo root) | per-camera IP / HTTP user+pass / RTSP user+pass+url | `infra/camera_creds.py` |
| `~/.env` (user home) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHAT_ID`, plus 26 other keys | `listener/daemon.py::_load_home_env` (added in US-017b) |

`*.env.example` files at the repo root are the templates. Copy,
fill in real values, mode 600, never commit.

Other env vars honored at runtime:

| Var | Default | Purpose |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | webhook bind |
| `LISTEN_PORT` | `8090` | webhook bind |
| `VISION_LLM_URL` | `http://127.0.0.1:8080` | Qwen3-VL endpoint |
| `LOG_LEVEL` | `INFO` | log level |
| `FARMSV_RTSP_RECONNECT_SECONDS` | `3600` | US-017d watchdog cadence |
| `FARMSV_RTSP_MAX_RETRIES` | `10` | US-017e cap-and-defer threshold |

## Daemon management

The v2 daemon runs under launchd as `com.farm.surveillance.v2`. Plist:
`<HOME_DIR>/Library/LaunchAgents/com.farm.surveillance.v2.plist`.

```bash
# view status
launchctl list | grep com.farm.surveillance.v2
tail -f logs/daemon.log

# bounce (requires explicit operator approval at the moment of bounce)
kill <PID>
launchctl load <HOME_DIR>/Library/LaunchAgents/com.farm.surveillance.v2.plist
```

`KeepAlive: true` in the plist means launchd respawns automatically on
exit. The plist does **not** carry Telegram credentials — those load
from `~/.env` at boot.

## Development

Requires Python 3.11. Setup:

```bash
./.venv/bin/python3.11 -m pip install -e .
./.venv/bin/python3.11 -m pytest tests/ -x --tb=short
```

The 122-test suite covers:

- `/alert` route (with IP validation, payload shape, motion gate hookup)
- Camera registry prefix-key fix (single reader per camera)
- `~/.env` loading at daemon boot
- `scheduled_reconnect_watchdog` lifecycle (mock container, advance time)
- `max_reconnect_attempts` cap-and-defer (always-raise mock decoder)

## PRDs

Each phase is a numbered PRD in `docs/PHASE-V2-*.json`. Current state:

| PRD | Phase | Status |
|---|---|---|
| PHASE-V2-CORE | pipeline buildout | done |
| PHASE-V2-014 | camera webhook activation | done |
| PHASE-V2-016 | diagnostic visibility + boot hardening | done |
| PHASE-V2-017 | rtsp robustness + telegram delivery | done |

## License

Operator-private for now. License TBD before public squash release.
