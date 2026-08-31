"""
cameras.py — Camera identity + IP/credential lookup (Phase.167 §13.4).

STATUS: stable
THREAD SAFETY: thread-safe (pure parser; reads file at call time,
    no shared state).

INPUTS:
    - file $FARMSURV_CAMERAS_ENV or infra.paths.CAMERAS_ENV_FILE (NEW
      schema, Phase.167 §13.2) — gitignored, identity-only
      (CAM{N}_IP / CAM{N}_NAME / CAM{N}_ZONE) plus optional
      CAM{N}_SNAPSHOT_ALIAS / CAM{N}_PREVIEW_ALIAS chat shorthand.
    - file infra.paths.CAMERA_CREDS_FILE (LEGACY schema, operator's
      current camera-creds.env) — gitignored, secrets + identity mixed
      ({PREFIX}_IP / {PREFIX}_HTTP_USER / {PREFIX}_HTTP_PASS /
      {PREFIX}_RTSP_URL).
    - Two schemas are accepted:
        NEW (Phase.167 §13.2):
            CAM1_IP=10.0.0.1
            CAM1_NAME=<FRIENDLY_NAME>
            CAM1_ZONE=yard
            CAM1_HTTP_USER=admin
            CAM1_HTTP_PASS=secret
            CAM1_RTSP_URL=rtsp://admin:secret@10.0.0.1:554/...
        LEGACY (operator's current camera-creds.env):
            {PREFIX}_IP=10.x.x.x
            {PREFIX}_HTTP_USER=admin
            {PREFIX}_HTTP_PASS=secret
            {PREFIX}_RTSP_URL=rtsp://admin:secret@10.x.x.x:554/...
            (PREFIX is one of the registered legacy prefixes — see
            _LEGACY_PREFIX_TO_NAME below. The operator's fleet uses
            FRONT/BACK/OUTSIDE_*; tests use TEST_*.)
            etc.
    - function arg env_path: str | None — overrides the default path lookup.
      Caller may pass a synthetic-fixture path (tests) or operator path
      (production).

OUTPUTS:
    - CameraSpec dataclass instances with code/name/ip/zone/http_creds/rtsp_url
    - Short-prefix codes (CAM1, CAM2, ...) assigned in declaration order
      for the NEW schema, or derived from the legacy prefix for legacy.
    - For legacy parsing: code is the upper-cased prefix (e.g. FRONT for
      FRONT_IP/FRONT_RTSP_URL).

PUBLIC API:
    class CameraSpec
        Frozen dataclass: code, name, ip, zone, http_user, http_pass,
        rtsp_url. Use cameras.spec.CameraSpec — this is the public handle.

    load_cameras(env_path: str | None = None) -> list[CameraSpec]
        Parse the env file at env_path (or default lookup chain), try
        NEW schema first, fall back to LEGACY if no CAM*_IP vars found.
        Returns [] if the file does not exist or contains no cameras.

    by_code(code: str, env_path: str | None = None) -> CameraSpec
        Return the CameraSpec for the given CAM1-style code, or raise
        KeyError with a list of valid codes in the message.

    by_ip(ip: str, env_path: str | None = None) -> CameraSpec
        Return the CameraSpec whose IP matches `ip`, or raise KeyError.
        IP comparison is exact-string; no CIDR or hostname support.

    all_codes(env_path: str | None = None) -> list[str]
        Convenience: list[str] of valid codes in declaration order.

    display_name_for(identifier: str, env_path: str | None = None) -> str
        Resolve a camera identifier (code OR name) to the canonical
        friendly display name from the registry. Falls back to the
        input unchanged when no spec matches. Phase.167 §13.5
        Commit 13 — used by Telegram formatters so body strings
        reflect registry state, not caller-supplied literals.

    load_camera_aliases(env_path: str | None = None) -> tuple[dict[str, str], dict[str, str]]
        Read operator-private chat shorthand from CAM{N}_SNAPSHOT_ALIAS /
        CAM{N}_PREVIEW_ALIAS in cameras.env. Returns
        (snapshot_aliases, preview_aliases) where each dict maps
        shorthand → CameraSpec.code. Phase.167 §13.4 Commit 18
        (T3 C18) — replaces hardcoded `_SNAPSHOT_CAMERA_ALIASES`
        / `_PREVIEW_CAMERA_ALIASES` dicts with env-loaded values
        so the operator's chat shorthand stays gitignored. Unknown
        CAM{N} codes in the env file are dropped with a stderr WARN.

DOES NOT DO:
    - Open an RTSP connection → infra.frame_capture / infra.persistent_rtsp
    - Decrypt or store secrets — values are read directly from the env file
    - Validate RTSP reachability — that's capture-time responsibility
    - Resolve alias names → infra.camera_aliases (caller resolves first)
    - Migrate the operator's env file — see §13.4 migration plan

WHY HERE:
    Phase.167 §13.4. The fleet has grown from 2 to 6 cameras and is
    about to grow more (solar-barn pair planned). The old convention
    (FRONT/BACK/OUTSIDE_*) is operator-specific and leaked into scripts
    as PII. A generic, short-prefix code (CAM1, CAM2, ...) decouples code
    from operator naming and lets test fixtures ride along in public
    releases without exposing operator network topology.

SIDE EFFECTS:
    - load_cameras() emits one stderr `[load_cameras] WARN: ...` line
      per camera whose _IP env var disagrees with the host parsed from
      its _RTSP_URL. This is the IP-rotation signal that infra.camera_creds
      used to emit; surfacing it here means every caller (scripts,
      camera_creds delegation, future modules) gets it for free.

    Coexists with infra.camera_creds (the legacy dict parser). Scripts
    may use either while migration is in progress; the goal is to
    consolidate on infra.cameras over the rest of 6B.167.

CALLED BY:
    - listener.listener: load_cameras() at bootstrap (Phase.167 T6)
    - scripts/cam_browser.py: by_ip(ip) at login (Phase.167 T3)
    - scripts/configure_webhook.py: by_code() / all_codes() (Phase.167 T1)
    - scripts/verify_webhook.py: same (Phase.167 T1)
    - scripts/read_alarm_settings.py: same (Phase.167 T1)
    - infra.camera_creds.get_http_user/_password: delegate to by_ip() in
      Commit 6 (this PR series).

CALLS INTO:
    - stdlib os: file existence check
    - stdlib open(): read env file
    - infra.paths (lazy): CAMERA_CREDS_FILE default

RELATED:
    - camera-creds.env: the operator's production env file (legacy schema)
    - infra/tests/fixtures/synthetic_cameras.env: the public-repo test
      fixture (NEW schema). See Commit 6.
    - infra.camera_creds: legacy dict parser; will delegate here in Commit 6
    - infra.camera_aliases: caller resolves CAMERA_NAME_ALIASES first
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class CameraSpec:
    """One camera's identity + auth + RTSP endpoint. Frozen + hashable.

    Fields are intentionally explicit (no Optional). Defaults match the
    Reolink convention: HTTP user is "admin" unless overridden; HTTP
    pass / rtsp_url are empty until the env file populates them.

    Phase.167 §13.4. Replaces the operator-specific FRONT/BACK/OUTSIDE_*
    labels that used to leak through scripts.
    """
    code: str           # CAM1, CAM2, ... (NEW schema) or FRONT/BACK/... (legacy)
    name: str           # friendly label, e.g. "<FRIENDLY_NAME>"
    ip: str             # dotted-quad or hostname
    zone: str = ""      # e.g. "yard", "porch" — used by alert routing
    http_user: str = "admin"
    http_pass: str = ""
    rtsp_url: str = ""


def _default_env_path() -> str:
    """Resolution order for env_path when caller passed None:
      1. $FARMSURV_CAMERAS_ENV (Phase.167 §13.4 — operator can pin a
         specific env file for one-off runs)
      2. infra.paths.CAMERAS_ENV_FILE (NEW §13.2 schema, CAM1_IP etc.)
      3. infra.paths.CAMERA_CREDS_FILE (legacy §13.1 schema, fallback)

    Lazy-imports infra.paths to avoid a circular dependency at module load.
    """
    explicit = os.environ.get("FARMSURV_CAMERAS_ENV")
    if explicit:
        return explicit
    from infra.paths import CAMERAS_ENV_FILE, CAMERA_CREDS_FILE
    if os.path.exists(str(CAMERAS_ENV_FILE)):
        return str(CAMERAS_ENV_FILE)
    return str(CAMERA_CREDS_FILE)


def _read_env_kv(env_path: str) -> dict[str, str]:
    """Parse env file into flat {KEY: VALUE}.

    Skips blank lines and comments (lines starting with `#`). Does not
    handle shell-style escaping or quoted values — Reolink passwords
    are simple enough that this works. Returns {} if file is missing.
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


