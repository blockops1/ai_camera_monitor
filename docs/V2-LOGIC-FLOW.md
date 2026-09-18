# v2 Logic Flow — Textual

Source: `<PROJECT_ROOT>/` @ commit `4daabf3` (all 8 US-014 stories committed).
Last verified by reading source: 2026-09-08.

---

## TL;DR

A Reolink camera POSTs `/alert` on `0.0.0.0:8090` → Flask daemon normalizes the payload
→ validates the source IP against `camera-creds.env` → pulls 4 recent frames from a
persistent RTSP reader → runs an 11-stage pipeline (cooldown → YOLO gate → VM1 → VM2 →
vehicle match) → formats 1-3 Telegram messages → sends to home chat `<TELEGRAM_CHAT_ID>`.

---

## A. HTTP ENTRY — `listener/daemon.py` (Flask, port 8090)

Two routes, both POST only:

| Route       | Purpose                  | Handler chain                                                |
|-------------|--------------------------|--------------------------------------------------------------|
| `/alert`    | PRIMARY (Reolink POST)   | `daemon.alert()` → normalize → IP validate → frames → pipeline |

launchd plist: `com.farm.surveillance.v2.plist`, `KeepAlive=true`, `RunAtLoad=true`.
Currently PID 87987. Logs → `<PROJECT_ROOT>/logs/daemon.log`.

---

## B. `/alert` HANDLER (the active path post-US-014f)


POST /alert (Flask)
│
├─ source_ip = request.headers["X-Forwarded-For"].split(",")[0] || request.remote_addr
│
├─ payload = request.get_json(silent=True)            ← silent=True → None on bad JSON
│   if payload is None → return 400 {"reason": "invalid json"}
│
├─ alert_dict = normalize_reolink(payload, source_ip)  ← Reolink nested shape
│   if None:
│       return 400 {"reason": "unrecognized payload shape"}
│
├─ camera_id = alert_dict.camera_id        ← "Front Door Outside" (friendly name)
│  camera_label = alert_dict.camera_label
│
├─ if NOT validate_source_ip(camera_id, source_ip):
│       # Anti-spoof: cameras can only POST from their registered IP
│       scan get_all_cameras() for matching (name==camera_label AND ip==source_ip)
│       if match:
│           _learned_camera_map.learn(label, matched_prefix)
│           camera_id = matched_prefix            ← e.g. "FRONT"
│       else:
│           return 403 {"reason": "IP validation failed"}
│
├─ alert_dict["frames"] = get_recent_frames(
│       camera_id, n=4, offset_seconds=6
│   )                                             ← /tmp/frame_001.jpg..frame_004.jpg
│
└─ result = pipeline.run(alert_dict) → return 200 JSON


The learned-camera map (`_LearnedCameraMap(maxlen=32)`) caches the (label→prefix) match
so future calls with the same friendly name skip the full scan.

---

## C. NORMALIZERS — `listener/daemon.py`

### `normalize_reolink(payload, source_ip)` → `dict | None`

**Input** (Reolink default alert payload):
json
{
  "type": "motion",
  "alarm": {
    "alarmTime": "...",
    "channelName": "Front Door Outside",
    "device":      "Front Door Outside",
    "name":        "...",
    "time":        "2026-09-07T20:00:00Z",
    "type":        "person|vehicle|animal|motion",
    "..."
  }
}


**Output**:
json
{
  "id": "<uuid4>",
  "camera_id":     "Front Door Outside",
  "camera_label":  "Front Door Outside",
  "classification":"person",
  "frames": [],
  "timestamp": "2026-09-07T20:00:00Z"
}


---


## D. IP VALIDATION — `infra/camera_creds.py`

- Parsed ONCE at module import (cached in `_all_cameras`).
- Reads `camera-creds.env` from `infra/paths.CAMERA_CREDS_FILE`
  (or `$FARM_CAMERA_CREDS_FILE` override).
- 6 cameras registered: FRONT, BACK, OUTSIDE_FRONT_GARAGE, OUTSIDE_FRONT_POWER,
  OUTSIDE_FRONT_SOLAR, OUTSIDE_BACK_SOLAR.
- Each row: `{name, ip, user, pass, rtsp_url, http_user, http_pass}`.
- `validate_source_ip(camera_id, request_ip)` → True iff
  `get_camera(camera_id).ip == request_ip`. Strict equality, no CIDR.

---

## E. FRAME PULL — `infra/frame_capture.py`


