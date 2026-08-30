"""
camera_aliases.py — Canonical camera-name resolution from webhook payloads.

STATUS: stable
THREAD SAFETY: thread-safe (immutable mapping + pure function)

INPUTS:
    - function arg name: str (required) — camera name as it appears in
      the webhook payload (may use legacy casing/spacing)
    - module constant CAMERA_NAME_ALIASES: dict[str, str] (immutable)

OUTPUTS:
    - return value (resolve_camera_name): str — canonical camera name
      (alias → canonical; unchanged otherwise)

PUBLIC API:
    CAMERA_NAME_ALIASES: dict[str, str]
        Maps webhook-payload form to canonical OSD name. Currently a
        single legacy alias from the 2026-07-20 camera reposition.
    resolve_camera_name(name: str) -> str
        Return the canonical camera name. If `name` is an alias, returns
        the canonical form. Otherwise returns `name` unchanged.

DOES NOT DO:
    - Load camera creds or RTSP URLs → infra.camera_creds
    - Capture frames → infra.frame_capture
    - Render OSD text on frames → camera-side firmware responsibility

WHY HERE:
    Webhook payloads from Reolink sometimes use a different casing or
    spacing than the canonical OSD name (e.g. "Front Corner Inside"
    after the 2026-07-20 reposition was actually "Front Door Outside").
    This module centralizes the alias → canonical mapping so listeners
    can normalize names before lookup. Per AGENTS.md §4 (one module =
    one job), this was extracted from frame_capture.py in Part 9 step 6.

    As of 2026-07-29 the legacy "Building back solar" alias is removed
    (camera physically retired; see docs/CLEANUP-2026-07-29-RETIRED-CAMERAS.md).
    Add new aliases here, not at call sites.

CALLED BY:
    - listener.listener: resolve_camera_name() per alert (normalizes
      the camera name from the webhook before matching/lookup)

CALLS INTO:
    - stdlib dict: CAMERA_NAME_ALIASES is an immutable map at module load

RELATED:
    - infra.camera_creds: load_camera_creds keys by canonical name; aliases
      must be resolved BEFORE creds lookup
"""

# Aliases: webhook payloads from Reolink sometimes use a different
# casing/spacing than the canonical OSD name. Maps payload form -> canonical form.
# Exposed at module level so alert_listener can resolve names.
# (As of 2026-07-29 the legacy "Building back solar" alias is removed —
# that camera was physically retired; see docs/CLEANUP-2026-07-29-RETIRED-CAMERAS.md.)
CAMERA_NAME_ALIASES = {
    "Front Corner Inside": "Front Door Outside",  # legacy alias after 2026-07-20 reposition
}


def resolve_camera_name(name: str) -> str:
    """Return the canonical camera name. If `name` is an alias, returns the
    canonical form. Otherwise returns `name` unchanged.
    """
    return CAMERA_NAME_ALIASES.get(name, name)
