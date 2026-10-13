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
    - HTTP 200 with JSON pipeline result (via /alert, segmented stages)
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
    - listener.pipeline: 10+ stage functions for segmented pipeline
    - flask: HTTP server + request parsing
    - infra.camera_creds: validate_source_ip() for anti-spoof
    - infra.frame_capture: capture_around_now() for RTSP frame pull
    - uuid: generate alert_id strings
    - collections: defaultdict for learned camera maps
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import MutableMapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Env loading (must happen BEFORE any infra imports, because infra modules
# capture env vars at import time.  US-069-regression root cause:
# quick_classifier.py read os.environ before _load_home_env ran,
# freezing values to [].  Solution: load .env FIRST, then infra imports.
# ---------------------------------------------------------------------------
_REPO_ENV_PATH: str = str(Path(__file__).resolve().parent.parent / ".env")


def _load_home_env() -> int:
    """Load .env files into os.environ.

    Resolution order (precedence, lowest to highest):

      1. <repo>/.env          — operator-owned, in-repo, mode 600
      2. ~/.env               — operator-global fallback (legacy)
      3. <repo>/telegram-creds.env -- per-app creds (highest precedence)
      4. <repo>/camera-creds.env   -- per-app creds
      5. <repo>/llm-creds.env      -- per-app creds

    For steps 1-2: existing env vars win over file values
    (``if k not in os.environ``). This lets launchd-injected env vars
    and prior file loads take priority.

    For steps 3-5 (per-app creds files): ALL keys override existing values.
    This is the core safety mechanism from US-101z -- if a worker uses
    write_file on <repo>/.env and clobbers the Telegram token with a
    placeholder, the real token in telegram-creds.env still wins.
    Incident 2026-10-05: 3 alerts lost to Telegram 404 when .env was
    overwritten.

    US-017b: secrets (telegram token, etc.) live in a gitignored .env file
    rather than the launchd plist so they can be rotated without plist edits
    and so the plist does not contain plaintext credentials.

    PRD-V2-022 (2026-09-09): operator directive — env file belongs INSIDE the
    app directory so the app is self-contained. ~/.env is retained only as a
    fallback for operators who keep their secrets at $HOME.

    NOTE: This function is called at module-import time, BEFORE any
    `from infra import ...` line below.  This ensures every infra module
    sees the correct env values when it reads os.environ at import time.

    CRITICAL IMPORT-ORDER FIX (US-103x-2, 2026-10-09):
    The .env load (steps 1-2) runs BEFORE the `from infra import paths`
    import. Without this ordering, the infra init chain (alert_overrides_baseline
    → quick_classifier) hits a RuntimeError because os.environ is empty at
    import time and _require_env_lower_bool() finds no value for
    MOTION_GATE_NIGHT_SUPPRESS_ENABLED.

    Returns:
        Total number of keys loaded/overridden across all files.
    """
    from dotenv import dotenv_values  # python-dotenv

    # Paths for steps 1-2: existing keys win (no override)
    candidate_paths = [
        _REPO_ENV_PATH,
        os.path.expanduser("~/.env"),
    ]

    # CRITICAL: load .env BEFORE importing infra, because
    # infra/__init__.py -> alert_overrides_baseline -> quick_classifier
    # calls _require_env_lower_bool() at module-import time.
    _keys_loaded_by_this_call: set[str] = set()
    for env_path in candidate_paths:
        if not os.path.exists(env_path):
            continue
        values = dotenv_values(env_path) or {}
        for k, v in values.items():
            if v is None:
                continue
            if k not in os.environ:
                os.environ[k] = v
                _keys_loaded_by_this_call.add(k)

    # NOW safe to import infra (env vars are already loaded)
    from infra import paths as _p  # for cred file paths

    # Paths for steps 3-5: override everything (last wins)
    creds_paths = [
        str(_p.TELEGRAM_CREDS_FILE),
        str(_p.CAMERA_CREDS_FILE),
        str(_p.LLM_CREDS_FILE),
    ]

    # Per-app creds file labels for boot logging (F6)
    creds_labels = {
        str(_p.TELEGRAM_CREDS_FILE): "telegram",
        str(_p.CAMERA_CREDS_FILE): "camera",
        str(_p.LLM_CREDS_FILE): "llm",
    }

    total_loaded = len(_keys_loaded_by_this_call)

    # Steps 3-5: per-app creds files (override everything)
    for env_path in creds_paths:
        if not os.path.exists(env_path):
            continue
        values = dotenv_values(env_path) or {}
        for k, v in values.items():
            if v is None:
                continue
            os.environ[k] = v  # always override (last wins)
            total_loaded += 1

    # F6: log which creds files were loaded and which were missing/empty
    _log_creds_status(creds_labels, total_loaded)

    return total_loaded


def _log_creds_status(creds_labels: dict[str, str], total_loaded: int) -> None:
    """Log the status of per-app creds files at boot time.

    Formats a summary like:
        "Loaded creds: telegram=ok camera=ok llm=missing"
    No secrets are logged.

    NOTE: This runs at import time (before the logging config is fully
    wired up), so it writes to stderr directly rather than through the
    logger. The logging setup at line 153+ captures this if basicConfig
    has already run; otherwise it's a fallback to stderr.
    """
    import logging

    # At import time, basicConfig hasn't run yet (it's at line 163+).
    # Use a temporary root logger that writes to stderr as a fallback.
    tmp_log = logging.getLogger("daemon._creds")
    if not tmp_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        tmp_log.addHandler(handler)
        tmp_log.setLevel(logging.INFO)

    statuses: dict[str, str] = {}
    for cred_path, label in creds_labels.items():
        if not os.path.exists(cred_path):
            statuses[label] = "missing"
            continue
        # Check for world-readable creds files (security warning, F4)
        try:
            file_stat = os.stat(cred_path)
            file_mode = file_stat.st_mode & 0o777
            if file_mode & 0o007:  # world-readable bits set
                tmp_log.warning(
                    "creds file %s is world-readable (mode=%s); "
                    "set mode 600 to restrict access",
                    cred_path, oct(file_mode),
                )
        except OSError:
            statuses[label] = "unreadable"
            continue
        # Check if file has any non-comment, non-blank content
        has_content = False
        try:
            with open(cred_path) as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        has_content = True
                        break
        except OSError:
            statuses[label] = "unreadable"
            continue
        statuses[label] = "ok" if has_content else "empty"

    parts = [f"{label}={status}" for label, status in statuses.items()]
    tmp_log.info("Loaded creds: %s", " ".join(parts))
    tmp_log.info("env: loaded %d total keys across all env files", total_loaded)


# Load .env BEFORE any infra imports.  The return value is stored in
# _ENV_TOTAL_LOADED for logging in main().
_ENV_TOTAL_LOADED = _load_home_env()

from infra import (
    paths as infra_paths,
    pipeline_cooldown as infra_pipeline_cooldown,
    tg_upload_cleanup,
)
from infra.env_required import _require_env
from infra.event_record import write_event_record
from infra.quiet_hours import in_quiet_hours
from listener.health import _record_alert_run  # type: ignore[attr-defined]
from listener.pipeline import (
    stage_apply_baseline_override,
    stage_build_tg1,
    stage_build_tg2,
    stage_build_tg3,
    stage_cooldown_check,
    stage_detail_class_vm2,
    stage_load_frames,
    stage_perform_match,
    stage_prepare_artifacts,
    stage_verify_class_vm1,
)
from telegram_formatter import codec, dispatcher
from telegram_formatter.dispatcher import ConfigError, DeliveryError

# ---------------------------------------------------------------------------
# Logging — wire once at import time so every logger in the v2 codebase
# (daemon, frame_capture, gate, pipeline, quick_classifier, …) routes to
# logs/daemon.log via a RotatingFileHandler (50 MB × 5 backups).
#
# Honor LOG_LEVEL env var (default INFO). Setting it BEFORE basicConfig
# means operator can debug-class issues without redeploying code.
# ---------------------------------------------------------------------------
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE = str(Path(__file__).resolve().parent.parent / "logs" / "daemon.log")
handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=50 * 1024 * 1024,
    backupCount=5,
    delay=True,
)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    handlers=[handler],
    force=True,  # override any earlier basicConfig() (e.g. from a library)
)

