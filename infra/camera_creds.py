"""
camera_creds.py — Backwards-compat thin wrapper around infra.cameras.

STATUS: stable
THREAD SAFETY: thread-safe (delegates to infra.cameras, which is pure
    parser — reads file at call time, no shared state).

INPUTS:
    - file camera-creds.env (path passed as env_path argument or
      resolved via infra.paths.CAMERA_CREDS_FILE)
    - function arg env_path: str | None — absolute path; None means
      use the standard default-location lookup chain.

OUTPUTS:
    - return value: dict[str, dict[str, str]] (load_camera_creds) —
      keyed by canonical camera name; each entry has {"rtsp_url": str,
      "ip": str (optional)}. Empty dict if env_path does not exist.
    - return value: str | None (get_http_user / get_http_password) —
      HTTP cred for the camera at the given IP, or None if the IP is
      not in the env file.
    - side effect: see infra.cameras (delegated). IP-rotation warning
      prints to stdout when _IP and _RTSP_URL host disagree.

PUBLIC API:
    load_camera_creds(env_path: str) -> dict[str, dict[str, str]]
        Parse camera-creds.env and return a dict keyed by camera name.
        LEGACY format (Phase.166 §11.87.0):
            FRONT_RTSP_URL=rtsp://user:pass@10.x.x.x:554/...
            BACK_RTSP_URL=rtsp://user:pass@10.x.x.x:554/...
            FRONT_IP=10.x.x.x
        NEW format (Phase.167 §13.2):
            CAM1_IP=10.x.x.x
            CAM1_RTSP_URL=rtsp://user:pass@10.x.x.x:554/...
            etc.
        Returns:
            {
              "<FRIENDLY_NAME>":  {"rtsp_url": "...", "ip": "..."},
              "<FRIENDLY_NAME>":    {"rtsp_url": "...", "ip": "..."},
            }
    get_http_user(ip, env_path=None) -> str | None
        HTTP username for the camera at `ip`. Returns "admin" as a
        default if no _HTTP_USER is set. Returns None if `ip` is not
        in the env file. Phase.166 §11.87.2.
    get_http_password(ip, env_path=None) -> str | None
        HTTP password for the camera at `ip`. Returns None if `ip` is
        not in the env file or the password field is missing/empty.
        Phase.166 §11.87.2.

DOES NOT DO:
    - Open an RTSP connection → infra.frame_capture (uses persistent_rtsp)
    - Resolve alias names → infra.camera_aliases (caller resolves first)
    - Decrypt or store secrets — values are read directly from the env file
    - Validate RTSP reachability — that's capture-time responsibility
    - Re-implement env parsing — delegates to infra.cameras (Phase.167 §13.4)

WHY HERE:
    Per AGENTS.md §4 (one module = one job). Originally extracted from
    frame_capture.py in Part 9 step 6. Phase.167 §13.4 turned the
    actual parsing into infra.cameras (the new code); this module now
    serves as the backwards-compat shim for callers that still expect
    the dict-shaped return value of load_camera_creds() and the
    IP→cred lookup helpers. When all callers migrate to infra.cameras,
    this module's body collapses to nothing.

    Sanity check (IP-rotation detection): lives in infra.cameras now —
    it emits the same warning this module used to emit, every caller
    of load_cameras() gets it for free.

    Module-level camera_map was removed in Phase.167 §13.4 — the
    operator-flavored prefix→name map now lives in infra.cameras._LEGACY
    _PREFIX_TO_NAME, and the public-release pipeline scrubs the
    operator-flavored block (matching the v0.1.2 release convention).

CALLED BY:
    - listener.listener: load_camera_creds() at bootstrap (deprecated;
      migrate to infra.cameras.load_cameras)
    - scripts/cam_browser.py: get_http_user() / get_http_password() at
      CLI login (Phase.166 §11.87.2)

CALLS INTO:
    - infra.cameras: the real parser. THIS MODULE IS A WRAPPER.
    - stdlib os.path: only used for backward-compat env_path resolution
    - stdlib open(): no direct file I/O — delegates to infra.cameras

RELATED:
    - camera-creds.env: the file these helpers parse
    - infra.cameras: the actual parser (Phase.167 §13.4 — Commit 5)
    - infra.camera_aliases: caller should resolve names via this module
      BEFORE looking up creds (CAMERA_NAME_ALIASES → canonical → creds)
    - infra.persistent_rtsp: uses the parsed RTSP URLs to maintain
      per-camera persistent connections
    - scripts/cam_browser.py: IP→cred lookup at login
"""

# Phase.167 §13.4 — Commit 6: this module is now a thin wrapper.
# All env-file parsing lives in infra.cameras. The functions below
# preserve the legacy dict-shaped return value of load_camera_creds()
# so existing callers (listener bootstrap, scripts/cam_browser.py,
# any T1-T5 test that asserts the dict shape) continue to work
# without modification.

from infra import cameras as _cameras


def load_camera_creds(env_path: str) -> dict:
    """Parse camera-creds.env and return a dict keyed by camera name.

    Backwards-compat shim over infra.cameras.load_cameras. Same env
    format, same {name: {"rtsp_url": ..., "ip": ...}} return shape.

    Args:
        env_path: absolute path to camera-creds.env

    Returns:
        {canonical_name: {"rtsp_url": str, "ip": str}, ...}
        Empty dict if env_path does not exist.
    """
    specs = _cameras.load_cameras(env_path)
    result: dict[str, dict[str, str]] = {}
    for s in specs:
        entry: dict[str, str] = {}
        if s.rtsp_url:
            # Match pre-Phase.167 behavior: only include the key when
            # the env file actually set _RTSP_URL. The listener checks
            # `if "rtsp_url" in info` before using it; an empty string
            # there is a behavioral change.
            entry["rtsp_url"] = s.rtsp_url
        entry["ip"] = s.ip
        result[s.name] = entry
    return result


def get_http_user(ip: str, env_path: str | None = None) -> str | None:
    """HTTP username for the camera at `ip`, or None.

    Backwards-compat shim over infra.cameras.by_ip. Returns "admin" as
    a default when the IP matches a camera with no explicit _HTTP_USER.
    Returns None only when the IP is not in the env file at all.

    Args:
        ip: camera IP (exact string match)
        env_path: optional absolute path override; None uses the
            resolution chain via infra.cameras (env var →
            infra.paths.CAMERA_CREDS_FILE).

    Raises:
        KeyError: not raised by this function — unknown IP returns None
            to preserve the original contract. (infra.cameras.by_ip
            raises KeyError on unknown IP; this shim catches it.)

    Note:
        Infra.cameras' "admin" default kicks in at the parser level, so
        this shim just returns spec.http_user — same behavior as before.
    """
    try:
        spec = _cameras.by_ip(ip, env_path)
    except KeyError:
        return None
    return spec.http_user or "admin"


def get_http_password(ip: str, env_path: str | None = None) -> str | None:
    """HTTP password for the camera at `ip`, or None.

    Backwards-compat shim over infra.cameras.by_ip. Returns None when
    the IP is not in the env file OR the password field is missing/empty.

    Args:
        ip: camera IP (exact string match)
        env_path: optional absolute path override; None uses the
            resolution chain via infra.cameras (env var →
            infra.paths.CAMERA_CREDS_FILE).

    Raises:
        KeyError: not raised by this function — unknown IP returns None.

    Security:
        Caller must NOT log the returned value. Use this in scripts
        that pass the password straight into CamBrowser.login(ip, user, password).
    """
    try:
        spec = _cameras.by_ip(ip, env_path)
    except KeyError:
        return None
    return spec.http_pass or None
