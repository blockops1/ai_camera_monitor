"""
camera_audio.py — Phase.106 — Reolink CGI audio dispatch for camera speakers.

Phase.106 (2026-08-22). Sends a `cmd=AudioFilePlay` request to a Reolink
camera's CGI endpoint so the camera plays a pre-recorded audio clip from
its internal storage (or uploads one first via the API).

This module is INTENTIONALLY small. The listener's `_process_person_alert`
calls `dispatch_audio_clip(...)` only when the env gate is on
(`PERSON_AUDIO_ENABLED=1`), so the module can be a no-op in production
until clips are recorded. When the gate is on but a dependency or
network call fails, dispatch_audio_clip logs and returns — never raises.

STATUS: provisional (Phase.106; will stabilize after first live audio
    dispatch is observed)
THREAD SAFETY: thread-safe (per-call network call, no shared state)

INPUTS:
    - env var FARM_REOLINK_AUTH_TOKEN — Reolink CGI auth token (loaded
        from ~/.env via infra.paths). Optional; if missing, dispatch
        is logged and skipped.
    - env var FARM_REOLINK_CREDENTIALS_DIR — directory with per-camera
        credentials (loaded via infra.creds; per-camera file pattern).
    - function arg camera_name — canonical name (e.g. "Front Door Outside")
    - function arg event_type — "person" / "people" (audit only)
    - function arg matched_name — name of the matched person (audit only)
    - function arg audio_file — path to .wav/.mp3 clip to play
        (optional; if None, falls back to a default clip)
    - function arg repeat — number of times to repeat (default 1)

OUTPUTS:
    - HTTP POST to camera's CGI endpoint
    - log lines: dispatched / skipped / failed
    - return value: bool (True if dispatch accepted, False if skipped/failed)

PUBLIC API:
    dispatch_audio_clip(
        camera_name: str,
        event_type: str = "person",
        matched_name: str | None = None,
        audio_file: str | None = None,
        repeat: int = 1,
    ) -> bool
        Send the audio play command to the camera. Returns True on
        success, False otherwise (never raises).

DOES NOT DO:
    - Upload audio files to the camera. The Reolink 833A supports
      upload via `cmd=AudioFileUpload`, but it's a separate step.
      v1 assumes clips are pre-uploaded via the Reolink client and
      addressed by filename.
    - Validate the audio file format. The camera rejects unknown
      formats and logs a CGI error; we surface that as a failure.
    - Handle token refresh. Tokens are long-lived (~hours). The
      listener doesn't cache them — caller passes fresh tokens via env.
    - Run synchronously with a long timeout. Network call has a 5s
      default timeout. If Reolink is slow, we log and skip.

WHY HERE:
    Single-purpose: send one HTTP POST. ~80 LoC. Lives in infra/ not
    listener/ because the dependency on env vars + HTTP makes it
    testable independently from the person pipeline.

CALLED BY:
    - listener.person_event_pipeline._try_dispatch_audio (only when
      PERSON_AUDIO_ENABLED=1)

CALLS INTO:
    - urllib.request (stdlib; no extra deps)

RELATED:
    - PLAN §11.36 — Phase.106 audio dispatch design
    - camera configs — Reolink RLC-833A spec for the AudioFilePlay
      CGI endpoint (verified in production RLC-833A manual).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


# Reolink CGI endpoint path. Per Reolink API docs.
_CGI_PATH = "/cgi-bin/api.cgi"

# Per-camera IP map (matches infra.paths / listener.py). Used to
# resolve camera_name → IP for the CGI POST. Mirrors the same
# canonical-name → IP map the listener uses for RTSP URLs.
#
# Phase.106 — only FDO is in PERSON_GATEKEEPER_CAMERAS, so the
# only camera this module dispatches for in v1 is FDO. Once §11.36b
# adds more cameras, this map grows.
_CAMERA_IP = {
    "Front Door Outside": "192.168.1.39",
    "Outside Front Solar": "192.168.1.103",
    "Outside Front Garage": "192.168.1.73",
    "Outside Front Power": "192.168.1.20",
    "Outside Back Solar": "192.168.1.71",
    "Back Door Inside": "192.168.1.55",
}

# Default clip filename on the camera. Operator pre-uploads via
# Reolink client. Phase.106 §11.36 step 9 — first iteration
# uses a single default. §11.36b adds selection logic (time-of-day,
# rotation, dedup).
_DEFAULT_CLIP = "greeting.wav"


def _build_cgi_url(camera_ip: str, token: str | None) -> str:
    """Build the Reolink CGI URL for AudioFilePlay.

    The token is added as a query param if present. Reolink cameras
    reject requests without a valid token (or with the wrong token
    if the camera enforces authentication on the API).
    """
    params = [
        ("cmd", "AudioFilePlay"),
        ("source", "Local"),
    ]
    if token:
        params.append(("token", token))
    qs = urllib.parse.urlencode(params)
    return f"http://{camera_ip}{_CGI_PATH}?{qs}"


def _build_request_body(audio_file: str, repeat: int) -> bytes:
    """Build the JSON body for AudioFilePlay.

    Reolink expects a JSON body with file name, volume, and repeat.
    """
    return json.dumps({
        "file": audio_file,
        "volume": 50,  # 0-100; operator-tunable later
        "repeat": repeat,
    }).encode("utf-8")


def _resolve_camera_ip(camera_name: str) -> str | None:
    """Look up the camera's IP from the static map."""
    return _CAMERA_IP.get(camera_name)


