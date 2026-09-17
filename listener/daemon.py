"""
daemon.py — Flask webhook server + launchd plist generator for listener.

STATUS: stable
THREAD SAFETY: single-threaded (Flask dev server, single-worker)

INPUTS:
    - env var LISTEN_HOST (default 0.0.0.0) — bind interface
    - env var LISTEN_PORT (default 8090) — TCP port to bind (matches v1)
    - env var TELEGRAM_BOT_TOKEN, TELEGRAM_HOME_CHAT_ID, VISION_LLM_URL
    - POST body: JSON dict — camera alert payload (flat or Reolink shape)

OUTPUTS:
    - HTTP 200 with JSON response from pipeline.run() (via /alert)
    - HTTP 202 with alert_id on /alert accept
    - generate_plist() returns a str: a launchd plist XML

PUBLIC API:
    app — Flask app instance with POST /alert route
    generate_plist() -> str
        Return a plist XML string for manual installation.

DOES NOT DO:
    - Install or start the daemon (operator copies plist manually)
    - Auto-start the service (no auto-start, but plist has KeepAlive=true
      so launchd restarts on crash)
    - Validate camera request signatures (camera_creds handles that)
    - Run as a multi-worker server (dev server only)

WHY HERE:
    The operator needs a ready-to-review daemon before activation.
    Plist generation lives in the same module so the operator can
    pipe `python -m listener.daemon` to stdout and inspect before
    copying to ~/Library/LaunchAgents/.

CALLED BY:
    (external: Reolink camera sends POST /alert)

CALLS INTO:
    - listener.pipeline: run() for the 11-stage pipeline
    - flask: HTTP server + request parsing
    - infra.camera_creds: validate_source_ip() for anti-spoof
    - infra.frame_capture: get_recent_frames() for RTSP frame pull
    - uuid: generate alert_id strings
    - collections: defaultdict for learned camera maps
"""

from __future__ import annotations

import logging
import os
import uuid
from collections import deque
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path

from flask import Flask, jsonify, request

from infra import paths as infra_paths, tg_upload_cleanup
from listener import pipeline
from telegram_formatter import codec, dispatcher
from telegram_formatter.dispatcher import ConfigError, DeliveryError

# ---------------------------------------------------------------------------
# Logging — wire once at import time so every logger in the v2 codebase
# (daemon, frame_capture, gate, pipeline, quick_classifier, …) routes to
# stderr. launchd captures stderr → logs/daemon-error.log per the plist,
# so all WARNING+ diagnostics from any module land in one place.
#
# Honor LOG_LEVEL env var (default INFO). Setting it BEFORE basicConfig
# means operator can debug-class issues without redeploying code.
# US-016g (2026-09-08): consolidated to stream=sys.stdout so logs merge
# with Flask/werkzeug in logs/daemon.log (the launchd StandardOutPath).
# ---------------------------------------------------------------------------
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=__import__("sys").stdout,
    force=True,  # override any earlier basicConfig() (e.g. from a library)
)

app = Flask(__name__)
log = logging.getLogger("daemon")

# Module-level cache of the repo-rooted .env path. Computed at module load
# using __file__ -> repo root; tests can monkeypatch this name to redirect
# lookups. Module-load computation avoids per-call Path traversal cost.
_REPO_ENV_PATH: str = str(Path(__file__).resolve().parent.parent / ".env")


# _format_local_timestamp is defined at the end of this module (after the
# Flask app setup) to avoid a circular-import deadlock:
#   daemon → pipeline → telegram_formatter.alert → daemon
# The actual logic lives in infra/format_ts.py.


# ---------------------------------------------------------------------------
# Learned camera name → camera_id cache (LRU 32)
# ---------------------------------------------------------------------------
class _LearnedCameraMap(MutableMapping):
    """LRU 32 cache mapping camera friendly names to camera IDs.

    Populated from the first sighting of a new camera name in a payload.
    Falls back to a fallback map derived from camera-creds env file.
    """

    def __init__(self, maxlen: int = 32):
        self._maxlen = maxlen
        self._cache: deque[str] = deque(maxlen=maxlen)
        self._map: dict[str, str] = {}

    def __getitem__(self, key: str) -> str:
        return self._map[key]

    def __setitem__(self, key: str, value: str) -> None:
        if key not in self._map:
            if len(self._map) >= self._maxlen:
                oldest = self._cache.popleft()
                self._map.pop(oldest, None)
            self._cache.append(key)
        self._map[key] = value

    def __delitem__(self, key: str) -> None:
        self._cache.remove(key)
        del self._map[key]

    def __iter__(self):
        return iter(self._map)

    def __len__(self):
        return len(self._map)

    def learn(self, friendly_name: str, camera_id: str) -> None:
        """Learn a mapping from friendly name to camera_id."""
        self[friendly_name] = camera_id


