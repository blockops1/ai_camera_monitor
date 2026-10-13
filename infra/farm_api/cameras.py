"""farm_api.cameras — Camera resource functions.

STATUS: alpha — V2-068b1
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - camera-creds.env (via infra.camera_creds)
    - data/frames/<camera>/ (filesystem)
    - infra/paths.py for FRAMES_DIR, DATA_DIR

OUTPUTS:
    - CameraInfo, CameraHealth dataclasses
    - list[CameraInfo], CameraHealth | None, Path | None

DOES NOT DO:
    - Modify camera state (write tools in V2-069)
    - Fetch live snapshots (read last frame from disk only)
    - Connect to RTSP (URL is redacted in output)

REDACTION:
    - IP addresses in rtsp_url and ip fields are replaced with [REDACTED-LAN-IP]
      matching pattern 192.168.x.x

CALLERS:
    - infra.mcp_server (MCP tool adapters)
    - Any code that needs a read-only camera inventory
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from infra.camera_creds import get_all_cameras, get_camera
from infra.paths import FRAMES_DIR

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Match IPv4 addresses in the 192.168.x.x range (private LAN).
# Used to redact IPs in RTSP URLs and standalone IP fields.
_LAN_IP_RE = re.compile(r"192\.168\.\d{1,3}\.\d{1,3}")

REDACTED = "[REDACTED-LAN-IP]"


def _redact_ip(value: str) -> str:
    """Replace all 192.168.x.x IP addresses with [REDACTED-LAN-IP]."""
    return _LAN_IP_RE.sub(REDACTED, value)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CameraInfo:
    """Read-only snapshot of a single camera's identity.

    Attributes:
        camera_id: Uppercase prefix, e.g. "FRONT", "BACK".
        display_name: Human-readable name, e.g. "Front Door Outside".
        model: Camera model string (empty if not known).
        rtsp_url_redacted: RTSP URL with LAN IP replaced.
        ip_redacted: Camera IP address, redacted.
    """
    camera_id: str
    display_name: str
    model: str
    rtsp_url_redacted: str
    ip_redacted: str


@dataclass(frozen=True)
class CameraHealth:
    """Read-only snapshot of a single camera's operational health.

    Attributes:
        camera_id: Uppercase prefix, e.g. "FRONT".
        online: Whether the camera is reachable.
        last_frame_at: ISO8601 timestamp of the latest frame, or None.
        rtsp_errors_last_24h: Count of RTSP errors in the last 24 hours.
    """
    camera_id: str
    online: bool
    last_frame_at: str | None
    rtsp_errors_last_24h: int


# ---------------------------------------------------------------------------
# Model lookup — derive from camera metadata if available
# ---------------------------------------------------------------------------

# Known model mapping by IP prefix (from camera-creds.env comments).
# This is a best-effort mapping; the creds file does not carry a model field.
_MODEL_BY_IP = {
    "192.168.1.73": "Reolink RLC-510A",
    "192.168.1.108": "Reolink RLC-510A",
    "192.168.1.103": "Reolink RLC-510A",
    "192.168.1.113": "Reolink RLC-510A",
}


def _camera_model(camera_id: str) -> str:
    """Return the model string for a camera, or empty string if unknown."""
    cam = get_camera(camera_id)
    if cam is None:
        return ""
    ip = cam.get("ip", "")
    return _MODEL_BY_IP.get(ip, "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_cameras() -> list[CameraInfo]:
    """Return a list of CameraInfo for every known camera.

    Reads camera-creds.env via infra.camera_creds. IP addresses in the
    returned objects are redacted (192.168.x.x -> [REDACTED-LAN-IP]).

    Returns:
        A list of CameraInfo, one per camera in camera-creds.env.
        Returns an empty list if the creds file is missing.
    """
    all_cams = get_all_cameras()
    result: list[CameraInfo] = []
    for name, info in all_cams.items():
        # Derive camera_id from the prefix field set by camera_creds
        camera_id = info.get("prefix", name.upper())
        rtsp = info.get("rtsp_url", "")
        ip = info.get("ip", "")
        model = _camera_model(camera_id)

        result.append(CameraInfo(
            camera_id=camera_id,
            display_name=name,
            model=model,
            rtsp_url_redacted=_redact_ip(rtsp),
            ip_redacted=_redact_ip(ip),
        ))
    return result


def get_camera_health(camera_id: str) -> CameraHealth | None:
    """Return health data for a single camera.

    This is a stub implementation (refined in US-068b6). Currently reads
    from the filesystem to determine online status and last frame time.
    RTSP error count is always 0 until listener state integration.

    Args:
        camera_id: Uppercase prefix, e.g. "FRONT".

    Returns:
        CameraHealth if the camera is known, None otherwise.
    """
    cam = get_camera(camera_id)
    if cam is None:
        return None

    camera_prefix = cam.get("prefix", camera_id)
    frames_dir = Path(FRAMES_DIR) / camera_prefix

    last_frame_at = None
    online = False

    if frames_dir.is_dir():
        # Find the newest frame file (deepest path = alert_id dir)
        for alert_dir in sorted(frames_dir.iterdir(), reverse=True):
            if not alert_dir.is_dir():
                continue
            files = list(alert_dir.iterdir())
            if files:
                newest = max(files, key=lambda f: f.stat().st_mtime)
                last_frame_at = datetime.fromtimestamp(
                    newest.stat().st_mtime, tz=UTC
                ).isoformat()
                online = True
                break

    return CameraHealth(
        camera_id=camera_id,
        online=online,
        last_frame_at=last_frame_at,
        rtsp_errors_last_24h=0,
    )


def get_camera_snapshot(camera_id: str) -> Path | None:
    """Return the path to the latest frame for a camera.

    Reads from data/frames/<camera_prefix>/ — finds the newest alert
    directory, then the newest frame file within it.

    Args:
        camera_id: Uppercase prefix, e.g. "FRONT".

    Returns:
        Path to the latest frame PNG, or None if no frames exist.
    """
    cam = get_camera(camera_id)
    if cam is None:
        return None

    camera_prefix = cam.get("prefix", camera_id)
    frames_dir = Path(FRAMES_DIR) / camera_prefix

    if not frames_dir.is_dir():
        return None

    # Find the newest alert directory (deepest = most recent)
    for alert_dir in sorted(frames_dir.iterdir(), reverse=True):
        if not alert_dir.is_dir():
            continue
        # Find the newest frame file within this alert
        png_files = [f for f in alert_dir.iterdir() if f.suffix == ".png"]
        if png_files:
            return max(png_files, key=lambda f: f.stat().st_mtime)

    return None
