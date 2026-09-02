#!/usr/bin/env python3
"""
tune_510a_motion_sensitivity.py — Read or apply the Reolink motion recipe.

Phase 6B.166 §11.87.4: parameterized with CLI flags + JSON recipe.
Phase 6B.167 §13.5 (2026-08-30, T2 Commit 8):
  - Added `--camera <code>` flag (preferred). Resolves via infra.cameras.
  - Added `--list-cameras` flag for camera-registry discovery.
  - Bare-IP positional kept as `--ip <addr>` form for one-shot scripts
    (deprecated). The `--apply-all` flow iterates `infra.cameras.all_codes()`,
    not a hardcoded camera map, so the script works on any site that
    registers its cameras in camera-creds.env.

Sliders (overridable individually):
- `--motion-sensitivity N`, `--smart-person`, `--smart-vehicle`, `--smart-pet`,
  `--delay-person`, `--delay-vehicle`, `--delay-pet` override individual sliders.
- `--dry-run` prints intended changes without writing to the camera.
- `--no-recipe` skips the JSON recipe and uses the embedded `RECIPE` constant
  (current default behavior).
- `--recipe-path PATH` overrides the JSON recipe path (default
  `infra.paths.MOTION_RECIPE_FILE`).

Recipe resolution chain (when JSON mode is active):
    base = fleet (from JSON)
    if label in JSON.cameras: base = merge(base, JSON.cameras[label])
    for cli_key, cli_val in overrides: base[cli_key] = cli_val

Rewritten 2026-08-02 for firmware v3.2.0.5180_2507241368.

Key findings driving this rewrite:

- Gear nav requires an **upper-LEFT** click of `navigation_bar_remoteconfig`;
  a center click opens a stripped-down panel with no motion/webhook controls.
- The Alarm Settings page on v3.2.0.5180 has TWO collapsed Sensitivity sections
  (Motion Detection, Smart Detection). Both must be expanded before reading or
  setting sliders.
- The Person/Vehicle/Pet labels are rendered as **images (not text)** on this
  firmware, so we cannot grep textContent for them. Instead, the script uses
  **DOM group + index**: within `.toogle-controller[Smart Detection]` (and the
  parallel `.alarm-wrap[Alarm Delay]`), the slider inputs always appear in
  DOM order Person=0, Vehicle=1, Pet=2 (verified 2026-08-02).
- Reolink auto-saves on slider release; no Save button on Alarm Settings page.
- Day/night split via separate time periods is still not achievable on the 510A
  at v3.2.0.5180 (firmware limitation, not config). Single-period only.
- AGENTS.md Hard Rule: **Detection Zones** are not touched by this script —
  changing them silently is a destructive regression (burned 2026-07-20).

The recipe applied / verified by this script (also in config/motion_recipe.json
under "fleet"):

| Section         | Slider     | Target | Notes                                |
|-----------------|------------|--------|---------------------------------------|
| Motion Detection| sensitivity | 25/50  | 0–50 scale, single period 24×7        |
| Smart Detection | Person     | 50/100 | 0–100 scale (AI confidence threshold)|
| Smart Detection | Vehicle    | 30/100 | (corrected from a prior doc's 50)     |
| Smart Detection | Pet        | 30/100 | low — no pets roam this property      |
| Alarm Delay     | Person     | 0s     | 0–8 sec scale                         |
| Alarm Delay     | Vehicle    | 2s     |                                       |
| Alarm Delay     | Pet        | 2s     | debounce — animals re-enter frames    |

Usage:
    # Read current state from one camera (non-destructive)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --camera CAM1 --read

    # Apply recipe (JSON mode, uses fleet + per-camera override) to one camera
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --camera CAM1 --apply

    # Apply the embedded RECIPE constant (current default behavior)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --camera CAM1 --apply --no-recipe

    # Apply with one slider overridden (Motion Detection = 40)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --camera CAM1 --apply \
        --motion-sensitivity 40

    # Apply to all registered cameras (CONFIRM via --confirm-all)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --apply-all --confirm-all

    # List cameras known to infra.cameras
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --list-cameras

    # Print just the recipe (embedded)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --recipe

    # Print just the resolved recipe (JSON mode, current camera)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --camera CAM1 --print-resolved

    # Dry-run: show what --apply WOULD do, without writing
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --camera CAM1 --apply --dry-run

STATUS: stable
THREAD SAFETY: single-threaded (drives a headless Chromium browser)

INPUTS:
    - CLI flags (--camera CODE, --ip ADDR, --list-cameras, --apply, --read,
      --apply-all, --confirm-all, --label, --dry-run, --no-recipe,
      --recipe-path, --motion-sensitivity, --smart-person, --smart-vehicle,
      --smart-pet, --delay-person, --delay-vehicle, --delay-pet)
    - env var CAMERA_CREDS_FILE (default: <repo>/camera-creds.env, via
      infra.paths.CAMERA_CREDS_FILE) for HTTP auth
    - file config/motion_recipe.json (via infra.paths.MOTION_RECIPE_FILE)
      for the JSON recipe; --no-recipe skips this
    - camera registry: env file is read via infra.cameras to resolve
      --camera <code> → (ip, user, pass) and to enumerate the fleet for
      --apply-all.

OUTPUTS:
    - return code: 0 = success / matches recipe,
                   1 = differs / some sliders did not persist,
                   2 = error
    - writes: camera-side slider state (via Reolink web UI)
    - console output: per-slider read/apply status, diff vs recipe

PUBLIC API:
    read_current_state(page) -> dict
        Read all 7 slider values from a logged-in Alarm Settings page.
    apply_recipe_with(page, recipe) -> dict
        Apply a recipe dict to a logged-in Alarm Settings page.
        Each value is (target, ok) where ok = slider accepted the write.
    apply_recipe(page) -> dict
        Back-compat wrapper: apply_recipe_with(page, RECIPE).
    compare_to_recipe(state, recipe=None) -> (bool, list)
        Compare a state dict against a recipe (or RECIPE if None).
    do_read(ip, label, recipe=None) -> int
        Read slider values from one camera and compare to recipe.
    do_apply(ip, label, recipe=None, dry_run=False) -> int
        Apply a recipe to one camera; verify after.
    _resolve_recipe_for_run(cli_overrides, label, no_recipe, recipe_path) -> dict
        Build the effective recipe (JSON fleet + per-camera + CLI overlay).
    _resolve_camera(args) -> tuple
        Resolve --camera <code> or --ip <addr> to (ip, user, password).
        Raises SystemExit on lookup failure.
    _list_cameras() -> None
        Print the camera registry (code / name / ip / zone).
    main() -> int
        CLI entry point.

DOES NOT DO:
    - Touch detection zones — AGENTS.md hard rule (burned 2026-07-20).
    - Validate JSON recipe — that's infra.recipe.load_recipe()'s job.
    - Persist CLI overrides or recipe edits — those go in
      config/motion_recipe.json, edited by hand.
    - Filter cameras by model — --apply-all applies to every registered
      camera (the camera-flavor `reolink-camera-config` skill documents
      which models accept the slider set this script writes).

CALLED BY:
    - operator shell: scripts/tune_510a_motion_sensitivity.py --camera CAM1 --apply
    - operator shell: scripts/tune_510a_motion_sensitivity.py --apply-all --confirm-all
    - one-shot override scripts: from tune_510a_motion_sensitivity
      import _set_slider_in_group, read_current_state (per
      reolink-camera-config skill "HOW to actually use the scripts").

CALLS INTO:
    - cam_browser: CamBrowser for login + page driving
    - infra.camera_creds: get_http_user(ip), get_http_password(ip) for auth
    - infra.cameras: by_code, by_ip, all_codes, load_cameras for registry
    - infra.recipe: load_recipe(path) for JSON recipe loading
    - infra.paths: MOTION_RECIPE_FILE for default recipe path
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Load cam_browser from sibling directory
sys.path.insert(0, str(Path(__file__).parent))
from cam_browser import CamBrowser

# --- Recipe (single source of truth) ---
#
# Updated 2026-08-06 after the gatekeeper false-positive / missed-departure
# investigation. Three changes vs the previous recipe:
#
#   motion_sensitivity 15 → 25   (catch slow vehicle departures; 15 was
#                                missing trucks pulling out — they
#                                produced too few changed macroblocks)
#   smart_vehicle     50 → 30   (Reolink KB recommends lower for
#                                vehicles since they're larger and more
#                                reliably classified; 50 was causing
#                                re-classification alerts on parked
#                                trucks as wind/motion triggered
#                                momentary re-detection)
#   delay_vehicle      0 → 2    (Reolink recommends 1–3s for vehicle:
#                                AI watches ~20 frames and requires
#                                motion in MOST frames before
#                                confirming. delay=0 fires on single-
#                                frame transient classifications)
#
# Person + Pet sliders unchanged from the 2026-08-02 recipe.
#
# Verification plan: apply to one camera first (the gatekeeper — the
# camera most likely to detect departures/arrivals), monitor 24–48h,
# then roll to the rest of the fleet. Do NOT touch detection zones —
# burned 2026-07-20.

RECIPE = {
    # Motion Detection (single time period 00:00-23:59) — scale 1-50
    "motion_sensitivity": 25,
    # Smart Detection — scale 0-100 each
    "smart_person": 50,
    "smart_vehicle": 30,
    "smart_pet": 30,
    # Alarm Delay — scale 0-8 (seconds) each
    "delay_person": 0,
    "delay_vehicle": 2,
    "delay_pet": 2,
}


# --- Camera registry helpers (Phase 6B.167 §13.5 Commit 8) ---
#
# Pre-6B.167 this module hardcoded a CAMERAS_510A dict with operator-flavored
# camera names + private LAN IPs. The hardcoded list leaked the operator's
# site layout into source code (which is what lands in the public release).
#
# Now: --apply-all iterates infra.cameras.load_cameras() (the canonical
# registry). Single-camera mode uses --camera <code> (or the legacy --ip
# <addr>). Per-camera JSON-recipe overrides still key on the camera's NAME
# (the friendly display name), which is operator-site-specific and lives
# in config/motion_recipe.json under "cameras": {<name>: ...}.

def _load_fleet():
    """Return list[CameraSpec] from infra.cameras.

    Wrapper so the rest of this module doesn't import infra.cameras at
    top-level (helps unit tests patch the load function without sys.modules
    gymnastics).
    """
    from infra.cameras import load_cameras
    return load_cameras()


def _resolve_camera(args):
    """Resolve --camera <code> or --ip <addr> to (ip, user, password).

    Phase 6B.167 §13.5 (Commit 8): prefer --camera <code> (e.g. CAM1,
    CAM2, ...). Fall back to --ip <addr> for one-shot scripts that still
    pass raw IPs. Both lookups route through infra.cameras so the same
    registry handles legacy operator-flavored prefixes AND the new CAM{N} convention.

    Raises SystemExit on:
      - neither --camera nor --ip set
      - --camera code unknown (with hint listing known codes)
      - --ip not registered in camera-creds.env
      - HTTP creds missing for the resolved camera
    """
    from infra.camera_creds import get_http_password, get_http_user
    from infra.cameras import all_codes, by_code, by_ip
    from infra.paths import CAMERA_CREDS_FILE

    if getattr(args, "camera", None):
        code = args.camera
        try:
            spec = by_code(code)
        except KeyError:
            codes = ", ".join(all_codes()) or "(none — env file empty or missing)"
            raise SystemExit(
                f"unknown camera code: {code!r}. Known codes: {codes}"
            )
        ip = spec.ip
    elif getattr(args, "ip", None):
        ip = args.ip
        # Validate the IP is registered (gives a useful error if not)
        try:
            by_ip(ip)
        except KeyError:
            raise SystemExit(
                f"unknown IP: {ip} (no _IP entry found in {CAMERA_CREDS_FILE})"
            )
    else:
        raise SystemExit(
            "must specify either --camera <code> or --ip <addr>"
        )

    user = get_http_user(ip)
    if user is None:
        raise SystemExit(
            f"unknown IP: {ip} (no _IP entry found in {CAMERA_CREDS_FILE})"
        )
    pw = get_http_password(ip)
    if not pw:
        raise SystemExit(
            f"missing HTTP password for {ip} in {CAMERA_CREDS_FILE} "
            f"(need <PREFIX>_HTTP_PASS where <PREFIX>_IP={ip})"
        )

    return ip, user, pw


def _list_cameras():
    """Print the camera registry loaded from infra.cameras."""
    specs = _load_fleet()
    if not specs:
        print("(no cameras found in camera-creds.env)")
        return
    print(f"{'CODE':<10} {'NAME':<25} {'IP':<16} {'ZONE'}")
    for s in specs:
        print(f"{s.code:<10} {s.name:<25} {s.ip:<16} {s.zone}")


# --- Credential loading ---

# --- Recipe resolution (CLI + JSON) ---


def _resolve_recipe_for_run(cli_overrides: dict, label: str | None,
                            no_recipe: bool, recipe_path: str | None) -> dict:
    """Build the effective recipe for this invocation.

    Resolution chain (when JSON is loaded):
        base = fleet
        if label in cameras: base = merge(base, cameras[label])
        for cli_key, cli_val in cli_overrides: base[cli_key] = cli_val

    When --no-recipe is set: skip JSON, start from RECIPE, overlay CLI.

    CLI values are validated against FLEET_RANGES; out-of-range values
    raise SystemExit with a clear message naming the bad key + range.

    Args:
        cli_overrides: dict like {"motion_sensitivity": 30, ...} from CLI flags.
            Keys with None values should be filtered out by the caller.
        label: human camera name (e.g. "CAM5"). If label is
            not in the JSON cameras map, falls through to fleet.
        no_recipe: skip JSON, use embedded RECIPE.
        recipe_path: explicit path to JSON recipe; None = use
            infra.paths.MOTION_RECIPE_FILE.

    Returns:
        dict with the 7 RECIPE_KEYS.
    """
    from infra.recipe import (
        FLEET_RANGES,
        RECIPE_KEYS,
        load_recipe,
    )

    # --- Pick base recipe + (optionally) per-camera override ---
    loaded_recipe = None
    if not no_recipe:
        try:
            loaded_recipe = load_recipe(env_path=recipe_path)
            base = dict(loaded_recipe["fleet"])
        except (FileNotFoundError, Exception) as e:
            # load_recipe raises RecipeLoadError (subclass of ValueError)
            # or FileNotFoundError; Exception covers both.
            # If CLI overrides exist, warn and fall back to RECIPE.
            # If no overrides, fail loudly — the operator wanted JSON mode.
            if cli_overrides:
                print(
                    f"  WARNING: could not load JSON recipe ({type(e).__name__}: {e}); "
                    f"using CLI overrides on top of embedded RECIPE.",
                    file=sys.stderr,
                )
                base = dict(RECIPE)
            else:
                print(
                    f"  ERROR: could not load JSON recipe ({type(e).__name__}: {e}). "
                    f"Pass --no-recipe to use embedded RECIPE, or --recipe-path to "
                    f"point at a different file.",
                    file=sys.stderr,
                )
                sys.exit(2)
    else:
        base = dict(RECIPE)

    # --- Per-camera override (JSON mode only) ---
    if loaded_recipe is not None and label:
        per_cam = loaded_recipe.get("cameras", {}).get(label, {})
        if per_cam:
            # Per-camera values must be RECIPE_KEYS, not metadata like _comment
            for k, v in per_cam.items():
                if k in RECIPE_KEYS:
                    base[k] = v

    # --- CLI overlay (always applied last) ---
    for k, v in cli_overrides.items():
        if v is None:
            continue
        if k not in RECIPE_KEYS:
            print(
                f"  ERROR: --{k.replace('_', '-')} is not a known slider. "
                f"Allowed: {', '.join(sorted(RECIPE_KEYS))}.",
                file=sys.stderr,
            )
            sys.exit(2)
        lo, hi = FLEET_RANGES[k]
        if isinstance(v, bool) or not isinstance(v, int):
            print(
                f"  ERROR: --{k.replace('_', '-')}={v} must be int, got {type(v).__name__}.",
                file=sys.stderr,
            )
            sys.exit(2)
        if not (lo <= v <= hi):
            print(
                f"  ERROR: --{k.replace('_', '-')}={v} is out of range [{lo}..{hi}].",
                file=sys.stderr,
            )
            sys.exit(2)
        base[k] = v

    return base


# --- Camera navigation (verified 2026-08-02 on v3.2.0.5180 firmware) ---

def open_panel(page) -> bool:
    """
    Click upper-LEFT of `navigation_bar_remoteconfig` to open the FULL settings panel.

    A center click on the same gear opens a stripped-down preview-side panel
    that has NO motion controls and NO webhook controls. The upper-LEFT click is
    the ONLY way to open the full Device / Surveillance / Network / Storage / System
    menu tree.

    Returns True if the menu opened (verified by the presence of "Alarm Settings"
    AND "Surveillance" AND "System" in the body text).
    """
    for attempt in range(3):
        # Check if already on the panel
        body = page.evaluate('() => document.body.innerText')
        if "Alarm Settings" in body and "Surveillance" in body and "System" in body:
            return True

        info = page.evaluate("""(() => {
            const el = document.getElementById('navigation_bar_remoteconfig');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        })()""")
        if not info:
            time.sleep(2)
            continue

        # Upper-LEFT quadrant (per maintainer 2026-08-02)
        page.mouse.click(info["x"] + info["w"] * 0.25,
                         info["y"] + info["h"] * 0.25)
        time.sleep(5)
    body = page.evaluate('() => document.body.innerText')
    return "Alarm Settings" in body and "Surveillance" in body and "System" in body


def click_text(page, text: str) -> bool:
    """Click an element whose textContent exactly matches via JS (bypasses Playwright
    auto-wait, which hangs on Reolink's SPA panels)."""
    return page.evaluate(f"""(() => {{
        const els = Array.from(document.querySelectorAll('*'));
        const t = els.find(e => e.textContent.trim() === '{text}' && e.offsetParent !== null);
        if (t) {{ t.click(); return true; }}
        return false;
    }})()""")


def open_alarm_settings(page) -> bool:
    """Open Alarm Settings panel AND expand both Sensitivity collapsibles.

    After this returns, all 7 sliders (Motion Detection + 3 Smart Detection +
    3 Alarm Delay) are accessible via the DOM group + index helpers below.
    """
    if not open_panel(page):
        return False
    if not click_text(page, "Alarm Settings"):
        return False
    time.sleep(5)

    # Expand both Sensitivity collapsibles (both start collapsed on v3.2.0.5180)
    click_text(page, "Motion Detection")
    time.sleep(1)
    click_text(page, "Smart Detection")
    time.sleep(2)

    return True


# --- Slider identification + read/write (group + DOM-index strategy) ---

def _find_slider_in_group(page, group_label: str, slider_max: int,
                          index_in_group: int) -> dict | None:
    """
    Robust slider identification via:
      1. Find the collapsible/container that owns this `group_label` row.
         Container classes are different per section on v3.2.0.5180:
           - Motion Detection  → `.toogle-controller` (contains "Motion Detection" text)
           - Smart Detection   → `.toogle-controller` (contains "Smart Detection" text)
           - Alarm Delay       → `.alarm-wrap`         (lives inside .sensitivity-box)
      2. Within that container, take the `index_in_group`-th input[type=range]
         whose max attribute matches `slider_max`.

    This works on v3.2.0.5180 firmware where Person/Vehicle/Pet labels are
    rendered as images (not text) — so we cannot grep for the icon name in the
    row's textContent. But the DOM ORDER is stable: Person is always index 0,
    Vehicle index 1, Pet index 2 within Smart Detection / Alarm Delay sections
    (verified 2026-08-02).

    group_label: a unique substring of the container's textContent (e.g.
                 "Smart Detection", "Alarm Delay", "Motion Detection").
                 For Alarm Delay, the container class is `.alarm-wrap`; the
                 MATCH-START has lots of help text, so we accept "startsWith"
                 OR "indexOf >= 0 within first 100 chars".
    slider_max: the max attribute to filter by (50, 100, or 8).
    index_in_group: 0-based index into the slider sequence within that group.

    Returns None if not found.
    """
    return page.evaluate(f"""(() => {{
        const candidates = Array.from(document.querySelectorAll(
            '.toogle-controller, .alarm-wrap, .new-sensitivity-box'
        ));
        // For 'Alarm Delay', only .alarm-wrap containers qualify (the
        // `.toogle-controller` containers are different sections).
        const label = '{group_label}';
        let group = null;
        if (label === 'Alarm Delay') {{
            // Find .alarm-wrap whose descendant H5 text is "Alarm Delay"
            const found = Array.from(document.querySelectorAll('.alarm-wrap'));
            group = found.find(el => {{
                const h5s = el.querySelectorAll('h5');
                return Array.from(h5s).some(h => h.textContent.trim() === 'Alarm Delay');
            }}) || found[0];  // fallback: first .alarm-wrap (only one per page)
        }} else {{
            // For Motion Detection / Smart Detection, find .toogle-controller
            // whose textContent includes the label.
            group = candidates.find(el => {{
                if (!(el.className || '').toString().includes('toogle-controller')) return false;
                const txt = (el.textContent || '').trim().substring(0, 50);
                return txt.startsWith(label);
            }});
            // Fallback: any .toogle-controller whose text includes label
            if (!group) {{
                group = candidates.find(el => {{
                    if (!(el.className || '').toString().includes('toogle-controller')) return false;
                    return (el.textContent || '').includes(label);
                }});
            }}
        }}
        if (!group) return null;
        const inputs = Array.from(group.querySelectorAll('input[type=range]'))
                       .filter(e => e.max === '{slider_max}');
        if ({index_in_group} >= inputs.length) return null;
        const inp = inputs[{index_in_group}];
        const r = inp.getBoundingClientRect();
        return {{
            value: parseInt(inp.value),
            max: parseInt(inp.max),
            min: parseInt(inp.min),
            x: Math.round(r.x),
            y: Math.round(r.y),
        }};
    }})()""")


def _set_slider_in_group(page, group_label: str, slider_max: int,
                         index_in_group: int, target_value: int) -> bool:
    """Set a slider (identified by group + DOM index) to target_value, then verify."""
    ok = page.evaluate(f"""(() => {{
        const candidates = Array.from(document.querySelectorAll(
            '.toogle-controller, .alarm-wrap, .new-sensitivity-box'
        ));
        const label = '{group_label}';
        let group = null;
        if (label === 'Alarm Delay') {{
            const found = Array.from(document.querySelectorAll('.alarm-wrap'));
            group = found.find(el => {{
                const h5s = el.querySelectorAll('h5');
                return Array.from(h5s).some(h => h.textContent.trim() === 'Alarm Delay');
            }}) || found[0];
        }} else {{
            group = candidates.find(el => {{
                if (!(el.className || '').toString().includes('toogle-controller')) return false;
                return (el.textContent || '').trim().startsWith(label);
            }});
            if (!group) {{
                group = candidates.find(el => {{
                    if (!(el.className || '').toString().includes('toogle-controller')) return false;
                    return (el.textContent || '').includes(label);
                }});
            }}
        }}
        if (!group) return false;
        const inputs = Array.from(group.querySelectorAll('input[type=range]'))
                       .filter(e => e.max === '{slider_max}');
        if ({index_in_group} >= inputs.length) return false;
        const inp = inputs[{index_in_group}];
        inp.focus();
        inp.value = '{target_value}';
        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }})()""")
    if not ok:
        return False
    time.sleep(1.0)
    out = _find_slider_in_group(page, group_label, slider_max, index_in_group)
    return out is not None and out["value"] == target_value


# --- High-level read / apply ---

def read_current_state(page) -> dict:
    """
    Read the current slider values from the camera. Caller must have called
    open_alarm_settings(page) first.

    Returns dict with keys matching RECIPE (motion_sensitivity, smart_person,
    smart_vehicle, smart_pet, delay_person, delay_vehicle, delay_pet).
    Missing keys indicate a slider could not be located.
    """
    out = {}
    # Motion Detection — single slider at index 0
    motion = _find_slider_in_group(page, "Motion Detection", 50, 0)
    if motion:
        out["motion_sensitivity"] = motion["value"]
    # Smart Detection — Person / Vehicle / Pet by DOM order (index 0/1/2)
    for idx, key in enumerate(["smart_person", "smart_vehicle", "smart_pet"]):
        s = _find_slider_in_group(page, "Smart Detection", 100, idx)
        if s:
            out[key] = s["value"]
    # Alarm Delay — Person / Vehicle / Pet by DOM order
    for idx, key in enumerate(["delay_person", "delay_vehicle", "delay_pet"]):
        s = _find_slider_in_group(page, "Alarm Delay", 8, idx)
        if s:
            out[key] = s["value"]
    return out


def apply_recipe_with(page, recipe: dict) -> dict:
    """Apply a recipe dict to the camera. Caller must have called open_alarm_settings(page).

    Returns dict mapping slider key to (target_value, ok_bool). The "motion"
    key is used for backwards-compat with apply_recipe() readers; the other
    keys match the recipe directly. The "error" key is set if the Motion
    Detection slider cannot be located.
    """
    result: dict = {"motion": None, "smart_person": None, "smart_vehicle": None,
                    "smart_pet": None, "delay_person": None, "delay_vehicle": None,
                    "delay_pet": None}

    # Motion Detection — single slider
    motion = _find_slider_in_group(page, "Motion Detection", 50, 0)
    if not motion:
        return {**result, "error": "no Motion Detection slider found"}
    ok = _set_slider_in_group(page, "Motion Detection", 50, 0,
                              recipe["motion_sensitivity"])
    result["motion"] = (recipe["motion_sensitivity"], ok)

    # Smart Detection (Person, Vehicle, Pet by DOM order)
    for idx, key in enumerate(["smart_person", "smart_vehicle", "smart_pet"]):
        target = recipe[key]
        ok = _set_slider_in_group(page, "Smart Detection", 100, idx, target)
        result[key] = (target, ok)

    # Alarm Delay (Person, Vehicle, Pet by DOM order)
    for idx, key in enumerate(["delay_person", "delay_vehicle", "delay_pet"]):
        target = recipe[key]
        ok = _set_slider_in_group(page, "Alarm Delay", 8, idx, target)
        result[key] = (target, ok)

    return result


def apply_recipe(page) -> dict:
    """Back-compat wrapper: apply_recipe_with(page, RECIPE).

    Kept for callers (e.g. one-shot override scripts) that don't care about
    the per-camera override / CLI overlay chain.
    """
    return apply_recipe_with(page, RECIPE)


def compare_to_recipe(state: dict, recipe: dict | None = None) -> tuple:
    """Return (matches, diffs). matches: bool. diffs: list of (key, actual, expected)."""
    ref = recipe if recipe is not None else RECIPE
    diffs = []
    for key, expected in ref.items():
        if key not in state:
            diffs.append((key, "MISSING", expected))
        elif state[key] != expected:
            diffs.append((key, state[key], expected))
    return len(diffs) == 0, diffs


# --- Camera session drivers ---

def do_read(ip: str, label: str, recipe: dict | None = None) -> int:
    """Read current motion settings from one camera.

    Args:
        ip: camera IP address
        label: human label for output
        recipe: dict to compare against; if None, uses RECIPE.

    Returns exit code: 0 = matches recipe, 1 = differs, 2 = error.
    """
    from infra.camera_creds import get_http_password

    ref = recipe if recipe is not None else RECIPE
    pw = get_http_password(ip)
    if not pw:
        print(f"  ERROR: no HTTP_PASS in camera-creds.env for IP {ip}", file=sys.stderr)
        return 2
    try:
        with CamBrowser(headless=True) as cb:
            cb.login(ip, "admin", pw)
            time.sleep(2)
            if not open_alarm_settings(cb.page):
                print("  ERROR: gear panel did not open (gear click failed 3x)",
                      file=sys.stderr)
                return 2
            state = read_current_state(cb.page)
            print(f"\n[{label}] {ip} — current state:")
            for key in ref:
                v = state.get(key, "—")
                match = "✓" if v == ref[key] else "✗"
                print(f"  {match} {key:25} = {v}  (recipe: {ref[key]})")
            matches, diffs = compare_to_recipe(state, ref)
            if matches:
                print("\n  ALL VALUES MATCH RECIPE.")
                return 0
            else:
                print(f"\n  {len(diffs)} value(s) differ from recipe:")
                for key, actual, expected in diffs:
                    print(f"    {key}: actual={actual} expected={expected}")
                return 1
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


def do_apply(ip: str, label: str, recipe: dict | None = None,
             dry_run: bool = False) -> int:
    """Apply motion settings to one camera.

    Args:
        ip: camera IP address
        label: human label for output
        recipe: dict to apply; if None, uses RECIPE.
        dry_run: if True, print intended changes without writing to camera.

    Returns exit code:
        0 = applied + verified (or dry-run OK),
        1 = verify failed / some sliders did not persist,
        2 = apply error.
    """
    from infra.camera_creds import get_http_password

    ref = recipe if recipe is not None else RECIPE

    # Dry-run: print what we'd do without logging in
    if dry_run:
        print(f"\n[{label}] {ip} — DRY RUN, would apply recipe:")
        for k, v in ref.items():
            if k.startswith("_"):
                # skip _comment keys — informational, not a slider
                continue
            print(f"  would set {k:<22} = {v}")
        print("\n  (no changes written; --dry-run was set)")
        return 0

    pw = get_http_password(ip)
    if not pw:
        print(f"  ERROR: no HTTP_PASS in camera-creds.env for IP {ip}", file=sys.stderr)
        return 2
    try:
        with CamBrowser(headless=True) as cb:
            cb.login(ip, "admin", pw)
            time.sleep(2)
            if not open_alarm_settings(cb.page):
                print("  ERROR: gear panel did not open", file=sys.stderr)
                return 2
            print(f"\n[{label}] {ip} — applying recipe:")
            result = apply_recipe_with(cb.page, ref)
            if "error" in result:
                print(f"  ERROR: {result['error']}", file=sys.stderr)
                return 2
            for key, val in result.items():
                target, ok = val
                marker = "✓" if ok else "✗"
                print(f"  {marker} {key:25} = {target}")
            if not all(ok for _, ok in result.values()):
                print("\n  WARNING: one or more sliders did not confirm — re-read "
                      "and manually inspect the camera.")
                return 1

            # Re-read after a moment to confirm persistence (Reolink auto-saves
            # on slider change, but verify after the slow path settles)
            time.sleep(1)
            state = read_current_state(cb.page)
            matches, diffs = compare_to_recipe(state, ref)
            print("\n  Re-read after apply:")
            for key in ref:
                v = state.get(key, "—")
                match = "✓" if v == ref[key] else "✗"
                print(f"    {match} {key:25} = {v}  (expected {ref[key]})")
            if matches:
                print("\n  ALL VALUES MATCH RECIPE. APPLY SUCCESSFUL.")
                return 0
            else:
                print(f"\n  {len(diffs)} value(s) did not stick — slider events may need "
                      f"an extra 'change' or 'blur' dispatch. Investigate.", file=sys.stderr)
                return 1
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(
        description="Read or apply the Reolink motion recipe to one or all cameras.")
    target = parser.add_argument_group("camera selector")
    target.add_argument("--camera", metavar="CODE", default=None,
                        help="Camera code (CAM1, CAM2, TEST_FRONT, ...). "
                             "Resolves via infra.cameras.")
    target.add_argument("--ip", metavar="ADDR", default=None,
                        help="[deprecated] Bare IP address. Use --camera instead. "
                             "Kept for one-shot scripts that pass a raw IP.")
    target.add_argument("ip_positional", nargs="?", default=None,
                        help=argparse.SUPPRESS)  # legacy bare-IP positional
    target.add_argument("--list-cameras", action="store_true",
                        help="Print the camera registry and exit.")
    parser.add_argument("--read", action="store_true",
                        help="Read current state and compare to recipe (non-destructive).")
    parser.add_argument("--apply", action="store_true",
                        help="Apply the recipe to ONE camera. CONFIRM this is the intended target.")
    parser.add_argument("--apply-all", action="store_true",
                        help="Apply the recipe to every registered camera. "
                             "Requires --confirm-all.")
    parser.add_argument("--confirm-all", action="store_true",
                        help="Required safety flag when using --apply-all.")
    parser.add_argument("--label", default="",
                        help="Human label for the camera (used in output and for "
                             "per-camera JSON recipe lookup). If omitted, defaults "
                             "to the camera's friendly name from infra.cameras.")
    parser.add_argument("--recipe", action="store_true",
                        help="Print the embedded RECIPE constant and exit.")
    # Phase 6B.166 §11.87.4 — new flags
    parser.add_argument("--motion-sensitivity", type=int, default=None,
                        help="Override motion sensitivity (0-50).")
    parser.add_argument("--smart-person", type=int, default=None,
                        help="Override smart person (0-100).")
    parser.add_argument("--smart-vehicle", type=int, default=None,
                        help="Override smart vehicle (0-100).")
    parser.add_argument("--smart-pet", type=int, default=None,
                        help="Override smart pet (0-100).")
    parser.add_argument("--delay-person", type=int, default=None,
                        help="Override delay person (0-8 sec).")
    parser.add_argument("--delay-vehicle", type=int, default=None,
                        help="Override delay vehicle (0-8 sec).")
    parser.add_argument("--delay-pet", type=int, default=None,
                        help="Override delay pet (0-8 sec).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intended changes without writing to the camera.")
    parser.add_argument("--no-recipe", action="store_true",
                        help="Skip JSON recipe (config/motion_recipe.json); use "
                             "embedded RECIPE constant. CLI overrides still apply.")
    parser.add_argument("--recipe-path", type=str, default=None,
                        help="Path to motion recipe JSON (default: "
                             "infra.paths.MOTION_RECIPE_FILE = "
                             "<repo>/config/motion_recipe.json).")
    parser.add_argument("--print-resolved", action="store_true",
                        help="Print the resolved recipe (after JSON + per-camera "
                             "+ CLI overlay) and exit. Useful for debugging what "
                             "an --apply WOULD do.")
    args = parser.parse_args()

    # --- Legacy bare-IP positional back-compat ---
    # If user wrote `tune_510a_motion_sensitivity.py 10.0.0.1 --read` (old form),
    # accept that as --ip 10.0.0.1.
    if args.ip_positional and not args.ip:
        args.ip = args.ip_positional

    # --- --list-cameras: short-circuit, no camera selector needed ---
    if args.list_cameras:
        _list_cameras()
        return 0

    # --- CLI overrides (only non-None values) ---
    cli_overrides = {
        "motion_sensitivity": args.motion_sensitivity,
        "smart_person":       args.smart_person,
        "smart_vehicle":      args.smart_vehicle,
        "smart_pet":          args.smart_pet,
        "delay_person":       args.delay_person,
        "delay_vehicle":      args.delay_vehicle,
        "delay_pet":          args.delay_pet,
    }

    # --- Print embedded RECIPE (pre-resolution) ---
    if args.recipe:
        print("Embedded RECIPE constant (--recipe shows this, NOT the resolved recipe):")
        for k, v in RECIPE.items():
            print(f"  {k:25} = {v}")
        return 0

    # --- --print-resolved: needs a target (camera or IP), but no actual apply ---
    if args.print_resolved:
        label = args.label or None
        if (args.camera or args.ip) and not label:
            # Derive label from the resolved camera's friendly name
            try:
                ip, _, _ = _resolve_camera(args)
                from infra.cameras import by_ip
                try:
                    label = by_ip(ip).name
                except KeyError:
                    label = None
            except SystemExit:
                raise  # propagate lookup errors
        resolved = _resolve_recipe_for_run(
            cli_overrides=cli_overrides,
            label=label,
            no_recipe=args.no_recipe,
            recipe_path=args.recipe_path,
        )
        print("Resolved recipe (JSON fleet + per-camera + CLI overlay):")
        for k, v in resolved.items():
            print(f"  {k:25} = {v}")
        if label:
            print(f"\n  (per-camera label: {label})")
        if args.no_recipe:
            print("  (--no-recipe: JSON skipped; embedded RECIPE used as base)")
        return 0

    # --- --apply-all path ---
    if args.apply_all:
        if not args.confirm_all:
            print("ERROR: --apply-all requires --confirm-all as a safety flag.",
                  file=sys.stderr)
            return 2
        fleet = _load_fleet()
        if not fleet:
            print("ERROR: --apply-all but no cameras found in camera-creds.env.",
                  file=sys.stderr)
            return 2
        print(f"=== Applying recipe to {len(fleet)} camera(s) ===")
        all_ok = True
        for spec in fleet:
            label = args.label or spec.name
            print(f"\n----- {spec.code}: {spec.name} ({spec.ip}) -----")
            resolved = _resolve_recipe_for_run(
                cli_overrides=cli_overrides,
                label=label,
                no_recipe=args.no_recipe,
                recipe_path=args.recipe_path,
            )
            rc = do_apply(spec.ip, label, recipe=resolved, dry_run=args.dry_run)
            if rc != 0:
                all_ok = False
        if all_ok:
            if args.dry_run:
                print(f"\n=== DRY RUN: {len(fleet)} camera(s) listed what would be applied. ===")
            else:
                print(f"\n=== ALL {len(fleet)} CAMERA(S) APPLIED SUCCESSFULLY ===")
            return 0
        else:
            print("\n=== One or more cameras had errors — inspect above ===",
                  file=sys.stderr)
            return 1

    # --- Single-camera path (--camera or --ip) ---
    if not (args.camera or args.ip):
        parser.print_help()
        print("\nCamera registry (from infra.cameras):")
        _list_cameras()
        return 0

    ip, _, _ = _resolve_camera(args)
    # Resolve label: explicit --label wins; else friendly name from registry; else IP
    if args.label:
        label = args.label
    else:
        try:
            from infra.cameras import by_ip
            label = by_ip(ip).name
        except KeyError:
            label = ip

    resolved = _resolve_recipe_for_run(
        cli_overrides=cli_overrides,
        label=label,
        no_recipe=args.no_recipe,
        recipe_path=args.recipe_path,
    )

    if args.read:
        return do_read(ip, label, recipe=resolved)
    elif args.apply:
        return do_apply(ip, label, recipe=resolved, dry_run=args.dry_run)
    else:
        # Default if just a camera/IP is given: read
        return do_read(ip, label, recipe=resolved)


if __name__ == "__main__":
    sys.exit(main())
