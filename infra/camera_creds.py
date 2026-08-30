"""
camera_creds.py — Parse camera-creds.env into a structured dict.

STATUS: stable
THREAD SAFETY: thread-safe (pure parser; reads file at call time,
    no shared state)

INPUTS:
    - file camera-creds.env (path passed as env_path argument) —
      one line per camera with FRONT_RTSP_URL=, FRONT_IP=, etc.
      Format documented in the load_camera_creds() docstring below.
    - function arg env_path: str (required) — absolute path to the
      camera-creds.env file

OUTPUTS:
    - return value: dict[str, dict] — keyed by canonical camera name;
      each entry has {"rtsp_url": str, "ip": str (optional)}
    - return value is empty dict {} if env_path does not exist
    - side effect: prints a warning to stdout (not logging!) when IP
      rotation is detected (env _IP var disagrees with RTSP URL host)

PUBLIC API:
    load_camera_creds(env_path: str) -> dict
        Parse camera-creds.env and return a dict keyed by camera name.
        Format expected in env_path:
            FRONT_RTSP_URL=rtsp://user:pass@192.168.1.39:554/...
            BACK_RTSP_URL=rtsp://user:pass@192.168.1.85:554/...
            # Optional explicit override:
            FRONT_IP=192.168.1.39
    get_http_user(ip: str, env_path: str | None = None) -> str | None
        Look up the HTTP username for a camera by IP. Walks every env
        var ending in _HTTP_USER, finds the one whose sibling _IP matches
        `ip`. Returns "admin" as a default if no _HTTP_USER is set but a
        matching _IP entry exists. Returns None if the IP isn't in the
        env file at all. Phase.166 §11.87.2.
    get_http_password(ip: str, env_path: str | None = None) -> str | None
        Look up the HTTP password for a camera by IP. Same walk pattern
        as get_http_user but reads _HTTP_PASS. Returns None if the IP
        isn't in the env file or the password field is missing/empty.
        Phase.166 §11.87.2.

DOES NOT DO:
    - Open an RTSP connection → infra.frame_capture (uses persistent_rtsp)
    - Resolve alias names → infra.camera_aliases (caller resolves first)
    - Decrypt or store secrets — values are read directly from the env file
    - Validate RTSP reachability — that's capture-time responsibility

WHY HERE:
    Per AGENTS.md §4 (one module = one job), this was extracted from
    frame_capture.py in Part 9 step 6. The creds parser has nothing to
    do with frame capture — it just turns a dotenv-style file into a
    structured dict. The IP extraction logic handles Reolink RTSP URLs
    with @-encoded passwords correctly (anchor on host:port, walk back
    from last @).

    Sanity check: if a camera has both an RTSP-derived IP and an
    explicit _IP env var, they must agree. (2026-07-24 hardening —
    env file uses both, and IP rotation means the two can drift.) The
    warning prints to stdout because the listener hasn't set up logging
    at bootstrap when this runs.

    Module-level camera_map is hardcoded because the camera fleet is
    fixed as of 2026-07-29 cleanup. New cameras require editing this
    module (and adding to known_vehicles or camera registry).

CALLED BY:
    - listener.listener: load_camera_creds() at bootstrap
    - scripts/cam_browser.py: get_http_user() / get_http_password() at
      CLI login (Phase.166 §11.87.2)

CALLS INTO:
    - stdlib os.path: file existence check
    - stdlib open(): read env file
    - stdlib print(): IP-rotation warning (no logger at bootstrap)
    - infra.paths (lazy): CAMERA_CREDS_FILE default for the IP→cred
      helpers (Phase.166 §11.87.2)

RELATED:
    - camera-creds.env: the file this module parses
    - infra.camera_aliases: caller should resolve names via this module
      BEFORE looking up creds (CAMERA_NAME_ALIASES → canonical → creds)
    - infra.persistent_rtsp: uses the parsed RTSP URLs to maintain
      per-camera persistent connections
    - scripts/cam_browser.py: IP→cred lookup at login (Phase.166
      §11.87.2 — replaces the hardcoded FRONT_HTTP_PASS / BACK_HTTP_PASS
      branching)
"""

import os