get_recent_frames(camera_id, n=4, offset_seconds=6) -> [path1, path2, path3, path4]
│
├─ reader = CameraCaptureRegistry.get(camera_id)
│     → singleton, lazy-boots a PersistentRTSPReader if not present
│     → looks up rtsp_url from infra.camera_creds.get_camera()
│
├─ if not reader.is_healthy() → return []   ← warmup window guard
│
├─ reader.get_recent_frames(n=4, output_dir=FRAMES_DIR/<cam>)  
│     → grabs last-4 PIL frames from ring deque, saves to JPEGs,
│        cleans old frame_*.jpg first
│
└─ filter by mtime:  keep only frames whose file mtime ≥ now - offset_seconds=6


### `PersistentRTSPReader` lifecycle
- One thread per camera, daemon=True.
- PyAV over RTSP/TCP (`rtsp_transport: tcp`, `fflags: +genpts`, buffer 20MB, timeout 10s).
- 12-frame rolling buffer (`deque(maxlen=12)`).
- Reconnect loop: every 5s, scan readers; if `uptime > 10s` and
  `is_healthy(stale_seconds=5.0)` is False → stop, re-instantiate, restart.
- Exponential backoff for decode failures: 1s → 2s → 4s … capped at 30s.
- `frames_decoded_total`, `reconnects_total` counters exposed for ops visibility.

---

## F. PIPELINE — `listener/pipeline.py` (single-threaded, 11 stages)


pipeline.run(alert) -> dict
│
├─ [Stage 1] extract
│     camera_id = alert.camera_id
│     classification = alert.classification
│     camera_label = alert.camera_label
│
├─ [Stage 2] COOLDOWN — infra.pipeline_cooldown.PipelineCooldown
│     if cooldown.should_suppress(camera_id, classification):
│         return {"status": "suppressed", camera_id, classification}
│     (sliding window per (camera, classification); DEFAULT_WINDOWS keyed by class)
│
├─ [Stage 3] frames
│     frames = list(alert.frames)        ← 4 paths from get_recent_frames
│
├─ [Stage 4] GATE — infra.gate.run()
│     verdict = run_gate(
│         frame_paths=frames, camera_name=camera_id,
│         alert_id=alert.id, output_dir="/tmp"
│     )
│     → GateVerdict: {decision, class_label, confidence,
│                     frames: 4x PIL, crop_a, crop_b,
│                     bbox_a, bbox_b, frame_paths, crop_a_path, crop_b_path,
│                     pairwise_diff_path}
│
├─ [Stage 5] GATE SUPPRESS BRANCH
│     if verdict.decision == "suppress":
│         return {"status": "dropped", camera_id, classification,
│                 "reason": verdict.reason, "gate": _gsum(verdict)}
│
├─ [Stage 6] record_hit
│     cooldown.record_hit(camera_id, classification)
│
├─ [Stage 7] VERIFY (VM1) + TG#1
│     crop_a, crop_b = save crop PIL images to /tmp/_ga.jpg, /tmp/_gb.jpg
│     vm1_result = vision_analyzer.verify_class(crop_a_path, crop_b_path)
│         → httpx POST :8080/v1/chat/completions
│            model="vm1_classify", schema=vm1_prompt.SCHEMA_JSON
│            images: b64(jpg)[] × 2
│         → {"class": "vehicle|person|animal", "confidence": float}
│
│     diff = verdict.pairwise_diff_path or (frames[-1] if frames else "/tmp/diff.jpg")
│     tg1 = build_alert_message(
│         verdict=verdict, vm1_result=vm1_result,
│         frames=frames, diff_image=Path(diff), camera_label=camera_label
│     )
│     → {"caption": "...", "photos": [path1, path2, ...]}
│
├─ [Stage 8] DETAIL (VM2)
│     mode = vm1_result.get("class", "vehicle")
│     vm2_result = vision_analyzer.detail_class(mode, crop_a, crop_b)
│         → DISPATCH[mode] picks (schema, prompt_fn) from:
│            vehicle → infra.vehicle_prompt.build_vehicle_prompt
│            person  → infra.person_prompt.build_person_prompt
│            animal  → infra.animal_prompt.build_animal_prompt
│         → schema-enforced JSON response from :8080/v1/chat/completions
│
├─ [Stage 9] TG#2
│     tg2 = build_detail_message(mode, vm2_result,
│                                 Path(crop_a), Path(crop_b),
│                                 camera_label=camera_label)
│
├─ [Stage 10] VEHICLE MATCH (only if mode == "vehicle")
│     candidates = _load_candidates()    ← reads data/vehicles/known_vehicles.json
│                                         (12 entries ported from v1, may be missing)
│     match_result = vehicle_matcher.match_vehicle(vm2_result, candidates)
│
├─ [Stage 11] TG#3 (vehicle only)
│     tg3 = build_match_message(match_result, vm2_result)
│
└─ RETURN envelope:
    {
      "status": "ok",
      "camera_id, classification, frames,
      gate:        {decision, class_label, confidence, reason},
      vm1_result, tg1,
      vm2_result, tg2,
      match_result, tg3
    }