_learned_camera_map = _LearnedCameraMap(maxlen=32)


# ---------------------------------------------------------------------------
# Payload normalization helpers
# ---------------------------------------------------------------------------


def normalize_reolink(payload: dict, source_ip: str) -> dict | None:
    """Normalize a Reolink default webhook payload to v2 alert dict.

    Accepts:
        {
          "type": "motion",
          "alarm": {
            "alarmTime": "...",
            "channelName": "Front Door Outside",
            "device": "Front Door Outside",
            "name": "...",
            "time": "2026-09-07T20:00:00Z",
            "type": "person|vehicle|animal|motion",
            ...
          }
        }

    Returns None if required keys are missing.
    """
    alarm = payload.get("alarm")
    if not isinstance(alarm, dict):
        return None

    # Prefer channelName, then device, then name for camera identification
    device_name = alarm.get("channelName") or alarm.get("device") or alarm.get("name")
    if not device_name:
        return None

    event_type = alarm.get("type")
    if event_type is None:
        raise KeyError("alarm missing required key 'type'")
    outer_type = payload.get("type")
    if outer_type and outer_type != event_type:
        event_type = outer_type
    if not isinstance(event_type, str):
        raise TypeError(f"alarm 'type' must be a string, got {type(event_type).__name__}: {event_type!r}")
    event_type = event_type.lower()

    ts_key = alarm.get("time") or alarm.get("alarmTime")
    if ts_key is None:
        log.warning("alarm missing timestamp keys 'time' and 'alarmTime'; using now()")
        timestamp = datetime.now(UTC).isoformat()
    else:
        timestamp = ts_key

    return {
        "id": str(uuid.uuid4()),
        "camera_id": device_name,
        "camera_label": device_name,
        "classification": event_type,
        "frames": [],
        "timestamp": timestamp,
    }


# ---------------------------------------------------------------------------
# /alert route
# ---------------------------------------------------------------------------