def load_camera_creds(env_path: str) -> dict:
    """
    Parse camera-creds.env and return a dict keyed by camera name.

    Expected format in env_path (one line per camera):
        FRONT_RTSP_URL=rtsp://user:pass@192.168.1.39:554/...
        BACK_RTSP_URL=rtsp://user:pass@192.168.1.85:554/...
        # Optional explicit override:
        FRONT_IP=192.168.1.39

    Returns:
        {
            "Front Corner Inside": {"rtsp_url": "...", "ip": "..."},
            "Back Door Inside":   {"rtsp_url": "...", "ip": "..."},
        }
    """
    camera_map = {
        "front": "Front Door Outside",
        "back": "Back Door Inside",
        # New RLC-510A cameras (2026-07-24 swap; 2026-07-29 cleanup
        # removed the prior RLC-833A pair Building Back Solar + Building
        # Front Corner — see docs/CLEANUP-2026-07-29-RETIRED-CAMERAS.md).
        "outside_front_garage": "Outside Front Garage",
        "outside_front_power": "Outside Front Power",
        "outside_front_solar": "Outside Front Solar",  # gatekeeper
        "outside_back_solar": "Outside Back Solar",
    }

    # RTSP URLs have form: rtsp://user:pass@host:port/path
    # Passwords may contain @ (escaped as %40 or literal), so anchor on
    # ":[port]/" pattern, then walk back to find the host.
    # Strategy: split on "/" first, the host:port part is unambiguous.
    def _extract_ip(url: str) -> str | None:
        try:
            # Strip "rtsp://" prefix
            after_scheme = url.split("://", 1)[1]
            # Split on "/" — first part is "user:pass@host:port"
            auth_and_host = after_scheme.split("/", 1)[0]
            # The last "@" separates auth from host (password may contain @)
            at_idx = auth_and_host.rfind("@")
            if at_idx < 0:
                return None
            host_port = auth_and_host[at_idx + 1 :]
            # host_port is "host:port" — split on the last ":" for IPv6 safety
            # (but IPv4 RTSP is overwhelmingly common)
            if host_port.startswith("["):
                # IPv6: [::1]:554
                close = host_port.find("]")
                if close < 0:
                    return None
                return host_port[1:close]
            colon = host_port.rfind(":")
            return host_port[:colon] if colon > 0 else host_port
        except (IndexError, ValueError):
            return None

    result: dict[str, dict[str, str]] = {}

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

            for prefix, camera_name in camera_map.items():
                if key == f"{prefix}_rtsp_url":
                    result.setdefault(camera_name, {})["rtsp_url"] = value
                    # Extract IP from RTSP URL as a fallback
                    ip = _extract_ip(value)
                    if ip and "ip" not in result[camera_name]:
                        result[camera_name]["ip"] = ip
                elif key == f"{prefix}_ip" and value:
                    result.setdefault(camera_name, {})["ip"] = value

    # Sanity check: if a camera has both an RTSP-derived IP and an
    # explicit _IP env var, they must agree. (2026-07-24 hardening —
    # env file uses both, and IP rotation means the two can drift.)
    for camera_name, info in result.items():
        rtsp_url = info.get("rtsp_url", "")
        explicit_ip = info.get("ip")
        extracted = _extract_ip(rtsp_url) if rtsp_url else None
        if explicit_ip and extracted and explicit_ip != extracted:
            print(
                f"[load_camera_creds] WARN: {camera_name}: _IP={explicit_ip} "
                f"but RTSP URL host={extracted} — IP rotation detected, "
                f"update {camera_name}'s _IP env var to match"
            )

    return result


# --- Phase.166 §11.87.2: IP→HTTP_USER/HTTP_PASS lookup helpers ---
#
# The browser-automation scripts (cam_browser.py, configure_webhook*.py,
# verify_webhook.py) need to log into cameras and need the HTTP username
# + password for a given IP. The old pattern was a hardcoded
# `if ip == "192.168.1.39": creds.get("FRONT_HTTP_PASS")` block in every
# script — duplicated, brittle, breaks when a new IP gets added.
#
# These helpers walk the env file once and find the env-var PREFIX whose
# {PREFIX}_IP matches the requested IP. Then read {PREFIX}_HTTP_USER /
# {PREFIX}_HTTP_PASS. This is the same pattern as the load_camera_creds
# IP-extraction logic but in reverse: name→IP for capture (above),
# IP→name for browser auth (here).


def _read_env_kv(env_path: str) -> dict[str, str]:
    """Parse an env-style file into a flat {KEY: VALUE} dict.

    Comments (#) and blank lines are skipped. Quote characters are
    stripped from values (does not handle shell-style escaping — Reolink
    passwords don't need it). Returns {} if the file is missing.
    """
    out: dict[str, str] = {}
    if not os.path.exists(env_path):
        return out
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _resolve_prefix_for_ip(ip: str, kv: dict[str, str]) -> str | None:
    """Walk kv looking for any {PREFIX}_IP == ip. Return the PREFIX, or None.

    Matches every env var ending in `_IP` and checks the value. Stops
    on the first match — the fleet convention is one IP per prefix.
    """
    suffix = "_IP"
    for key, value in kv.items():
        if not key.endswith(suffix):
            continue
        if value == ip:
            return key[: -len(suffix)]
    return None


def get_http_user(ip: str, env_path: str | None = None) -> str | None:
    """Return the HTTP username for the camera at `ip`, or None.

    Resolution order:
      1. If env_path is None, use infra.paths.CAMERA_CREDS_FILE.
      2. Read the env file.
      3. Find the {PREFIX}_IP == ip.
      4. Return kv.get(f"{PREFIX}_HTTP_USER", "admin").
      5. If no matching IP in the file: return None.

    Returns "admin" as a default (per Reolink convention) when the IP
    is found but the env doesn't set _HTTP_USER explicitly. Returns
    None only when the IP is unknown — caller should treat None as
    "no such camera in the env file".
    """
    if env_path is None:
        # Lazy import to avoid a circular dep — paths.py imports nothing
        # from camera_creds, but keeping the import inside the function
        # means tests can monkeypatch paths.CAMERA_CREDS_FILE without
        # the module-level graph caring.
        from infra.paths import CAMERA_CREDS_FILE
        env_path = CAMERA_CREDS_FILE
    kv = _read_env_kv(env_path)
    prefix = _resolve_prefix_for_ip(ip, kv)
    if prefix is None:
        return None
    return kv.get(f"{prefix}_HTTP_USER", "admin")


def get_http_password(ip: str, env_path: str | None = None) -> str | None:
    """Return the HTTP password for the camera at `ip`, or None.

    Same resolution order as get_http_user. Returns None if the IP is
    not in the env file OR the matching _HTTP_PASS is missing/empty.

    Caller must NOT log the returned value. Use this in scripts that
    pass the password straight into CamBrowser.login(ip, user, password).
    """
    if env_path is None:
        from infra.paths import CAMERA_CREDS_FILE
        env_path = CAMERA_CREDS_FILE
    kv = _read_env_kv(env_path)
    prefix = _resolve_prefix_for_ip(ip, kv)
    if prefix is None:
        return None
    pw = kv.get(f"{prefix}_HTTP_PASS", "")
    return pw if pw else None