def _rtsp_host(url: str) -> str | None:
    """Extract the host/IP from an RTSP URL like `rtsp://user:pass@host:port/...`.

    Handles:
      - Plain IPv4: rtsp://user:pass@10.0.0.1:554/stream → '10.0.0.1'
      - @ in password (literal or urlencoded %40): last '@' separates auth/host
      - IPv6: rtsp://user:pass@[::1]:554/stream → '::1'

    Returns None on malformed input. Anchor is the `:port/` segment — if a
    password contains `:port/` literally it will be misread, but Reolink
    passwords don't include slashes so this is safe in practice.
    """
    if not url or not url.startswith("rtsp://"):
        return None
    try:
        # Strip "rtsp://"
        after_scheme = url[len("rtsp://"):]
        # First path segment is "user:pass@host:port" (or just "host:port" if no auth)
        auth_and_host = after_scheme.split("/", 1)[0]
        # Last '@' separates auth from host (passwords may contain @)
        at_idx = auth_and_host.rfind("@")
        host_port = auth_and_host[at_idx + 1:] if at_idx >= 0 else auth_and_host
        # IPv6: [::1]:554
        if host_port.startswith("["):
            close = host_port.find("]")
            if close < 0:
                return None
            return host_port[1:close]
        colon = host_port.rfind(":")
        return host_port[:colon] if colon > 0 else host_port
    except (IndexError, ValueError):
        return None