app = Flask(__name__)
log = logging.getLogger("daemon")


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
        raise TypeError(
            f"alarm 'type' must be a string, got {type(event_type).__name__}: {event_type!r}"
        )
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
# Box identifier
# ---------------------------------------------------------------------------


def _write_event(
    alert_id: str,
    camera_id: str,
    frames: list,
    gate_result: dict,
    vm1_result: dict | None,
    started_at: str,
    dropped: str | None = None,
    vm2_result: dict | None = None,
    match_result: dict | None = None,
    artifact_dir: str = "",
) -> None:
    """Emit an event.json record for this pipeline run.

    Wraps ``write_event_record`` with pipeline-specific data assembly.
    Does not raise -- errors are logged and the alert proceeds.
    """
    try:
        finished_at = datetime.now(UTC).isoformat()
        frame_paths = [str(f) for f in frames]
        gate_summary = gate_result.get("gsum", {})
        write_event_record(
            alert_id=alert_id,
            camera_id=camera_id,
            box_id=_box_id(),
            started_at=started_at,
            finished_at=finished_at,
            gate=gate_summary,
            vm1=vm1_result,
            vm2=vm2_result,
            match=match_result,
            dropped=dropped,
            frame_paths=frame_paths,
            artifact_dir=artifact_dir,
        )
    except Exception:
        log.exception(
            "event_record: alert_id=%s camera=%s failed to write event.json",
            alert_id, camera_id,
        )


