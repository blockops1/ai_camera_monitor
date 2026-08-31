"""
recipe.py — Load and resolve the motion-detection recipe.

STATUS: provisional
THREAD SAFETY: single-threaded (validates + caches on first load;
    re-imported per-call site; no shared state)

INPUTS:
    - file config/motion_recipe.json — read by default; path
      override via env_path argument
      Schema:
        {
          "_comment": "...",
          "fleet": {
            "motion_sensitivity": <int 0-50>,
            "smart_person": <int 0-100>,
            "smart_vehicle": <int 0-100>,
            "smart_pet": <int 0-100>,
            "delay_person": <int 0-8>,
            "delay_vehicle": <int 0-8>,
            "delay_pet": <int 0-8>
          },
          "cameras": {
            "<camera label>": {  # e.g. "<CAMERA_LABEL>"
              "motion_sensitivity": <int 0-50>,   # optional override
              ...
              "_comment": "..."  # optional human note
            }
          }
        }
      Per-camera entries with {} (no keys) fall through to fleet
      values silently.

OUTPUTS:
    - return value: dict with all 7 recipe keys (motion_sensitivity,
      smart_person, smart_vehicle, smart_pet, delay_person,
      delay_vehicle, delay_pet), each value an int
    - load_recipe() raises RecipeLoadError on bad JSON / missing file
      / unknown keys / out-of-range values
    - resolve_for_camera(label) returns the merged recipe dict
      (fleet + per-camera override) for the given camera label.
      Unknown camera labels return the fleet recipe (no error —
      caller decides whether to warn).

PUBLIC API:
    load_recipe(env_path: str | None = None) -> dict
        Load + validate config/motion_recipe.json. Returns the raw
        top-level dict ("fleet", "cameras", "_comment"). Validates
        every key on load (range + whitelist). Raises RecipeLoadError
        on any validation failure.
        env_path=None → uses infra.paths.MOTION_RECIPE_FILE.
    resolve_for_camera(label: str, recipe: dict | None = None,
                       env_path: str | None = None) -> dict
        Compute the effective recipe for a single camera by merging
        the fleet recipe with the per-camera override (override wins).
        Unknown camera labels return the fleet recipe unchanged.
        recipe/env_path both optional — if neither is given, calls
        load_recipe() internally (one-shot validation).
    validate_recipe(recipe: dict) -> None
        Pure validation helper (no I/O). Raises RecipeLoadError on
        unknown keys or out-of-range values. Useful for tests that
        construct synthetic recipe dicts.
    RECIPE_KEYS — frozenset of the 7 valid slider keys.
    FLEET_RANGES — dict mapping slider key → (min, max) inclusive.

DOES NOT DO:
    - Does NOT apply a recipe to a camera. (That's the caller's job:
      scripts/tune_510a_motion_sensitivity.py uses the resolved
      dict to call the Reolink API.)
    - Does NOT log recipe loads or misses. (Callers may log if they
      care; infra modules don't pollute logs on every parameterize.)
    - Does NOT auto-migrate the old RECIPE dict from
      tune_510a_motion_sensitivity.py. (That dict stays as a
      fallback for --no-recipe invocations; see Q3 in PROPOSAL.)

CALLED BY:
    - scripts/tune_510a_motion_sensitivity.py: resolve_for_camera()
      at --apply / --dry-run (Phase.166 §11.87.4 - planned)
    - scripts/apply_all_tuning.py: iterates resolve_for_camera() per
      camera (Phase.166 §11.87.7 - planned)

CALLS INTO:
    - stdlib json: load + validate JSON
    - stdlib pathlib.Path: file existence check
    - infra.paths: MOTION_RECIPE_FILE (default file path)

RELATED:
    - docs/PROPOSAL-script-parameterization.md §11.87.x.3 — proposal
      schema + design rationale
    - config/motion_recipe.json — the actual recipe data
    - scripts/tune_510a_motion_sensitivity.py RECIPE — fallback
      defaults embedded in the script (used when --no-recipe passed)
"""

from __future__ import annotations

import json
from pathlib import Path

from infra.paths import MOTION_RECIPE_FILE

# Slider key whitelist — exactly the 7 fields the Reolink RLC-510A
# firmware exposes on the Motion Detection / Smart Detection / Alarm
# Delay panels. If a recipe file contains anything else, we reject
# the load rather than silently ignoring it.
RECIPE_KEYS = frozenset({
    "motion_sensitivity",
    "smart_person",
    "smart_vehicle",
    "smart_pet",
    "delay_person",
    "delay_vehicle",
    "delay_pet",
})

# Inclusive (min, max) for each slider, matching the Reolink RLC-510A
# firmware scale. Verified against the slider readback in
# scripts/tune_510a_motion_sensitivity.py::_find_slider_in_group().
FLEET_RANGES = {
    "motion_sensitivity": (0, 50),   # 0 = lowest motion sensitivity
    "smart_person": (0, 100),         # Smart Person confidence
    "smart_vehicle": (0, 100),        # Smart Vehicle confidence
    "smart_pet": (0, 100),            # Smart Pet confidence
    "delay_person": (0, 8),           # seconds of sustained detection
    "delay_vehicle": (0, 8),          # seconds of sustained detection
    "delay_pet": (0, 8),              # seconds of sustained detection
}

# Keys that are NOT recipe values but are allowed as metadata in the
# JSON file. _comment is the standard escape hatch used elsewhere in
# this repo (motion_gate_thresholds.json, alert_overrides.json).
METADATA_KEYS = frozenset({"_comment"})


