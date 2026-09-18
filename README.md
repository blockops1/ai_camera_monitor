# farm-surveillance-v2

Most consumer IP-camera systems treat every motion alert as worth showing
you. They flood Telegram with branches moving in the wind, headlights
sweeping across a driveway, IR glare flickering off a fence. The result
is the same as no notification at all: you silence the channel.

**farm-surveillance-v2** is the opposite. It runs a small vision pipeline
on each motion event — a YOLO first-pass gate to filter obvious noise, a
vision-LLM (Qwen3-VL) to verify the subject and pull structured attributes
(color, body, make/model, breed), and a motion-aware crop so only the
**moving** subject ends up in the alert, not every vehicle visible in
frame. Per-camera confidence thresholds let you tune a windy porch camera
to demand 0.85 before an alert fires while a quiet driveway camera alerts
at 0.50.

The pipeline delivers up to three Telegram messages per event: a
wide-frame alert with the motion box, a cropped close-up of just the
moving subject, and (if recognized) a structured match result. The crop
is the part that earns its keep — it's the difference between "a dog
crossed the driveway" and "a small fluffy dog crossed the driveway, here
is just the dog."

Built for Reolink IP cameras; runs as a single Python daemon under
`launchd` on macOS. Phase PRDs in [`docs/`](docs/).

## What this is

A small Python service that:

1. Listens on `:8090/alert` for Reolink motion webhooks (IP-validated)
2. Captures 4 frames around the motion event from a continuous per-camera
   RTSP ring buffer (12-frame lossless PNG ring per camera)
3. Runs a YOLO gate to filter noise vs real subjects
4. Verifies subject class via vision-LLM (Qwen3-VL on `:8080`)
5. Asks vision-LLM for class-specific detail
6. Sends three Telegram notifications: alert, detail, optional match
7. Throttles per-`(camera, classification)` to avoid alert storms

Pipeline shape in [`docs/PLAN.md`](docs/PLAN.md). PRDs by phase in
[`docs/PHASE-V2-*.json`](docs/).

## What alerts look like

When a camera sends a motion webhook, the pipeline runs end-to-end and
delivers up to three Telegram notifications: an alert with the wide
frame and motion box, a cropped detail image of the moving subject,
and (if the subject is recognized) a structured match result.

Example: a dark gray Ford F-150 driving down the gravel driveway.

| 1. Alert + motion box (wide frame) | 2. Cropped subject |
|---|---|
| ![Telegram alert: wide frame with motion box](docs/images/example-alert-truck-wideframe.jpg) | ![Telegram alert: cropped F-150](docs/images/example-alert-truck-crop1.jpg) |
| Two-frame stack from the camera. Red box = where motion was detected. | Same event, cropped to just the moving vehicle. |

| 3. Closer crop (second angle) | 4. Recognized subject + attributes |
|---|---|
| ![Cropped F-150 close-up](docs/images/example-alert-truck-crop2.jpg) | ![Recognized subject attributes](docs/images/example-alert-truck-attributes.jpg) |
| Closer view of the same vehicle as it passes. | Vision-LLM structured output: color, body style, make, model. |

The crop is the part of the pipeline that earns its keep: when multiple
vehicles are visible in the wide frame (parked + moving), the crop
selector only sends the **moving** one to Telegram, not every vehicle
in frame. Same for animals — the pipeline crops to the animal, not the
whole scene.

Example: a small fluffy dog crossing the gravel.

| Wide frame + motion box | Cropped detail |
|---|---|
| ![Wide frame dog alert](docs/images/example-alert-dog-wideframe.jpg) | ![Cropped dog detail](docs/images/example-alert-dog-detail.jpg) |
| Same two-frame pattern; motion box over the dog. | Vision-LLM cropped to just the dog. |

Telegram format and alert wording are documented in
[`telegram_formatter/`](telegram_formatter/).

## Reolink camera setup

This daemon expects Reolink cameras to POST motion alerts to its
`/alert` webhook. Each camera must be wired up twice: once on the
camera's web UI (motion alerts enabled, webhook URL configured,
framerate set), and once in `camera-creds.env` so the daemon can
validate incoming requests and pull frames from the camera's RTSP
stream.

This section is a working overview. Model-specific quirks (RLC-510A
vs RLC-833A, firmware differences) live in operator playbooks — this
README covers what works on modern firmware across both.

### 1. Enable push notifications on the camera

Open the camera's web UI (`http://<camera-ip>/`, log in with admin
credentials). On Reolink models with firmware v3.0.0+:

- Click the **gear icon** (top-right). On some models, only a
  center-click on the gear opens the full settings panel — a click
  on the icon's edge may open a stripped-down view that hides the
  Push and webhook controls.
- Click **Surveillance → Push**.
- Toggle **Push Notifications** ON.

If your camera runs older firmware, this toggle may not appear in
the gear panel — use the Reolink mobile or desktop app to enable
push notifications per camera.

### 2. Configure the webhook URL