@app.post("/alert")
def alert():
    """Accept camera alerts from Reolink webhooks (v2 alert shape).

    Accepts two payload shapes:
    1. Flat: {camera, ip, event, timestamp} — v1 simple format
    2. Reolink default: {type, alarm: {...}} — nested Reolink format

    Normalizes to v2 alert dict, validates source IP, returns 202 + alert_id.
    """
    source_ip = (
        request.headers.get("X-Forwarded-For", request.remote_addr or "")
        .split(",")[0]
        .strip()
    )

    # Parse JSON
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"status": "error", "reason": "invalid json"}), 400

    # Normalize Reolink nested payload to v2 alert dict.
    alert_dict = normalize_reolink(payload, source_ip)

    if alert_dict is None:
        return jsonify(
            {
                "status": "error",
                "reason": "unrecognized payload shape (Reolink nested expected)",
            }
        ), 400

    camera_id = alert_dict["camera_id"]
    camera_label = alert_dict["camera_label"]

    # Validate source IP via camera_creds
    from infra.camera_creds import validate_source_ip

    if not validate_source_ip(camera_id, source_ip):
        # Try to find a camera whose friendly name matches camera_label
        from infra.camera_creds import get_all_cameras

        all_cameras = get_all_cameras()
        matched_cam_id = None
        for cam in all_cameras.values():
            if cam.get("name") == camera_label and source_ip == cam.get("ip"):
                matched_cam_id = cam.get("prefix", camera_id)
                break
        if matched_cam_id is None:
            return jsonify({"status": "error", "reason": "IP validation failed"}), 403

        # Learn the mapping for next time
        _learned_camera_map.learn(camera_label, matched_cam_id)
        alert_dict["camera_id"] = matched_cam_id
        camera_id = matched_cam_id

    # Pull recent frames from the persistent RTSP reader.
    # `get_recent_frames` is imported from `infra.frame_capture`.
    from infra.frame_capture import get_recent_frames

    n_frames = 4
    offset_seconds = 6
    alert_dict["frames"] = get_recent_frames(
        camera_id, n=n_frames, offset_seconds=offset_seconds
    )

    # Run the alert through the full pipeline
    # (pipeline + dispatcher are imported at module top so tests can patch them)
    result = pipeline.run(alert_dict)

    # US-050b: per-stage Telegram dispatch.
    # Each TG-producing stage (TG#1, TG#2, TG#3) is dispatched
    # immediately after pipeline.run() returns. Failures of one
    # dispatch do NOT unwind the others (each is independent).
    # Daemon still returns HTTP 200 even if dispatch fails —
    # operator sees the gap in logs/daemon.log, not as a
    # camera-side retry storm.
    alert_id = result.get("id", "unknown")
    chat_id = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    # ---- TG#1 dispatch (after stage 10: build_tg1) ----
    _send_tg1(result.get("tg1"), alert_id, chat_id, bot_token)

    # ---- TG#2 dispatch (after stage 11: build_tg2) ----
    _send_tg2(result.get("tg2"), alert_id, chat_id, bot_token)

    # ---- TG#3 dispatch (after stage 13: build_tg3) ----
    _send_tg3(result.get("tg3"), alert_id, chat_id, bot_token)

    # Sweep old cache dirs (24h retention) — after all per-stage dispatches.
    try:
        tg_upload_cleanup.sweep()
    except Exception:  # noqa: BLE001
        log.warning("tg_upload_cleanup: post-dispatch sweep failed (non-fatal)")

    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Per-stage dispatch helpers (TG#1, TG#2, TG#3)
# Each helper pre-converts photos (TG#1/TG#2), then calls dispatcher.dispatch().
# TG#3 is text-only (no photo conversion). Failure is logged; does not raise.
# ---------------------------------------------------------------------------


def _convert_photos(alert_id: str, photo_paths: list[str]) -> list[str]:
    """Pre-convert source photo paths to cached JPEGs.

    Returns the list of cached JPEG paths. Empty if none convert.
    """
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    msg_cache_dir = str(
        Path(infra_paths.TG_UPLOADS_DIR) / date_str / alert_id
    )
    converted: list[str] = []
    for src_path in photo_paths:
        jpg = codec.encode_jpeg(
            src_path, msg_cache_dir, jpeg_quality=88, max_dim=1920
        )
        if jpg is not None:
            converted.append(jpg)
    return converted


def _send_tg1(tg_msg: dict | None, alert_id: str, chat_id: str, bot_token: str) -> None:
    """Dispatch TG#1: alert message with photos."""
    if not tg_msg or not bot_token:
        if tg_msg and not bot_token:
            log.warning("tg-send: alert_id=%s TG#1 missing TELEGRAM_BOT_TOKEN, skipping", alert_id)
        return
    orig_photos = tg_msg.get("photos") or []
    if orig_photos:
        tg_msg["photos"] = _convert_photos(alert_id, orig_photos)
    try:
        responses = dispatcher.dispatch(
            [tg_msg], bot_token=bot_token, chat_id=chat_id,
        )
        converted_photos = tg_msg.get("photos") or []
        upload_bytes = sum(os.path.getsize(p) for p in converted_photos) if converted_photos else 0
        method = "sendMediaGroup" if converted_photos else "sendMessage"
        log.info(
            "tg-send: alert_id=%s TG#1 chat=%s method=%s sources=%d converted=%d upload=%d response=HTTP %d",
            alert_id, chat_id, method, len(orig_photos), len(converted_photos),
            upload_bytes, responses[0].status_code if responses else 0,
        )
    except (ConfigError, DeliveryError) as exc:
        log.error("tg-send: alert_id=%s TG#1 error=%s: %s", alert_id, type(exc).__name__, exc)
    except Exception as exc:  # noqa: BLE001
        log.error("tg-send: alert_id=%s TG#1 unexpected %s: %s", alert_id, type(exc).__name__, exc)


def _send_tg2(tg_msg: dict | None, alert_id: str, chat_id: str, bot_token: str) -> None:
    """Dispatch TG#2: detail message with photos."""
    if not tg_msg or not bot_token:
        if tg_msg and not bot_token:
            log.warning("tg-send: alert_id=%s TG#2 missing TELEGRAM_BOT_TOKEN, skipping", alert_id)
        return
    orig_photos = tg_msg.get("photos") or []
    if orig_photos:
        tg_msg["photos"] = _convert_photos(alert_id, orig_photos)
    try:
        responses = dispatcher.dispatch(
            [tg_msg], bot_token=bot_token, chat_id=chat_id,
        )
        converted_photos = tg_msg.get("photos") or []
        upload_bytes = sum(os.path.getsize(p) for p in converted_photos) if converted_photos else 0
        method = "sendMediaGroup" if converted_photos else "sendMessage"
        log.info(
            "tg-send: alert_id=%s TG#2 chat=%s method=%s sources=%d converted=%d upload=%d response=HTTP %d",
            alert_id, chat_id, method, len(orig_photos), len(converted_photos),
            upload_bytes, responses[0].status_code if responses else 0,
        )
    except (ConfigError, DeliveryError) as exc:
        log.error("tg-send: alert_id=%s TG#2 error=%s: %s", alert_id, type(exc).__name__, exc)
    except Exception as exc:  # noqa: BLE001
        log.error("tg-send: alert_id=%s TG#2 unexpected %s: %s", alert_id, type(exc).__name__, exc)


def _send_tg3(tg_msg: dict | None, alert_id: str, chat_id: str, bot_token: str) -> None:
    """Dispatch TG#3: match message (text-only)."""
    if not tg_msg or not bot_token:
        if tg_msg and not bot_token:
            log.warning("tg-send: alert_id=%s TG#3 missing TELEGRAM_BOT_TOKEN, skipping", alert_id)
        return
    # TG#3 is text-only (photos=[]), no conversion needed.
    try:
        responses = dispatcher.dispatch(
            [tg_msg], bot_token=bot_token, chat_id=chat_id,
        )
        converted_photos = tg_msg.get("photos") or []
        upload_bytes = sum(os.path.getsize(p) for p in converted_photos) if converted_photos else 0
        method = "sendMediaGroup" if converted_photos else "sendMessage"
        log.info(
            "tg-send: alert_id=%s TG#3 chat=%s method=%s sources=%d converted=%d upload=%d response=HTTP %d",
            alert_id, chat_id, method, 0, len(converted_photos),
            upload_bytes, responses[0].status_code if responses else 0,
        )
    except (ConfigError, DeliveryError) as exc:
        log.error("tg-send: alert_id=%s TG#3 error=%s: %s", alert_id, type(exc).__name__, exc)
    except Exception as exc:  # noqa: BLE001
        log.error("tg-send: alert_id=%s TG#3 unexpected %s: %s", alert_id, type(exc).__name__, exc)


def generate_plist() -> str:
    """Return a launchd plist XML string for manual installation.

    The operator copies this output to
    ~/Library/LaunchAgents/com.farm.surveillance.v2.plist.

    Includes full environment so the daemon finds Telegram + LLM creds.
    """
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = os.environ.get("LISTEN_PORT", "8090")

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")
    vision_url = os.environ.get("VISION_LLM_URL", "http://127.0.0.1:8080")

    # WorkingDirectory must be the v2 repo root so relative imports resolve.
    workdir = infra_paths.PROJECT_ROOT

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.farm.surveillance.v2</string>

    <key>ProgramArguments</key>
    <array>
        <string>{os.path.join(workdir, ".venv", "bin", "python3.11")}</string>
        <string>-m</string>
        <string>listener.daemon</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{workdir}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{os.path.join(workdir, ".venv", "bin")}:/usr/local/bin:/usr/bin:/bin</string>
        <key>LISTEN_HOST</key>
        <string>{host}</string>
        <key>LISTEN_PORT</key>
        <string>{port}</string>
        <key>TELEGRAM_BOT_TOKEN</key>
        <string>{tg_token}</string>
        <key>TELEGRAM_HOME_CHAT_ID</key>
        <string>{tg_chat_id}</string>
        <key>VISION_LLM_URL</key>
        <string>{vision_url}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{os.path.join(workdir, "logs", "daemon.log")}</string>

    <key>StandardErrorPath</key>
    <string>{os.path.join(workdir, "logs", "daemon-error.log")}</string>

    <key>SoftResourceLimits</key>
    <integer>256</integer>
</dict>
</plist>
"""


def main():
    """Entry point for `python -m listener.daemon`.

    Boots the persistent RTSP reader registry for all configured cameras
    BEFORE the Flask server accepts requests, so the first /alert has
    a reader ready. start_all() joins each reader's boot thread with a
    30s timeout, so this blocks briefly at startup.

    US-017b: also loads ~/.env (gitignored) into os.environ at boot so the
    daemon can pick up secrets like TELEGRAM_BOT_TOKEN without them living
    in the launchd plist. Existing env vars (set by launchd) take precedence.
    """
    # US-017b: load ~/.env so TELEGRAM_BOT_TOKEN + TELEGRAM_HOME_CHAT_ID
    # come from the operator's home directory rather than the launchd plist.
    _load_home_env()

    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("LISTEN_PORT", "8090"))

    import infra.camera_creds as _camera_creds
    from infra.frame_capture import CameraCaptureRegistry

    n_cameras = len(_camera_creds.get_all_cameras())
    log.info("Booting RTSP readers for %d camera(s)...", n_cameras)
    CameraCaptureRegistry.start_all()
    log.info("RTSP registry ready.")

    # US-030a: sweep stale tg_uploads dirs on boot (24h retention).
    try:
        sweep_result = tg_upload_cleanup.sweep()
        log.info(
            "tg_upload_cleanup: boot sweep done — dirs_removed=%d errors=%d",
            sweep_result["deleted_dirs"],
            sweep_result["errors"],
        )
    except Exception:  # noqa: BLE001
        log.warning("tg_upload_cleanup: boot sweep failed (non-fatal)")

    app.run(host=host, port=port)


def _load_home_env() -> None:
    """Load .env files into os.environ. Existing keys win over file values.

    Resolution order (first file found wins for any given key, but already-set
    env vars always win over file values):

      1. <repo>/.env          — operator-owned, in-repo, mode 600 (canonical)
      2. ~/.env               — operator-global fallback (legacy)

    US-017b: secrets (telegram token, etc.) live in a gitignored .env file
    rather than the launchd plist so they can be rotated without plist edits
    and so the plist does not contain plaintext credentials.

    PRD-V2-022 (2026-09-09): operator directive — env file belongs INSIDE the
    app directory so the app is self-contained. ~/.env is retained only as a
    fallback for operators who keep their secrets at $HOME.
    """
    from dotenv import dotenv_values  # python-dotenv

    # 1. Repo-rooted .env (canonical — operator directive 2026-09-09).
    #    __file__ -> .../listener/daemon.py -> repo root is parent.parent.
    #    Exposed at module level so tests can monkeypatch this constant
    #    without having to mock Path traversal.
    candidate_paths = [
        _REPO_ENV_PATH,
        # 2. Legacy ~/.env fallback for operators who keep secrets at $HOME.
        os.path.expanduser("~/.env"),
    ]

    total_loaded = 0
    for env_path in candidate_paths:
        if not os.path.exists(env_path):
            log.info("env: %s not present; skipping.", env_path)
            continue
        values = dotenv_values(env_path) or {}
        loaded_here = 0
        for k, v in values.items():
            if v is None:
                continue
            # Launchd-injected values win so operators can override without
            # touching the .env file.
            if k not in os.environ:
                os.environ[k] = v
                loaded_here += 1
        total_loaded += loaded_here
        log.info(
            "env: loaded %d var(s) from %s",
            loaded_here,
            env_path,
        )

    log.info(
        "env load complete: total=%d (telegram_token_len=%d)",
        total_loaded,
        len(os.environ.get("TELEGRAM_BOT_TOKEN", "")),
    )


# ---------------------------------------------------------------------------
# /debug/rtsp — live RTSP reader ring-buffer stats (operator introspection).
# Returns frame counts, ring fill levels, and last-frame age per camera.
# ---------------------------------------------------------------------------


@app.get("/debug/rtsp")
def debug_rtsp():
    """Return live stats for all persistent RTSP readers."""
    from infra.frame_capture import CameraCaptureRegistry

    stats = CameraCaptureRegistry.stats_all()
    payload = {
        "registry_present": bool(stats),
        "cameras": {
            cid: {
                "frames_decoded_total": s["frames_decoded_total"],
                "ring_size": s["ring_size"],
                "ring_capacity": s["ring_capacity"],
                "healthy_flag": s["healthy_flag"],
                "seconds_since_last_frame": s["seconds_since_last_frame"],
                "consecutive_errors": s["consecutive_errors"],
                "reconnects_total": s["reconnects_total"],
                "container_open": s["container_open"],
                "is_running": s["is_running"],
            }
            for cid, s in stats.items()
        },
    }
    return (payload, 200)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# _format_local_timestamp — UTC ISO → local display string
# ---------------------------------------------------------------------------
# Must be defined here (at module bottom) to avoid a circular-import deadlock:
#   daemon → pipeline → telegram_formatter.alert → daemon
# Display modules import directly from infra.format_ts to avoid the cycle.
def _format_local_timestamp(utc_iso: str) -> str:
    """Convert a UTC ISO-8601 timestamp to local display string.

    See :py:func:`infra.format_ts.format_local_timestamp` for full docs.
    """
    return __import__("infra.format_ts").format_ts.format_local_timestamp(
        utc_iso, tz_name=os.environ.get("DISPLAY_TZ"),
    )