class RecipeLoadError(ValueError):
    """Raised when config/motion_recipe.json fails validation."""


def validate_recipe(recipe: dict) -> None:
    """Validate a recipe dict. Raises RecipeLoadError on any problem.

    Pure function — no I/O. Accepts the raw top-level dict (the same
    shape load_recipe() returns). Caller is expected to have already
    parsed the JSON.
    """
    if not isinstance(recipe, dict):
        raise RecipeLoadError(
            f"recipe must be a top-level JSON object, got {type(recipe).__name__}"
        )

    # Top-level keys: _comment (optional), fleet (required), cameras (optional)
    unknown_top = set(recipe.keys()) - METADATA_KEYS - {"fleet", "cameras"}
    if unknown_top:
        raise RecipeLoadError(
            f"unknown top-level keys in recipe: {sorted(unknown_top)} "
            f"(allowed: fleet, cameras, _comment)"
        )

    if "fleet" not in recipe:
        raise RecipeLoadError("recipe missing required 'fleet' section")

    fleet = recipe["fleet"]
    if not isinstance(fleet, dict):
        raise RecipeLoadError(
            f"recipe.fleet must be a JSON object, got {type(fleet).__name__}"
        )
    _validate_slider_dict(fleet, scope="fleet")

    cameras = recipe.get("cameras", {})
    if not isinstance(cameras, dict):
        raise RecipeLoadError(
            f"recipe.cameras must be a JSON object, got {type(cameras).__name__}"
        )
    for label, override in cameras.items():
        if not isinstance(override, dict):
            raise RecipeLoadError(
                f"recipe.cameras[{label!r}] must be a JSON object, "
                f"got {type(override).__name__}"
            )
        _validate_slider_dict(override, scope=f"cameras[{label!r}]")


def _validate_slider_dict(sliders: dict, *, scope: str) -> None:
    """Validate one fleet/camera slider dict in isolation."""
    unknown = set(sliders.keys()) - RECIPE_KEYS - METADATA_KEYS
    if unknown:
        raise RecipeLoadError(
            f"unknown recipe keys in {scope}: {sorted(unknown)} "
            f"(allowed: {sorted(RECIPE_KEYS | METADATA_KEYS)})"
        )
    for key, value in sliders.items():
        if key in METADATA_KEYS:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise RecipeLoadError(
                f"recipe.{scope}.{key}={value!r}: "
                f"must be int (got {type(value).__name__})"
            )
        lo, hi = FLEET_RANGES[key]
        if not (lo <= value <= hi):
            raise RecipeLoadError(
                f"recipe.{scope}.{key}={value}: out of range "
                f"(must be {lo}..{hi})"
            )


def load_recipe(env_path: str | None = None) -> dict:
    """Load + validate config/motion_recipe.json.

    Returns the raw top-level dict. Validates every key on load.
    Raises RecipeLoadError on any problem. Raises FileNotFoundError
    if the file is missing (callers may catch + fall back to embedded
    RECIPE in the script).
    """
    path = Path(env_path) if env_path is not None else Path(MOTION_RECIPE_FILE)
    if not path.exists():
        raise FileNotFoundError(f"recipe file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RecipeLoadError(
            f"recipe file {path} is not valid JSON: {e}"
        ) from e
    validate_recipe(raw)
    return raw


def resolve_for_camera(
    label: str,
    recipe: dict | None = None,
    env_path: str | None = None,
) -> dict:
    """Compute the effective recipe for a single camera.

    Merges the fleet recipe with the per-camera override (override
    wins). Returns a flat dict containing all 7 slider keys.

    - label: camera label as it appears in recipe.cameras (e.g. "<CAMERA_LABEL>"). Unknown labels are tolerated: the
      fleet recipe is returned unchanged (callers can detect via
      recipe.get("cameras", {}).get(label)).
    - recipe: pre-loaded recipe dict (skip the load_recipe() round
      trip when iterating over many cameras). Optional.
    - env_path: file path for load_recipe() when recipe is None.
      Optional.

    Does not mutate the input recipe.
    """
    if recipe is None:
        recipe = load_recipe(env_path=env_path)

    fleet = recipe.get("fleet", {})
    cameras = recipe.get("cameras", {})
    # Phase.167 §13.4 Commit 17 (T3 C17): recipe.cameras keys are
    # CAM{N} codes (per infra.cameras._LEGACY_PREFIX_TO_CODE and
    # config/motion_recipe.json §13.4 keys). Translate label via
    # infra.cameras.code_for() so callers passing friendly names
    # ("<CAMERA_LABEL>") or legacy codes ("CAM1")
    # resolve transparently. Falls back to the literal label so test
    # fixtures passing synthetic codes still work.
    from infra.cameras import code_for  # §13.4: name → CAM{N}
    _cam_key = code_for(label) if label else ""
    if _cam_key and _cam_key in cameras:
        override = cameras[_cam_key]
    else:
        # Fallback: literal label (covers test fixtures using synthetic
        # labels and any caller that has not migrated to CAM{N} yet).
        override = cameras.get(label) or {}

    # Start with fleet, overlay override. _comment keys are filtered
    # out — they're metadata, not slider values.
    merged: dict = {}
    for key in RECIPE_KEYS:
        merged[key] = override.get(key, fleet.get(key))
    return merged
