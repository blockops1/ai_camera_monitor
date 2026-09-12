"""
camera_creds.py — Parse camera-creds.env, resolve RTSP URLs, anti-spoof IP validation.

STATUS: stable
THREAD SAFETY: single-threaded (parsed at import time, cached in module dict)

INPUTS:
    - file camera-creds.env (required) — parsed from PROJECT_ROOT or
      FARM_CAMERA_CREDS_FILE env var override
    - function arg camera_id: str (required) — the env-var prefix
      of the camera (e.g. "CAMERA_A")
    - function arg request_ip: str (required) — source IP to validate

OUTPUTS:
    - return value: dict | None — camera info {name, ip, user, pass, rtsp_url}
    - return value: bool — True if request_ip matches the camera's registered IP

PUBLIC API:
    _parse_env(env_path: str) -> dict
        Parse camera-creds.env into a dict keyed by uppercase env-var prefix.
        Each entry: {name, ip, user, pass, rtsp_url, http_user, http_pass}.
        The "name" comes from _CAMERA_MAP if defined, else falls back to the
        uppercased env-var prefix.
    get_camera(camera_id: str) -> dict | None
        Look up a single camera by uppercase ID. Returns None if not found.
    get_all_cameras() -> dict
        Return the full parsed camera dict.
    validate_source_ip(camera_id: str, request_ip: str) -> bool
        Return True if request_ip matches the camera's registered IP.

DOES NOT DO:
    - RTSP frame capture — that lives in infra/frame_capture.py
    - Telegram alert sending — that lives in listener/daemon.py
    - HTTP snapshot fetching — no REST client code
    - Camera configuration (PTZ, recording profiles)

WHY HERE:
    Separated from v1's frame_capture.py (which carried the parser as
    load_camera_creds) to give the webhook receiver (daemon.py) its own
    import for IP validation before accepting alert payloads.

CALLED BY:
    - listener.daemon: validate_source_ip() in the /alert handler
    - infra.frame_capture: get_rtsp_url() for RTSP URL construction

CALLS INTO:
    - infra.paths: CAMERA_CREDS_FILE, PROJECT_ROOT
    - os.path.exists, open — file I/O
"""

import os

from infra.paths import CAMERA_CREDS_FILE

# --------------------------------------------------------------------------
# Camera name mapping: env prefix -> human-readable name
# --------------------------------------------------------------------------
_CAMERA_MAP = {}
# Operators populate this with their own site-specific camera
# name mapping: env-var prefix -> human-readable display name.
# Example:
#   _CAMERA_MAP = {
#       "front_gate": "Front Gate",
#       "driveway":   "Driveway",
#       "back_yard":  "Back Yard",
#   }
# If you leave it empty, the pipeline falls back to the env-var
# prefix itself as the display name.


def _extract_ip_from_rtsp(url: str) -> str | None:
    """Extract the IP host from an RTSP URL.

    Handles passwords with @ (URL-encoded %40 or literal).
    Returns None if the URL is malformed.
    """
    try:
        after_scheme = url.split("://", 1)[1]
        auth_and_host = after_scheme.split("/", 1)[0]
        at_idx = auth_and_host.rfind("@")
        if at_idx < 0:
            return None
        host_port = auth_and_host[at_idx + 1:]
        if host_port.startswith("["):
            close = host_port.find("]")
            if close < 0:
                return None
            return host_port[1:close]
        colon = host_port.rfind(":")
        return host_port[:colon] if colon > 0 else host_port
    except (IndexError, ValueError):
        return None


def _parse_env(env_path: str) -> dict:
    """Parse camera-creds.env and return a dict keyed by uppercase prefix.

    Example dict:
        {
            "CAMERA_A": {
                "name": "CAMERA_A",
                "ip": "192.0.2.1",
                "user": "admin",
                "pass": "...",
                "rtsp_url": "rtsp://...",
                "http_user": "admin",
                "http_pass": "...",
            },
            ...
        }
    """
    result = {}

    if not os.path.exists(env_path):
        return result

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower()
            value = value.strip()

            for prefix, camera_name in _CAMERA_MAP.items():
                if key == f"{prefix}_rtsp_url":
                    entry = result.setdefault(camera_name, {})
                    entry["name"] = camera_name
                    entry["rtsp_url"] = value
                    entry["prefix"] = prefix.upper()
                    ip = _extract_ip_from_rtsp(value)
                    if ip:
                        entry["ip"] = ip
                elif key == f"{prefix}_ip" and value:
                    entry = result.setdefault(camera_name, {})
                    entry["name"] = camera_name
                    entry["ip"] = value
                elif key == f"{prefix}_user":
                    result.setdefault(camera_name, {})["user"] = value
                elif key == f"{prefix}_pass":
                    result.setdefault(camera_name, {})["pass"] = value
                elif key == f"{prefix}_http_user":
                    result.setdefault(camera_name, {})["http_user"] = value
                elif key == f"{prefix}_http_pass":
                    result.setdefault(camera_name, {})["http_pass"] = value

    return result


# --------------------------------------------------------------------------
# Module-level cache — parsed once, shared by all callers
# --------------------------------------------------------------------------
_camera_creds_path = os.environ.get("FARM_CAMERA_CREDS_FILE", CAMERA_CREDS_FILE)
_all_cameras = _parse_env(_camera_creds_path)


def get_all_cameras() -> dict:
    """Return the full parsed camera dict keyed by camera name."""
    return _all_cameras


def get_camera(camera_id: str) -> dict | None:
    """Look up a single camera by uppercase env-var prefix.

    Args:
        camera_id: Uppercase env-var prefix, e.g. "CAMERA_A".

    Returns:
        Camera dict {name, ip, user, pass, rtsp_url, ...} or None.
    """
    camera_id_upper = camera_id.upper()
    for info in _all_cameras.values():
        if info.get("prefix") == camera_id_upper:
            return info
    return None


def validate_source_ip(camera_id: str, request_ip: str) -> bool:
    """Check if request_ip matches the camera's registered IP.

    Used by the /alert handler to reject spoofed webhook sources.

    Args:
        camera_id: Uppercase camera ID prefix.
        request_ip: The source IP from the incoming request.

    Returns:
        True if the IP matches, False otherwise (including camera not found).
    """
    cam = get_camera(camera_id)
    if cam is None:
        return False
    return cam.get("ip") == request_ip