On the same Push page, scroll to **For Developer → Webhook**:

- Click **Add**.
- Enter the webhook URL: `http://<daemon-host>:8090/alert`
  (default daemon port is `8090`; override with `LISTEN_PORT`).
- Save.

The URL must be reachable from the camera's network. If the camera
is on `192.168.1.x` and the daemon is on a separate machine,
configure port forwarding or run the daemon on the camera's subnet.

### 3. Set framerate

Lower framerate = less RTSP bandwidth + lower motion-detection load.
Set the **main stream** to **6 fps** as a starting point
(bandwidth-friendly, plenty for vehicle/person detection).

The easiest path is the camera's web UI under
Settings → Stream → Encode (or via the CGI `SetEnc` endpoint on
supported firmware). Note: on RLC-510A v3.2.0+, only even fps values
are accepted (`[2, 4, 6, 8, 10, 12]`) — odd values return a
`param error`.

### 4. Wire the camera into camera-creds.env

The daemon rejects webhook requests from unknown camera IPs.
Add one stanza per camera to `camera-creds.env` (mode 600, never
committed). Template:

```bash
# Front door camera
FRONT_IP=192.168.1.50
FRONT_HTTP_USER=admin
FRONT_HTTP_PASS=<from-camera-web-ui>
FRONT_RTSP_USER=admin
FRONT_RTSP_PASS=<from-camera-web-ui>
FRONT_RTSP_URL=rtsp://${FRONT_RTSP_USER}:${FRONT_RTSP_PASS}@${FRONT_IP}:554/Preview_01_main
```

`camera-creds.env.example` at the repo root is the template. Copy it,
fill in real values, mode 600, never commit.

Verify each stanza is reachable: `curl -s -o /dev/null -w "%{http_code}\n"
-u <HTTP_USER>:<HTTP_PASS> http://<camera-ip>/cgi-bin/api.cgi?cmd=GetDevInfo`
should return `200`. If you get `401`, the HTTP user/pass in
`camera-creds.env` doesn't match the camera's actual credentials.

### 5. Restart and verify

After all four steps are wired, bounce the daemon (see
[Daemon management](#daemon-management) below) and trigger a test
motion event on the camera. The pipeline should deliver a Telegram
alert within 5-10 seconds. If nothing arrives, check
`logs/daemon.log` for connection errors against the camera IP.

## RTSP robustness

Each camera has its own RTSP reader with two layers of recovery:

| Mechanism | Cadence | What it does |
|---|---|---|
| Scheduled reconnect | every `FARMSV_RTSP_RECONNECT_SECONDS` (default 3600s) | proactive close + respawn of the RTSP stream |
| Reconnect attempt cap | after `FARMSV_RTSP_MAX_RETRIES` (default 10) consecutive failures | back off and let the scheduled reconnect take over |

Together these prevent two failure modes: silent decoder stalls
(triggered when no frames arrive for too long) and infinite reconnect
loops when a camera is genuinely offline.

## Diagnostics

- `GET /debug/rtsp` — per-reader ring size, decoded total, last-frame age,
  container-open, health flag, error count
- Unified log: `logs/daemon.log` (single stream, daemon writes here)

## Layout

```
infra/                   # shared modules: gate, frame_capture, cooldown, schemas
listener/                # webhook receiver + linear pipeline
vehicle_position/        # trajectory + crop extraction
telegram_formatter/      # Telegram stage formatters
tests/                   # pytest
docs/                    # PLAN.md, architecture diagrams
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
| `~/.env` (user home) | Telegram credentials + runtime flags (see `.env.example`) | `listener/daemon.py` |

`*.env.example` files at the repo root are the templates. Copy,
fill in real values, mode 600, never commit.

Other env vars honored at runtime:

| Var | Default | Purpose |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | webhook bind |
| `LISTEN_PORT` | `8090` | webhook bind |
| `VISION_LLM_URL` | `http://127.0.0.1:8080` | Qwen3-VL endpoint |
| `LOG_LEVEL` | `INFO` | log level |
| `FARMSV_RTSP_RECONNECT_SECONDS` | `3600` | scheduled reconnect cadence |
| `FARMSV_RTSP_MAX_RETRIES` | `10` | reconnect attempt cap before deferring |

## Daemon management

The daemon runs under launchd as `com.farm.surveillance.v2`. Plist:
`~/Library/LaunchAgents/com.farm.surveillance.v2.plist`.

```bash
# view status
launchctl list | grep com.farm.surveillance.v2
tail -f logs/daemon.log

# bounce
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.farm.surveillance.v2.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.farm.surveillance.v2.plist
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

The test suite covers:

- `/alert` route (IP validation, payload shape, motion gate hookup)
- Per-camera RTSP reader lifecycle (mock container, advance time)
- Decoder failure handling (always-raise mock decoder)
- Crop selector and trajectory extraction
- Telegram formatting stages

## License

This project is licensed under the MIT License — see [`LICENSE`](LICENSE).