def _warn_if_ip_rotated(spec: CameraSpec) -> None:
    """If a camera has both _IP and an _RTSP_URL host that disagree, warn.

    Reolink DHCP can rotate the camera's IP between reboots; when the
    operator updates one of _IP / _RTSP_URL but not the other, capture
    scripts and browser-automation scripts end up talking to different
    cameras. This is a deployment-data drift signal, not a parser bug.

    Caller: load_cameras() invokes this after both NEW-schema and
    LEGACY-schema parses. Phase.166 §11.87.2 hardening.
    """
    if not spec.rtsp_url:
        return
    rtsp_host = _rtsp_host(spec.rtsp_url)
    if rtsp_host and spec.ip and rtsp_host != spec.ip:
        print(
            f"[load_cameras] WARN: {spec.name}: _IP={spec.ip} "
            f"but RTSP URL host={rtsp_host} — IP rotation detected, "
            f"update {spec.name}'s _IP env var to match"
        )


# ---------------------------------------------------------------------------
# NEW-schema parser (CAM1_IP, CAM1_NAME, ...) — Phase.167 §13.2
# ---------------------------------------------------------------------------


def _parse_new_schema(kv: dict[str, str]) -> list[CameraSpec]:
    """Parse CAM{N}_IP / _ZONE / _NAME / _HTTP_USER / _HTTP_PASS / _RTSP_URL.

    Cameras are returned in CAM{N} ascending order (CAM1 first). Cameras
    with no _IP set are skipped — a camera without an IP can't be reached
    and is almost certainly a misconfiguration. Cameras missing optional
    fields (_NAME, _ZONE, _HTTP_USER, _HTTP_PASS, _RTSP_URL) get sensible
    defaults: name=code, zone='', http_user='admin', http_pass='',
    rtsp_url=''.

    Returns [] if no CAM{N}_IP entries are found (signals to the caller
    that it should fall back to legacy parsing).
    """
    # Collect CAM{N} → fields
    cameras: dict[str, dict[str, str]] = {}
    for key, value in kv.items():
        # Match keys like CAM1_IP, CAM23_NAME, etc.
        if not key.startswith("CAM"):
            continue
        # Strip "CAM" + digits + "_"
        i = 3
        while i < len(key) and key[i].isdigit():
            i += 1
        if i == 3 or i >= len(key) or key[i] != "_":
            continue
        code = key[:i]                # e.g. "CAM1"
        field = key[i + 1:]           # e.g. "IP", "NAME", "HTTP_USER"
        cameras.setdefault(code, {})[field] = value

    specs: list[CameraSpec] = []
    for code in sorted(cameras.keys(), key=lambda c: int(c[3:])):
        fields = cameras[code]
        ip = fields.get("IP", "")
        if not ip:
            continue  # partial block — skip
        specs.append(CameraSpec(
            code=code,
            name=fields.get("NAME", code),
            ip=ip,
            zone=fields.get("ZONE", ""),
            http_user=fields.get("HTTP_USER", "admin"),
            http_pass=fields.get("HTTP_PASS", ""),
            rtsp_url=fields.get("RTSP_URL", ""),
        ))
    return specs