def _box_id() -> str:
    """Return the multi-box namespace identifier.

    Priority: FARMSURV_BOX_ID env > HOSTNAME env > "unknown".
    """
    box = os.environ.get("FARMSURV_BOX_ID")
    if box:
        return box
    box = os.environ.get("HOSTNAME")
    if box:
        return box
    return "unknown"


# ---------------------------------------------------------------------------
# Segmented pipeline orchestrator
# ---------------------------------------------------------------------------


def _run_pipeline(alert: dict) -> dict:
    """Orchestrate the 10+ stage functions into the original result shape.

    This replaces the deleted monolithic ``run()`` from ``listener.pipeline``.
    The return dict has the same keys the rest of the /alert route expects
    (``status``, ``gate``, ``vm1_result``, ``tg1``, ``vm2_result``, ``tg2``,
    ``match_result``, ``tg3``, ``classification``, ``camera_id``, ``frames``).

    After every pipeline run (whether dropped or ok), writes a per-alert
    event.json record via ``infra.event_record.write_event_record``.
    """
    camera_id = alert.get("camera_id", "unknown")
    alert_id = alert.get("id", camera_id)
    classification = alert.get("classification", "motion")
    camera_label = alert.get("camera_label", camera_id)
    started_at = datetime.now(UTC).isoformat()

    # Parse the webhook's ISO 8601 timestamp so the gate can use it for
    # night-suppression checks.  _stage_gate falls back to datetime.now(UTC)
    # when the key is missing (same as the existing _parse_alarm fallback).
    ts_raw = alert.get("timestamp")
    if ts_raw:
        try:
            alert_ts = datetime.fromisoformat(ts_raw)
        except (TypeError, ValueError):
            alert_ts = datetime.now(UTC)
    else:
        alert_ts = datetime.now(UTC)

    # Stage 3: load frames (await capture future if present).
    frames = stage_load_frames(alert)

    # Stage 4: YOLO gate.
    gate_result = _stage_gate(frames, camera_id, alert_id, alert_ts)
    if gate_result["status"] == "dropped":
        _write_event(
            alert_id, camera_id, frames, gate_result, None,
            started_at, dropped="gate_dropped",
        )
        _record_alert_run()
        return {
            "id": alert_id,
            "status": "dropped",
            "reason": gate_result["reason"],
            "classification": "none",
            "gate": gate_result["gsum"],
        }

    # Stage 7b: log proceeded past gate.
    gsum = gate_result["gsum"]
    log.info(
        f"pipeline: proceeded alert_id={alert_id} "
        f"camera={camera_id} classification={classification} "
        f"top_class='{gsum['class_label']}' "
        f"top_confidence={gsum['confidence']}"
    )

    # Stage 8: prepare artifacts.
    art = stage_prepare_artifacts(
        gate_result["gate_verdict"], camera_id, alert_id
    )
    a_p = art["crop_a_path"]
    b_p = art["crop_b_path"]

    # Guard: gate must have produced crops.
    if a_p is None or b_p is None:
        raise RuntimeError(f"gate produced no crops for alert {alert.get('id')}")

    # Stage 9: verify_class VM1.
    vm1_result = stage_verify_class_vm1(a_p, b_p)

    # Stage 9b: VM1 unsure short-circuit (operator 2026-09-20 ~20:30 EDT).
    # Precedent: alert b1d400da-df87-4102-8d04-bdd7a9c16a1d. YOLO said
    # "cat 0.79" on grass-blade motion (motion-gate cleared it because
    # 0.79 > NIGHT_CONF_FLOOR=0.40 and cat isn't on the implausible-class
    # list). VM1 then returned {"class": "unsure", "subclass": "ambiguous"}
    # because there's no actual animal in the frame. The old pipeline still
    # built TG#1+TG#2+TG#3 with that unsure answer, sending a useless
    # notification. Now: if VM1 can't identify the class, drop the alert.
    if vm1_result.get("class") == "unsure":
        log.info(
            "pipeline: dropped alert_id=%s camera=%s reason=vm1_unsure "
            "vm1_class=unsure vm1_subclass=%r vm1_confidence=%r",
            alert_id, camera_id,
            vm1_result.get("subclass"),
            vm1_result.get("confidence"),
        )
        _write_event(
            alert_id, camera_id, frames, gate_result, vm1_result,
            started_at, dropped="vm1_unsure",
        )
        _record_alert_run()
        return {
            "id": alert_id,
            "status": "dropped",
            "reason": "vm1_unsure",
            "classification": classification,
            "gate": gate_result["gsum"],
            "vm1_result": vm1_result,
        }

    # Stage 9c: VM1 override — VM1 owns the routing decision (US-091, 2026-09-27).
    # If VM1 produced a valid class that differs from what the gate set,
    # override the classification. This fixes the case where YOLO misrouted
    # (e.g., "cat 0.81" on a person → VM1 says "person" → pipeline uses "person").
    vm1_class = vm1_result.get("class")
    if vm1_class in ("vehicle", "person", "animal") and vm1_class != classification:
        log.info(
            f"vm1_override_gate alert_id={alert_id} camera={camera_id} "
            f"gate={classification} vm1={vm1_class}"
        )
        classification = vm1_class

    # Stage 7a: cooldown check — NOW uses VM1-derived classification (moved
    # from before VM1 so cooldown keys on the final routing class, not gate's).
    cooldown_result = stage_cooldown_check(camera_id, classification)
    if cooldown_result["status"] == "dropped":
        _write_event(
            alert_id, camera_id, frames, gate_result, vm1_result,
            started_at, dropped="cooldown_dropped",
        )
        _record_alert_run()
        return {
            "id": alert_id,
            "status": "dropped",
            "reason": cooldown_result["reason"],
            "classification": classification,
            "gate": gate_result["gsum"],
            "vm1_result": vm1_result,
        }

    # Stage 10: build TG#1.
    tg1 = stage_build_tg1(
        gate_result["gate_verdict"], vm1_result, a_p, b_p,
        art["composite_path"],
        camera_label, alert,
    )

    # Stage 10b: detail_class VM2.
    mode = vm1_result.get("class", "vehicle")
    vm2_result = stage_detail_class_vm2(mode, a_p, b_p)

    # Stage 11: build TG#2.
    tg2 = stage_build_tg2(mode, vm2_result, a_p, b_p, camera_label, alert)

    # Stage 12: per-class match (person branch gated to FRONT/BACK cameras).
    match_result = stage_perform_match(mode, vm2_result, camera_label)

    # Stage 13: build TG#3 (text only; dispatch handled by caller).
    tg3 = stage_build_tg3(
        mode, match_result, vm2_result,
        a_p, b_p, camera_label, alert,
    )

    # Stage 13b: distant-vehicle baseline override (third layer of defense).
    alert = stage_apply_baseline_override(
        alert, vm1_result, camera_id, datetime.now(UTC)
    )

    # Write final event record (non-dropped).
    _write_event(
        alert_id, camera_id, frames, gate_result, vm1_result,
        started_at,
        vm2_result=vm2_result, match_result=match_result,
        artifact_dir=art["composite_path"],
    )

    # Record pipeline run timestamp for health heartbeat alerts_last_10m.
    _record_alert_run()

    return {
        "id": alert_id,
        "status": "ok",
        "camera_id": camera_id,
        "classification": classification,
        "frames": frames,
        "gate": gate_result["gsum"],
        "vm1_result": vm1_result,
        "tg1": tg1,
        "vm2_result": vm2_result,
        "tg2": tg2,
        "match_result": match_result,
        "tg3": tg3,
    }