---

## G. GATE INTERNALS — `infra/gate.py` (~760 LOC)

`run(frame_paths[4], camera_name, alert_id, output_dir) -> GateVerdict`

| Step | Operation |
|------|-----------|
| 1 | Load all 4 paths → PIL.Image (cached in `verdict.frames` — no TOCTOU) |
| 2 | `bbox_a = diff_pair_with_bbox(frame_2, frame_3)` — 10% per-side pad + round UP to mult of 32 → drawn as green box on composite AND used directly for `crop_a = frame_2.crop(bbox_a)` |
| 3 | `bbox_b = diff_pair_with_bbox(frame_3, frame_4)` — same rule → drawn as green box on composite AND used directly for `crop_b = frame_3.crop(bbox_b)` |
| 4 | Single bbox per slot. Green box region = exact crop region. No AND intersection, no separate "subject" bbox, no fallback. If both diff bboxes are None, suppress with `reason="no_server_motion"`. |
| 5 | Classify `crop_a` and `crop_b` against COCO. Confidence per crop. |
| 6 | `max(conf_a, conf_b)` → verdict.decision ∈ {"vehicle", "person", "suppress"} |
| 7 | Optionally write `frame_001..004.png`, `crop_a.png`, `crop_b.png`, `pairwise_diff.png` to `data/frames/<camera>/<alert_id>/` if `GATE_KEEP_DISK_ARTIFACTS=true` (default off). |
| 8 | Per-camera thresholds: `camera_name` is looked up to override `diff_threshold` / `min_area_px`. |
| 9 | Night-suppression heuristic via `is_night_at_edt(timestamp)` (Phase 6B.116). Safe default: don't suppress on missing timestamp. |

---

## H. COOLDOWN — `infra/pipeline_cooldown.py`

- Singleton per process (module-level instance created in `pipeline.run()`).
- Per `(camera_id, classification)` sliding window.
- `DEFAULT_WINDOWS` keyed by classification string (vehicle/person/animal/motion/…).
- `should_suppress(cam, class, window_seconds=0)`:
  1. Look up stored `(cam, class)` →
  2. First hit: record `now`, return False (this is the "first pip" of the window).
  3. Subsequent hits within window: return True.
  4. Hit after window: refresh `now`, return False.
- `record_hit(cam, class)` called on Stage 6 AFTER gate passes.

---

## I. VISION — `infra/vision_analyzer.py` → llama-server @ `127.0.0.1:8080`


POST /v1/chat/completions
  model:       "vm1_classify" | "vm2"    (PIDs: Qwen3-VL @ :8080)
  messages:    [{role:user, content:[{type:text, text:prompt()}, b64_image_a, b64_image_b, ...]}]
  response_format: <SCHEMA_JSON>          (vehicle/person/animal)


| Function        | Mode            | Prompt source                          | Schema source                 |
|-----------------|-----------------|----------------------------------------|-------------------------------|
| `verify_class`  | VM1 (classifier) | `infra/vm1_prompt.build_vm1_prompt()` | `infra/vm1_prompt.SCHEMA_JSON` |
| `detail_class`  | VM2 (vehicle)    | `infra.vehicle_prompt.build_vehicle_prompt()` | `infra.vehicle_prompt.SCHEMA_JSON` |
| `detail_class`  | VM2 (person)     | `infra.person_prompt.build_person_prompt()` | `infra.person_prompt.SCHEMA_JSON` |
| `detail_class`  | VM2 (animal)     | `infra.animal_prompt.build_animal_prompt()` | `infra.animal_prompt.SCHEMA_JSON` |
| Anything else    | — raises `VisionAnalyzerError("unknown detail_class mode")` |

Errors → `VisionAnalyzerError` (HTTP non-200, connection failure, parse error).

---

## J. TELEGRAM FORMATTERS — `telegram_formatter/`

| Function                    | File                  | Inputs                                                                | Output                   |
|-----------------------------|-----------------------|-----------------------------------------------------------------------|--------------------------|
| `build_alert_message`       | `alert.py:45`         | `verdict, vm1_result, frames, diff_image, camera_label`                | `{caption, photos}`     |
| `build_detail_message`      | `detail.py:41`        | `mode, vm2_result, crop_a, crop_b, camera_label`                       | `{caption, photos}`     |
| `build_match_message`       | `match_alert.py:38`   | `match_result, vm2_result`                                            | `{caption, photos}`     |