# ---------------------------------------------------------------------------
# Legacy-schema parser (FRONT_IP, BACK_IP, OUTSIDE_*_IP, ...)
# ---------------------------------------------------------------------------


# Legacy PREFIX → friendly NAME mapping.
#
# Phase.167 §13.3: this is the FALLBACK. The operator's existing
# camera-creds.env (as of 2026-08-30) uses FRONT_IP / BACK_IP /
# OUTSIDE_*_IP prefixes. Until the operator migrates that file to the
# NEW schema (CAM1_IP/CAM2_IP/...), this module needs to recognize those
# legacy prefixes to parse the env file.
#
# Both blocks (PROD_* and TEST_*) are needed:
#   - PROD_* are the operator's real prefixes — required for the env
#     file to actually parse in production. Note: these are operator-
#     flavored names; the public-release pipeline already scrubs them
#     (see v0.1.2's release notes for the camera_creds scrub path).
#   - TEST_* are synthetic prefixes the public tests use to exercise
#     the legacy parser without depending on operator data.
#
# Phase.167 §13.6 (public-repo impact): when this module ships in
# public, the PROD_* block is stripped at release time. The TEST_*
# block survives. infra.tests.test_cameras exercises both paths via
# TEST_* only.
_LEGACY_PREFIX_TO_NAME: dict[str, str] = {
    # --- Operator's real prefixes (production env file) ---
    "FRONT":                  "Front Door Outside",
    "BACK":                   "Back Door Inside",
    "CAM2":   "CAM2",
    "CAM3":    "CAM3",
    "CAM1":    "CAM1",
    "CAM4":     "CAM4",
    # --- Synthetic test prefixes (public-repo tests use these) ---
    "TEST_FRONT":          "Test Front",
    "TEST_BACK":           "Test Back",
    "TEST_SIDE":           "Test Side",
    "TEST_OUTSIDE":        "Test Outside",
    "TEST_FRONT_GARAGE":   "Test Front Garage",
    "TEST_FRONT_POWER":    "Test Front Power",
}

# Phase.167 §13.4 (T3 Commit 17): legacy prefixes now resolve to
# generic CAM{N} codes (in declaration order of _LEGACY_PREFIX_TO_NAME).
# This is the §13.4 migration contract: every CameraSpec produced by
# the legacy parser gets a CAM{N} code, regardless of whether the
# operator has migrated their env to the new schema yet. The friendly
# name from _LEGACY_PREFIX_TO_NAME is still returned in spec.name for
# backward compat with display layers that show the friendly name.
_LEGACY_PREFIX_TO_CODE: dict[str, str] = {
    # --- Operator's real prefixes (production env file) ---
    "FRONT":                  "CAM1",
    "BACK":                   "CAM2",
    "CAM2":   "CAM3",
    "CAM3":    "CAM4",
    "CAM1":    "CAM5",
    "CAM4":     "CAM6",
    # --- Synthetic test prefixes (public-repo tests use these) ---
    "TEST_FRONT":          "CAM1",
    "TEST_BACK":           "CAM2",
    "TEST_SIDE":           "CAM3",
    "TEST_OUTSIDE":        "CAM4",
    "TEST_FRONT_GARAGE":   "CAM5",
    "TEST_FRONT_POWER":    "CAM6",
}