def _stage_gate(frames, camera_id, alert_id, timestamp):
    """Stage 4: run YOLO gate on frames.

    Thin wrapper around ``infra.gate.run`` to keep the orchestrator
    import-free (avoids a circular import with listener.pipeline).

    Args:
        timestamp: tz-aware datetime from the webhook (used by night-suppress).
    """
    from infra.gate import run as run_gate

    gate_verdict = run_gate(
        frame_paths=frames,
        camera_name=camera_id,
        alert_id=alert_id,
        output_dir=str(infra_paths.data_dir_for(camera_id, alert_id)),
        timestamp=timestamp,
    )

    if gate_verdict.is_none():
        log.info(
            f"pipeline: dropped alert_id={alert_id} "
            f"camera={camera_id} classification=none "
            f"top_class='{gate_verdict.top_class}' "
            f"top_confidence={gate_verdict.top_confidence} "
            f"reason=no_class"
        )
        return {
            "status": "dropped",
            "reason": "no_class",
            "classification": "none",
            "gate_verdict": gate_verdict,
            "gsum": {
                "classification": gate_verdict.classification,
                "class_label": gate_verdict.class_label,
                "confidence": gate_verdict.confidence,
                "reason": gate_verdict.reason,
            },
        }

    return {
        "status": "ok",
        "gate_verdict": gate_verdict,
        "gsum": {
            "classification": gate_verdict.classification,
            "class_label": gate_verdict.class_label,
            "confidence": gate_verdict.confidence,
            "reason": gate_verdict.reason,
        },
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
    # Generate alert_id first so frames land in a per-alert directory,
    # preventing the shared-filename collision that caused TG#1 time-skew.
    alert_id = alert_dict.get("id") or str(uuid.uuid4())
    alert_dict["id"] = alert_id
    alert_dir = str(infra_paths.data_dir_for(camera_id, alert_id))

    n_frames = 4
    # capture_around_now spawns a daemon thread; Future returns immediately.
    frame_future = None
    from infra.frame_capture import CameraCaptureRegistry

    reader = CameraCaptureRegistry.get(camera_id)
    if reader is None:
        log.error(
            "No RTSP reader for camera %s — returning 503",
            camera_id,
        )
        return jsonify({"status": "error", "reason": "no_reader"}), 503

    try:
        frame_future = reader.capture_around_now(
            count=n_frames, stride_s=2.0, output_dir=alert_dir
        )
    except Exception:
        log.exception(
            "capture_around_now failed for %s — no fallback available",
            camera_id,
        )
        return jsonify({"status": "error", "reason": "capture_failed"}), 503

    alert_dict["frames"] = []  # placeholder; real frames from future
    alert_dict["frame_future"] = frame_future

    # Run the alert through the full segmented pipeline
    # (stage functions imported above so tests can inspect them; dispatcher
    # imported at module top so tests can patch dispatcher.dispatch)
    try:
        result = _run_pipeline(alert_dict)
    except Exception:
        log.exception("pipeline.run failed for alert")
        return jsonify({"status": "error"}), 200

    alert_id = result.get("id", "unknown")
    chat_id = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    # US-050b: per-stage Telegram dispatch.
    # Each TG-producing stage (TG#1, TG#2, TG#3) is dispatched
    # immediately after _run_pipeline() returns. A failure at any
    # stage N+1 logs.exception and returns HTTP 200 (no retry storm)
    # but does NOT continue to subsequent stages. Earlier-sent TGs
    # are NOT rolled back. No defensive fallbacks.
    #
    # record_hit fires BEFORE TG#1 dispatch (US-091, 2026-09-27).
    # Cooldown now suppresses the NEXT alert before the current alert's TG
    # messages finish sending. This is the ONLY call site for record_hit
    # in the production tree.

    # ---- record_hit (cooldown window starts) BEFORE TG#1 dispatch ----
    try:
        infra_pipeline_cooldown.record_hit(
            alert_dict["camera_id"],
            result.get("classification", "unknown"),
            time.monotonic(),
        )
    except Exception:
        log.exception("record_hit failed for alert_id=%s", alert_id)

    # ---- TG#1 dispatch (after stage 10: build_tg1) ----
    try:
        _send_tg1(result.get("tg1"), alert_id, chat_id, bot_token,
                  alert_dict["camera_id"])
    except Exception:
        log.exception("TG#1 dispatch failed for alert_id=%s", alert_id)
        return jsonify(result), 200

    # ---- TG#2 dispatch (after stage 11: build_tg2) ----
    try:
        _send_tg2(result.get("tg2"), alert_id, chat_id, bot_token,
                  alert_dict["camera_id"])
    except Exception:
        log.exception("TG#2 dispatch failed for alert_id=%s", alert_id)
        return jsonify(result), 200

    # ---- TG#3 dispatch (after stage 13: build_tg3) ----
    try:
        _send_tg3(result.get("tg3"), alert_id, chat_id, bot_token,
                  alert_dict["camera_id"])
    except Exception:
        log.exception("TG#3 dispatch failed for alert_id=%s", alert_id)
        return jsonify(result), 200

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


def _quiet_hours_suppress_log(alert_id: str, camera_id: str, tg_slot: str) -> None:
    """Emit a single structured log line for quiet-hours suppression."""
    log.info(
        "quiet-hours: alert_id=%s camera=%s slot=%s suppressed",
        alert_id, camera_id, tg_slot,
    )


def _convert_photos(alert_id: str, photo_paths: list[str]) -> list[str]:
    """Pre-convert source photo paths to cached JPEGs.

    Returns the list of cached JPEG paths. Empty if none convert.
    """
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    msg_cache_dir = str(Path(infra_paths.TG_UPLOADS_DIR) / date_str / alert_id)
    converted: list[str] = []
    for src_path in photo_paths:
        jpg = codec.encode_jpeg(src_path, msg_cache_dir, jpeg_quality=88, max_dim=1920)
        if jpg is not None:
            converted.append(jpg)
    return converted


def _send_tg1(tg_msg: dict | None, alert_id: str, chat_id: str, bot_token: str,
              camera_id: str = "") -> None:
    """Dispatch TG#1: alert message with photos."""
    # Quiet-hours gate: suppress during night for scoped cameras.
    if camera_id and in_quiet_hours(datetime.now(UTC), camera_id):
        _quiet_hours_suppress_log(alert_id, camera_id, "TG#1")
        return
    if not tg_msg or not bot_token:
        if tg_msg and not bot_token:
            log.warning(
                "tg-send: alert_id=%s TG#1 missing TELEGRAM_BOT_TOKEN, skipping",
                alert_id,
            )
        return
    orig_photos = tg_msg.get("photos") or []
    if orig_photos:
        tg_msg["photos"] = _convert_photos(alert_id, orig_photos)
    try:
        responses = dispatcher.dispatch(
            [tg_msg],
            bot_token=bot_token,
            chat_id=chat_id,
        )
        converted_photos = tg_msg.get("photos") or []
        upload_bytes = (
            sum(os.path.getsize(p) for p in converted_photos) if converted_photos else 0
        )
        method = "sendMediaGroup" if converted_photos else "sendMessage"
        log.info(
            "tg-send: alert_id=%s TG#1 chat=%s method=%s sources=%d converted=%d upload=%d response=HTTP %d",
            alert_id,
            chat_id,
            method,
            len(orig_photos),
            len(converted_photos),
            upload_bytes,
            responses[0].status_code if responses else 0,
        )
    except (ConfigError, DeliveryError) as exc:
        log.error(
            "tg-send: alert_id=%s TG#1 error=%s: %s", alert_id, type(exc).__name__, exc
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "tg-send: alert_id=%s TG#1 unexpected %s: %s",
            alert_id,
            type(exc).__name__,
            exc,
        )


def _send_tg2(tg_msg: dict | None, alert_id: str, chat_id: str, bot_token: str,
              camera_id: str = "") -> None:
    """Dispatch TG#2: detail message with photos."""
    # Quiet-hours gate: suppress during night for scoped cameras.
    if camera_id and in_quiet_hours(datetime.now(UTC), camera_id):
        _quiet_hours_suppress_log(alert_id, camera_id, "TG#2")
        return
    if not tg_msg or not bot_token:
        if tg_msg and not bot_token:
            log.warning(
                "tg-send: alert_id=%s TG#2 missing TELEGRAM_BOT_TOKEN, skipping",
                alert_id,
            )
        return
    orig_photos = tg_msg.get("photos") or []
    if orig_photos:
        tg_msg["photos"] = _convert_photos(alert_id, orig_photos)
    try:
        responses = dispatcher.dispatch(
            [tg_msg],
            bot_token=bot_token,
            chat_id=chat_id,
        )
        converted_photos = tg_msg.get("photos") or []
        upload_bytes = (
            sum(os.path.getsize(p) for p in converted_photos) if converted_photos else 0
        )
        method = "sendMediaGroup" if converted_photos else "sendMessage"
        log.info(
            "tg-send: alert_id=%s TG#2 chat=%s method=%s sources=%d converted=%d upload=%d response=HTTP %d",
            alert_id,
            chat_id,
            method,
            len(orig_photos),
            len(converted_photos),
            upload_bytes,
            responses[0].status_code if responses else 0,
        )
    except (ConfigError, DeliveryError) as exc:
        log.error(
            "tg-send: alert_id=%s TG#2 error=%s: %s", alert_id, type(exc).__name__, exc
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "tg-send: alert_id=%s TG#2 unexpected %s: %s",
            alert_id,
            type(exc).__name__,
            exc,
        )


def _send_tg3(tg_msg: dict | None, alert_id: str, chat_id: str, bot_token: str,
              camera_id: str = "") -> None:
    """Dispatch TG#3: match message (text-only)."""
    # Quiet-hours gate: suppress during night for scoped cameras.
    if camera_id and in_quiet_hours(datetime.now(UTC), camera_id):
        _quiet_hours_suppress_log(alert_id, camera_id, "TG#3")
        return
    if not tg_msg or not bot_token:
        if tg_msg and not bot_token:
            log.warning(
                "tg-send: alert_id=%s TG#3 missing TELEGRAM_BOT_TOKEN, skipping",
                alert_id,
            )
        return
    # TG#3 is text-only (photos=[]), no conversion needed.
    try:
        responses = dispatcher.dispatch(
            [tg_msg],
            bot_token=bot_token,
            chat_id=chat_id,
        )
        converted_photos = tg_msg.get("photos") or []
        upload_bytes = (
            sum(os.path.getsize(p) for p in converted_photos) if converted_photos else 0
        )
        method = "sendMediaGroup" if converted_photos else "sendMessage"
        log.info(
            "tg-send: alert_id=%s TG#3 chat=%s method=%s sources=%d converted=%d upload=%d response=HTTP %d",
            alert_id,
            chat_id,
            method,
            0,
            len(converted_photos),
            upload_bytes,
            responses[0].status_code if responses else 0,
        )
    except (ConfigError, DeliveryError) as exc:
        log.error(
            "tg-send: alert_id=%s TG#3 error=%s: %s", alert_id, type(exc).__name__, exc
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "tg-send: alert_id=%s TG#3 unexpected %s: %s",
            alert_id,
            type(exc).__name__,
            exc,
        )


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


def _cleanup_loop(interval_min: int) -> None:
    """Periodically call ``infra.cleanup.run()`` every ``interval_min`` minutes.

    Runs in a daemon thread started at daemon boot.  Skips immediately if
    ``interval_min`` is 0 (the caller checks this too).
    """
    import infra.cleanup as _cleanup

    interval_s = interval_min * 60
    while True:
        try:
            result = _cleanup.run()
            log.info(
                "cleanup run: dirs=%d freed=%d errors=%d",
                result["deleted_dirs"],
                result["freed_bytes"],
                result["errors"],
            )
        except Exception:
            log.exception("cleanup loop: run() raised")
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# MCP server spawn (US-068g1)
# ---------------------------------------------------------------------------


def _mcp_server_thread_target() -> None:
    """Thread target: call infra.mcp_server.run() with error logging.

    Wraps run() so that any exception (including sys.exit/SystemExit
    from token validation failure) is caught and logged at ERROR level
    rather than silently killing the thread.
    """
    import infra.mcp_server as _mcp

    try:
        _mcp.run()
    except SystemExit as exc:
        log.error("mcp_server: run() SystemExit=%s", exc.code)
    except Exception:
        log.exception("mcp_server: run() raised")


def _start_mcp_server() -> None:
    """Spawn infra.mcp_server.run() on a daemon thread when enabled.

    Reads FARMSURV_MCP_ENABLED (1=ON, 0=OFF, default 0). When enabled,
    starts the MCP server on a daemon thread and logs spawn info.
    When disabled, logs a disabled message.

    The MCP server's own run() handles token validation and abort-on-error.
    We catch any failure and log it (never swallow silently).
    """
    mcp_enabled = os.environ.get("FARMSURV_MCP_ENABLED", "0") == "1"
    mcp_port = int(os.environ.get("FARMSURV_MCP_PORT", "8091"))
    mcp_bind = os.environ.get("FARMSURV_MCP_BIND", "tailnet-only")

    if not mcp_enabled:
        log.info(
            "{event: 'mcp_server_disabled', reason: 'FARMSURV_MCP_ENABLED!=1'}"
        )
        return

    token = os.environ.get("FARMSURV_MCP_TOKEN", "")

    thread = threading.Thread(
        target=_mcp_server_thread_target,
        name="mcp_server",
        daemon=True,
    )
    thread.start()

    log.info(
        "{event: 'mcp_server_spawned', port=%d, bind_address=%s, far_msurv_mcp_token_len=%d}",
        mcp_port,
        mcp_bind,
        len(token),
    )


# ---------------------------------------------------------------------------
# API server spawn (US-072c)
# ---------------------------------------------------------------------------


def _api_server_thread_target() -> None:
    """Thread target: call infra.api_server.run() with error logging.

    Wraps run() so that any exception (including sys.exit/SystemExit
    from token validation failure) is caught and logged at ERROR level
    rather than silently killing the thread.
    """
    import infra.api_server as _api

    try:
        _api.run()
    except SystemExit as exc:
        log.error("api_server: run() SystemExit=%s", exc.code)
    except Exception:
        log.exception("api_server: run() raised")


def _start_api_server() -> None:
    """Spawn infra.api_server.run() on a daemon thread when enabled.

    Reads FARMSURV_API_ENABLED (1=ON, 0=OFF, default 0). When enabled,
    starts the API server on a daemon thread and logs spawn info.
    When disabled, logs a disabled message.

    The API server's own run() handles token validation and abort-on-error.
    We catch any failure and log it (never swallow silently).
    """
    api_enabled = os.environ.get("FARMSURV_API_ENABLED", "0") == "1"
    api_port = int(os.environ.get("FARMSURV_API_PORT", "8092"))
    api_bind = os.environ.get("FARMSURV_API_BIND", "tailnet-only")

    if not api_enabled:
        log.info(
            "{event: 'api_server_disabled', reason: 'FARMSURV_API_ENABLED!=1'}"
        )
        return

    token = os.environ.get("FARMSURV_API_TOKEN", "")

    thread = threading.Thread(
        target=_api_server_thread_target,
        name="api-server",
        daemon=True,
    )
    thread.start()

    log.info(
        "{event: 'api_server_spawned', port=%d, bind_address=%s, api_token_len=%d}",
        api_port,
        api_bind,
        len(token),
    )


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
    # Env was already loaded at module-import time (before infra imports).
    # AC6: fail-loud for operator-significant Telegram credentials.
    # The listener cannot function without them; better to crash at startup
    # than fail silently on first alert send.
    _require_env("TELEGRAM_BOT_TOKEN")
    _require_env("TELEGRAM_HOME_CHAT_ID")

    # US-069-regression: log the night-suppress config now that env is loaded.
    # This proves the values were actually read (not frozen as [] at import).
    try:
        from infra.quick_classifier import _get_suppress_cameras, _get_suppress_classes

        log.info(
            "night_suppress: cameras=%s classes=%s",
            _get_suppress_cameras(),
            _get_suppress_classes(),
        )
    except Exception:  # noqa: BLE001 — non-fatal; gate still works without it
        log.warning("night_suppress: startup log failed (non-fatal)")

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

    # US-061c: start the hourly cleanup thread (non-blocking, daemon=True).
    import infra.paths as _p

    interval_min = _p.FARMSURV_CLEANUP_INTERVAL_MIN
    if interval_min > 0:
        cleanup_thread = threading.Thread(
            target=_cleanup_loop,
            name="cleanup",
            daemon=True,
            kwargs={"interval_min": interval_min},
        )
        cleanup_thread.start()
        log.info(
            "cleanup thread started; interval=%dm, high=%d bytes, low=%d bytes",
            interval_min,
            _p.FARMSURV_FRAME_EVICT_HIGH_BYTES,
            _p.FARMSURV_FRAME_EVICT_LOW_BYTES,
        )
    else:
        log.info("cleanup thread skipped; interval=0")

    # US-068g1: spawn the MCP server on a daemon thread when enabled.
    _start_mcp_server()

    # US-072c: spawn the API server on a daemon thread when enabled.
    _start_api_server()

    # US-073h: start the periodic health heartbeat thread.
    from listener.health import HealthReporter

    mcp_enabled = os.environ.get("FARMSURV_MCP_ENABLED", "0") == "1"
    api_enabled = os.environ.get("FARMSURV_API_ENABLED", "0") == "1"
    hr = HealthReporter(
        port=port,
        env_total=_ENV_TOTAL_LOADED,
        plist_keepalive=True,
        api_enabled=api_enabled,
    )
    hr.start(mcp_enabled=mcp_enabled)

    app.run(host=host, port=port)


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
        utc_iso,
        tz_name=os.environ.get("DISPLAY_TZ"),
    )
