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
    spacing than the canonical OSD name (e.g. "<LEGACY_NAME>"
    after the 2026-07-20 reposition was actually "<FRIENDLY_NAME>").
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
#
# Phase 6B.167 §13.4 Commit 17 (T3 C17): both keys and values were
# operator-side friendly names. The §13.4 migration promotes these
# to CAM{N} codes per infra.cameras._LEGACY_PREFIX_TO_CODE. The alias
# preserves the 2026-07-20 camera reposition's friendly-name rename
# for retroactively-stored alerts whose stored camera_name field
# uses the legacy (pre-reposition) friendly name. resolvers downstream
# can promote the CAM{N} canonical back to the operator's friendly
# name via infra.cameras.display_name_for() when the alert needs to
# be rendered for the operator.
CAMERA_NAME_ALIASES: dict[str, str] = {}
# Phase 6B.167 §13.4 Commit 17 (T3 C17): the legacy friendly-name
# alias map is empty. Pre-2026-07-20 friendly-name renames
# ("<LEGACY_NAME>" -> "<FRIENDLY_NAME>") were operator-
# internal cosmetics; alerts from that retro-window are already in
# alert.jsonl with their original friendly name and continue to
# work via code_for() (which returns the input unchanged on
# unknown identifiers). CAM{N} codes don't need alias resolution.


def resolve_camera_name(name: str) -> str:
    """Return the canonical camera name. If `name` is an alias, returns the
    canonical form. Otherwise returns `name` unchanged.
    """
    return CAMERA_NAME_ALIASES.get(name, name)