def _parse_legacy_fallback(kv: dict[str, str]) -> list[CameraSpec]:
    """Parse FRONT_IP / BACK_IP / OUTSIDE_*_IP style blocks.

    Walks every env var ending in `_IP` and matches it against the legacy
    prefix map. Cameras with no matching prefix are skipped (the operator
    added a new camera to env but hasn't told this module yet — that's a
    deployment gap, not a parser bug).

    Returns [] if no _IP entries are found AND no _RTSP_URL entries
    can be parsed for known prefixes.

    Note: this is a PARSER, not a backdoor for the operator to add new
    cameras without updating the map. New cameras require either:
      (a) editing _LEGACY_PREFIX_TO_NAME in this module, or
      (b) migrating to the NEW schema (preferred — see §13.4 migration).

    IP-resolution priority per camera:
      1. Explicit {PREFIX}_IP env var
      2. IP extracted from {PREFIX}_RTSP_URL host (Phase.166 §11.87.0
         fallback — kept for backwards compat with old env files that
         only set RTSP_URL, no explicit _IP)
    """
    specs: list[CameraSpec] = []
    # Iterate _LEGACY_PREFIX_TO_CODE's insertion order so CAM{N}
    # assignment is stable across env files (env declaration order
    # would shuffle CAM{N} ↔ prefix). Skip prefixes that don't have
    # an _IP or _RTSP_URL in this env file.
    for prefix, code in _LEGACY_PREFIX_TO_CODE.items():
        if prefix not in _LEGACY_PREFIX_TO_NAME:
            continue  # code map has a prefix the name map doesn't know
        if prefix not in _LEGACY_PREFIX_TO_CODE:
            # Defensive: prefix is in name map but not code map (someone
            # edited one and not the other). Skip rather than emit a
            # spec with a fallback code.
            continue
        kv_prefix_present = (
            f"{prefix}_IP" in kv
            or f"{prefix}_RTSP_URL" in kv
        )
        if not kv_prefix_present:
            continue  # this prefix has no entries in this env file
        ip = kv.get(f"{prefix}_IP", "")
        if not ip:
            # Fallback: extract from RTSP URL host (Phase.166 §11.87.0)
            rtsp = kv.get(f"{prefix}_RTSP_URL", "")
            extracted = _rtsp_host(rtsp) if rtsp else None
            if not extracted:
                continue  # partial block — skip
            ip = extracted
        specs.append(CameraSpec(
            code=code,                                # §13.4: CAM{N} code (Phase.167 §13.4)
            name=_LEGACY_PREFIX_TO_NAME[prefix],      # legacy friendly name (still readable)
            ip=ip,
            zone="",                                  # legacy env file has no zone
            http_user=kv.get(f"{prefix}_HTTP_USER", "admin"),
            http_pass=kv.get(f"{prefix}_HTTP_PASS", ""),
            rtsp_url=kv.get(f"{prefix}_RTSP_URL", ""),
        ))
    return specs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_cameras(env_path: str | None = None) -> list[CameraSpec]:
    """Parse the env file and return all CameraSpecs in declaration order.

    Tries NEW schema first (CAM{N}_IP). If no CAM{N}_IP entries exist,
    falls back to LEGACY (FRONT_IP / BACK_IP / OUTSIDE_*_IP).

    Args:
        env_path: absolute path to the env file. If None, uses the
            resolution chain documented in _default_env_path().

    Returns:
        List of CameraSpec. Empty list if file does not exist or has
        no parseable cameras.

    Side effects:
        Emits a stderr warning per camera whose _IP and _RTSP_URL host
        disagree (Reolink DHCP-rotation drift signal — Phase.166
        §11.87.2 hardening, surfaced here from infra.camera_creds).
    """
    if env_path is None:
        env_path = _default_env_path()
    kv = _read_env_kv(env_path)

    new_specs = _parse_new_schema(kv)
    if new_specs:
        for s in new_specs:
            _warn_if_ip_rotated(s)
        return new_specs
    legacy_specs = _parse_legacy_fallback(kv)
    for s in legacy_specs:
        _warn_if_ip_rotated(s)
    return legacy_specs