---

## K. VEHICLE MATCHER — `vehicle_matcher/match.py`


match_vehicle(vm2_result: dict, candidates: list[dict]) -> dict
│
└─ candidates loaded from data/vehicles/known_vehicles.json (12 entries)
   Returns {"matched": bool, "entry": dict | None, "score": float, "reason": str}


---

## M. EXTERNAL DEPS

| Component          | Endpoint                              | Notes                          |
|--------------------|---------------------------------------|--------------------------------|
| Reolink cameras    | `<CAM_1_IP>,<CAM_2_IP>,<CAM_6_IP>,<CAM_4_IP>,<CAM_3_IP>,<CAM_7_IP>`    | POST /alert (X-Forwarded-For OK) |
| vision llama-server| `127.0.0.1:8080/v1/chat/completions`  | Qwen3-VL, PID 76829, -np 4     |
| Telegram bot       | env: `TELEGRAM_BOT_TOKEN`             | python-telegram-bot v21+      |
| Home chat          | `<TELEGRAM_CHAT_ID>` (`TELEGRAM_HOME_CHAT_ID`) | operator's home channel   |
| launchd            | `<HOME_DIR>/Library/LaunchAgents/com.farm.surveillance.v2.plist` | KeepAlive=true, RunAtLoad=true |

---

## N. KEY PATHS — `infra/paths.py`


PROJECT_ROOT = <HOME_DIR>/farm-surveillance-v2     (env override)
DATA_DIR     = $PROJECT_ROOT/data         (env: FARMSURV_DATA_DIR)
  FRAMES_DIR = DATA_DIR/frames
  ALERTS_DIR = DATA_DIR/alerts
  VEHICLES_DIR = DATA_DIR/vehicles
    VEHICLE_KNOWN_FILE = $VEHICLES_DIR/known_vehicles.json   ← 12 entries
LOGS_DIR     = $PROJECT_ROOT/logs
  daemon.log, daemon-error.log


---

## O. DEPLOYMENT

- LaunchAgent plist: `<HOME_DIR>/Library/LaunchAgents/com.farm.surveillance.v2.plist`
- Command: `<PROJECT_ROOT>/.venv/bin/python3.11 -m listener.daemon`
- Env injected by plist: `LISTEN_HOST=0.0.0.0`, `LISTEN_PORT=8090`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_HOME_CHAT_ID`, `VISION_LLM_URL=http://127.0.0.1:8080`, `PATH`.
- Working directory injected by plist: `<PROJECT_ROOT>`.
- Currently: **PID 87987** (post 22:02 bounce from PID 56850; old pre-US-014 binary).

---

## P. EARLY-EXIT / DROP PATHS

| Stage | Trigger                     | Return shape                                | HTTP code (in /alert) |
|-------|-----------------------------|---------------------------------------------|-----------------------|
| 1     | bad JSON                    | `{"status":"error","reason":"invalid json"}`| 400                   |
| 1     | unknown shape               | `{"status":"error","reason":"unrecognized payload shape"}` | 400  |
| 1     | source IP not in creds      | `{"status":"error","reason":"IP validation failed"}` | 403           |
| 2     | cooldown window active      | `{"status":"suppressed",...}`               | 200 (drop, not error) |
| 5     | gate decision == suppress   | `{"status":"dropped", "reason", "gate"}`   | 200 (drop, not error) |
| 4-11  | vision/llama-server error   | `VisionAnalyzerError` propagates → `_do_send` failure; telegram skip; pipeline returns {status:?, ...}; daemon returns 200 with that result |

The only conditions that return 4xx to the camera are 400 (bad payload) and 403
(anti-spoof rejection). All other failures are 200 with a degraded body — the camera
doesn't see the difference between "suppressed" and "ok".

---

## Q. ASYNCHRONY

- Pipeline is fully synchronous. The Flask route handler in `daemon.py` runs
  `pipeline.run()` directly; any async dispatch (e.g., for the Telegram client) is
  wrapped with `asyncio.run` at the call site.
- The persistent RTSP readers run in background threads (own PyAV decode loop).
- Reconnect-loop runs in a single daemon thread (`reconnect-loop`).
- The Flask app itself uses the default Werkzeug single-threaded dev server
  (one request at a time on `0.0.0.0:8090`). launchd's KeepAlive respawns on crash.