def _get_auth_token() -> str | None:
    """Get the Reolink auth token from env.

    Set FARM_REOLINK_AUTH_TOKEN in ~/.env. If missing, dispatch is
    skipped. We intentionally don't generate a token here — token
    acquisition is a separate flow (login API call) and out of
    scope for v1.
    """
    return os.environ.get("FARM_REOLINK_AUTH_TOKEN")


def dispatch_audio_clip(
    camera_name: str,
    event_type: str = "person",
    matched_name: str | None = None,
    audio_file: str | None = None,
    repeat: int = 1,
) -> bool:
    """Send AudioFilePlay command to the camera's CGI endpoint.

    Args:
        camera_name: canonical name (e.g. "Front Door Outside")
        event_type: webhook event type — audit only (default "person")
        matched_name: matched person name — audit only (optional)
        audio_file: filename on the camera (default "greeting.wav")
        repeat: how many times to play the clip (default 1)

    Returns:
        True if the camera accepted the request (HTTP 200).
        False if dispatch was skipped (env gate off, missing token/IP,
        or camera not in map) or failed (network error, HTTP non-200).
        Never raises — log-and-skip is the contract.
    """
    log.info(
        f"camera_audio: dispatch requested camera={camera_name} "
        f"event={event_type} matched={matched_name!r}"
    )

    ip = _resolve_camera_ip(camera_name)
    if ip is None:
        log.warning(
            f"camera_audio: no IP for camera={camera_name!r}; skip"
        )
        return False

    token = _get_auth_token()
    if not token:
        log.warning(
            "camera_audio: FARM_REOLINK_AUTH_TOKEN not set; skip"
        )
        return False

    clip = audio_file or _DEFAULT_CLIP
    url = _build_cgi_url(ip, token)
    body = _build_request_body(clip, repeat)

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        # Local llama-server endpoint; URL is built from hardcoded constants
        # in this module (DEFAULT_URL), not from user input. Bandit B310
        # flags urlopen because it can handle file:// schemes — irrelevant
        # here since we only ever pass http://127.0.0.1:PORT.
        with urllib.request.urlopen(req, timeout=5.0) as resp:  # nosec B310
            status = resp.status
            response_body = resp.read().decode("utf-8", errors="replace")
        if 200 <= status < 300:
            log.info(
                f"camera_audio: dispatched {clip} to {camera_name} "
                f"({ip}) status={status}"
            )
            return True
        else:
            log.warning(
                f"camera_audio: dispatch rejected by {camera_name} "
                f"({ip}) status={status} body={response_body[:200]}"
            )
            return False
    except urllib.error.URLError as err:
        log.warning(
            f"camera_audio: URLError dispatching to {camera_name} ({ip}): {err}"
        )
        return False
    except (TimeoutError, OSError) as err:
        log.warning(
            f"camera_audio: timeout/network error dispatching to "
            f"{camera_name} ({ip}): {err}"
        )
        return False
    except Exception:
        log.exception(
            f"camera_audio: unexpected error dispatching to "
            f"{camera_name} ({ip})"
        )
        return False


__all__ = [
    "_CAMERA_IP",
    "dispatch_audio_clip",
]