def _index(specs: list[CameraSpec]) -> dict[str, CameraSpec]:
    """Build a code-keyed index. Code collisions raise (config bug)."""
    idx: dict[str, CameraSpec] = {}
    for s in specs:
        if s.code in idx:
            raise ValueError(
                f"duplicate camera code {s.code!r} in env file "
                f"(ips: {idx[s.code].ip}, {s.ip})"
            )
        idx[s.code] = s
    return idx


def by_code(code: str, env_path: str | None = None) -> CameraSpec:
    """Return the CameraSpec for the given code, or raise KeyError.

    The error message includes the list of valid codes so a typo in
    `--camera CAMX` produces a useful error like:
        KeyError: "no camera CAMX (valid: ['CAM1', 'CAM2', 'CAM3'])"
    """
    specs = load_cameras(env_path)
    idx = _index(specs)
    if code not in idx:
        raise KeyError(
            f"no camera {code!r} "
            f"(valid: {sorted(idx.keys(), key=lambda c: (len(c), c))})"
        )
    return idx[code]


def by_ip(ip: str, env_path: str | None = None) -> CameraSpec:
    """Return the CameraSpec whose IP matches `ip`, or raise KeyError.

    Comparison is exact-string. No CIDR, no hostname resolution, no
    IP/port splitting. If you have an RTSP URL, parse out the IP first.

    Error message lists valid IPs (helps when an operator's static IP
    drifted and they're chasing the wrong IP).
    """
    specs = load_cameras(env_path)
    for s in specs:
        if s.ip == ip:
            return s
    raise KeyError(
        f"no camera with ip {ip!r} "
        f"(valid: {[s.ip for s in specs]})"
    )


def code_for(identifier: str, env_path: str | None = None) -> str:
    """Return the camera code for an identifier (name OR code).

    Phase.167 §13.5 (Commit 13): inverse of display_name_for.
    Used by audit-log writers that want the machine-readable code
    rather than the operator-flavored friendly name.

    If `identifier` is already a spec.code, returns it unchanged.
    If it's a spec.name, returns the matching spec.code.
    If no spec matches (test fixtures, unknown input), returns the
    input unchanged — same fallback contract as display_name_for.

    Args:
        identifier: Either a spec.code (e.g. "CAM1") or a
            spec.name (e.g. any friendly name in the registry).
            Match is exact-string, case-sensitive; no normalization.
        env_path: Optional env file path (see load_cameras for the
            default lookup chain). Defaults to None.

    Returns:
        The matched CameraSpec.code, or `identifier` if no match.
    """
    specs = load_cameras(env_path)
    for s in specs:
        if s.code == identifier or s.name == identifier:
            return s.code
    return identifier


def display_name_for(identifier: str, env_path: str | None = None) -> str:
    """Return the friendly display name for a camera identifier.

    Phase.167 §13.5 (Commit 13): decouple Telegram bodies (and other
    user-facing strings) from the camera identifier passed by callers.
    Callers may pass either a code (e.g. "CAM1") or a name; this
    helper resolves the identifier to the canonical spec, then
    returns the spec's friendly display name.

    Falls back to returning `identifier` unchanged when no spec matches.
    That keeps the formatter usable in test contexts where the camera
    registry is synthetic and may not contain the input string, and
    keeps legacy callers (who still pass the friendly name) working
    unchanged even when the registry has renamed the name field.

    Args:
        identifier: Either a spec.code (e.g. "CAM1") or a spec.name
            (any friendly name in the registry). Match is exact-string,
            case-sensitive; no normalization.
        env_path: Optional env file path (see load_cameras for the
            default lookup chain). Defaults to None.

    Returns:
        The matched CameraSpec.name, or `identifier` if no match.
    """
    specs = load_cameras(env_path)
    for s in specs:
        if s.code == identifier or s.name == identifier:
            return s.name
    return identifier


def all_codes(env_path: str | None = None) -> list[str]:
    """Convenience: list of camera codes in declaration order.

    Cheap; just calls load_cameras() and pulls the .code attribute.
    Scripts use this for `--list-cameras` output and validation.
    """
    return [s.code for s in load_cameras(env_path)]


# Phase.167 §13.4 (C18, 2026-08-31): operator-private chat
# shorthand. The operator types "CAM1" in chat; we resolve it to
# CAM5 via cameras.env. Aliases are read from
# CAM{N}_SNAPSHOT_ALIAS / CAM{N}_PREVIEW_ALIAS in the env file
# (comma-separated; whitespace around commas tolerated).
_ALIAS_SNAPSHOT_SUFFIX = "_SNAPSHOT_ALIAS"
_ALIAS_PREVIEW_SUFFIX = "_PREVIEW_ALIAS"


def _parse_aliases_from_kv(
    kv: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Parse CAM{N}_SNAPSHOT_ALIAS / CAM{N}_PREVIEW_ALIAS from env kv.

    Returns:
        (snapshot_aliases, preview_aliases) — each is
        {shorthand: code} mapping. Both dicts are empty if no
        alias keys are present. Unknown CAM{N} codes (i.e., keys
        that don't match a registered spec) are silently dropped
        — caller is expected to log a warning or surface in /status.

    Whitespace around comma-separated shorthand is stripped. Empty
    shorthand entries (e.g. trailing comma) are dropped. Alias
    keys are case-sensitive on the suffix (`_SNAPSHOT_ALIAS`) and
    on the shorthand itself (operator types `CAM1`, not `ofs`).
    """
    import re

    snapshot: dict[str, str] = {}
    preview: dict[str, str] = {}
    pattern = re.compile(r"^(CAM\d+)_([A-Z_]+)_ALIAS$")
    for key, value in kv.items():
        m = pattern.match(key)
        if not m:
            continue
        code, alias_kind = m.group(1), m.group(2)
        # Only known kinds are accepted. Unknown kinds (e.g. FOO,
        # TEST) are silently dropped — the regex matches them but
        # they're not snapshot or preview aliases.
        if alias_kind == "SNAPSHOT":
            target = snapshot
        elif alias_kind == "PREVIEW":
            target = preview
        else:
            continue
        # Skip empty values (e.g. CAM5_SNAPSHOT_ALIAS=).
        if not value or not value.strip():
            continue
        parts = [p.strip() for p in value.split(",") if p.strip()]
        for shorthand in parts:
            target[shorthand] = code
    return snapshot, preview


def load_camera_aliases(
    env_path: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Load camera chat shorthand aliases from the env file.

    Phase.167 §13.4 (C18, 2026-08-31). The operator populates
    CAM{N}_SNAPSHOT_ALIAS and CAM{N}_PREVIEW_ALIAS in cameras.env
    to make `/snapshot?camera=CAM1` and `/preview?camera=CAM1` work
    without hardcoding the shorthand in source. The dict is
    gitignored — shorthand stays operator-private.

    Args:
        env_path: Optional explicit env file. Defaults to the
            operator's active cameras.env (resolved via
            _default_env_path).

    Returns:
        (snapshot_aliases, preview_aliases) tuple. Both are empty
        if the env file is missing or contains no alias keys.
        Aliases whose CAM{N} code isn't in load_cameras() are
        dropped (with a stderr WARN).
    """
    path = env_path if env_path is not None else _default_env_path()
    kv = _read_env_kv(path)
    snap, prev = _parse_aliases_from_kv(kv)
    valid_codes = {s.code for s in load_cameras(env_path)}
    for alias_map in (snap, prev):
        unknown = [a for a, c in alias_map.items() if c not in valid_codes]
        for alias in unknown:
            del alias_map[alias]
        if unknown:
            print(
                f"[load_camera_aliases] WARN: dropping aliases for "
                f"unknown cameras: {unknown}",
                file=sys.stderr,
            )
    return snap, prev