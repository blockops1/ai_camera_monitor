#!/usr/bin/env python3
"""
read_alarm_settings.py — Read the FULL Alarm Settings page on a Reolink camera.

Phase 6B.166 §11.87.5: camera list comes from infra.camera_creds (the
canonical source); recipe comparison is opt-in via --recipe.

[STATUS / module header sections follow below — see refactor-module-header skill]

STATUS: stable
THREAD SAFETY: single-threaded (drives a headless Chromium browser)

INPUTS:
    - camera-creds.env (loaded via infra.camera_creds) — source of camera list
    - config/motion_recipe.json (loaded via infra.recipe) — only used when
      --recipe is set; ignored otherwise
    - CLI flags: ip (optional), --json, --headed, --recipe, --recipe-path,
      --label

OUTPUTS:
    - human-readable per-camera slider dump (default) OR JSON to stdout
    - exit codes: 0 = all reads OK, 1 = at least one camera failed, 2 =
      fatal config error (no creds file, no HTTP_PASS)

PUBLIC API:
    open_alarm_settings(page) -> None
        Navigate to the Alarm Settings page. Idempotent.
    read_all_inputs(page) -> list
        Enumerate every visible input/select on the Alarm Settings page.
    read_visible_text(page) -> list
        Get all visible text on the page for context.
    expand_section(page, label) -> bool
        Click a section header to expand.
    scroll_config_box(page, position) -> None
        Scroll the config-scroll-box container to a position.
    read_camera(ip, user, password, *, headless=True) -> dict
        Read the Alarm Settings page on one camera.
    print_human(camera_name, result, *, recipe=None) -> None
        Pretty-print one camera's result, optionally diffed against recipe.

DOES NOT DO:
    - Change any camera state (read-only).
    - Touch detection zones.
    - Load camera-creds.env directly (uses infra.camera_creds).
    - Apply recipe to cameras (that's tune_510a_motion_sensitivity.py).

CALLED BY:
    - operator shell: scripts/read_alarm_settings.py            # all cameras
    - operator shell: scripts/read_alarm_settings.py --camera <label>  # single camera
    - scripts/apply_all_tuning.py (one-shot use)

Camera labels and IPs are sourced from camera-creds.env via infra.camera_creds.
Phase 6B.167 §13.4: positional `ip` arg deprecated; use `--camera <label>` (or
`--camera CAMn` once infra.cameras.py lands in T2 Commit 5).

CALLS INTO:
    - cam_browser: CamBrowser for login + page driving
    - infra.camera_creds: load_camera_creds, get_http_user, get_http_password
    - infra.recipe: load_recipe, resolve_for_camera (only when --recipe)
    - infra.paths: CAMERA_CREDS_FILE, MOTION_RECIPE_FILE
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).parent))
from cam_browser import CamBrowser

# --- Camera list (Phase 6B.166 §11.87.5) ---
#
# Cameras are discovered from infra.camera_creds.load_camera_creds(env_path),
# which reads <repo>/camera-creds.env (via infra.paths.CAMERA_CREDS_FILE).
# The camera_map there maps prefixes (e.g. 'outside_front_solar') to canonical
# labels (e.g. 'Front Porch') — those labels match config/motion_recipe.json
# per-camera override keys.
#
# The hardcoded ALL_CAMERAS list was removed 2026-08-30 in §11.87.5.
#
# Phase 6B.167 §13.4: this site-specific inventory is intentionally not pinned
# in source. Cameras come from camera-creds.env (operator's per-deploy config);
# run `scripts/read_alarm_settings.py --list-cameras` to see what is configured.


def open_alarm_settings(page) -> None:
    """Navigate to the Alarm Settings page. Idempotent — if already there, no-op.

    Critical context — burned 2026-07-26:
      The gear icon at top-right of the post-login landing page is the SYSTEM
      SETTINGS gear (id='navigation_bar_remoteconfig', ~58x58 at top-right).
      Clicking the center doesn't always work — the menu only opens if you
      click the upper-left quadrant. maintainer: "the gear is weird. I had to click
      like on the upper left part of the gear menu to get it to work."

      After the left menu appears, click "Alarm Settings" (text in left sidebar,
      under "Device" group). It should be in the first visible position.
    """
    # Check if already on Alarm Settings page (config-tab.active = "Alarm Settings")
    already_there = page.evaluate("""
        (() => {
            const active = document.querySelector('.config-tab.active');
            return active && active.textContent.trim() === 'Alarm Settings';
        })()
    """)
    if already_there:
        return

    # Step 1: Find the system gear icon
    info = page.evaluate("""
        (() => {
            const el = document.getElementById('navigation_bar_remoteconfig');
            if (!el) return null;
            const box = el.getBoundingClientRect();
            return {x: box.x, y: box.y, w: box.width, h: box.height};
        })()
    """)
    if info:
        # Click upper-left part per maintainer's hint. Sometimes the click
        # misses on the first try — retry up to 3 times, verifying the
        # menu actually opens (look for "Device" header) between tries.
        for attempt in range(3):
            cx = info['x'] + info['w'] * 0.3
            cy = info['y'] + info['h'] * 0.3
            page.mouse.click(cx, cy)
            time.sleep(5)

            # Did the menu open?
            menu_open = page.evaluate("""
                (() => {
                    const all = Array.from(document.querySelectorAll('.item-title, [class*="config-item"]'));
                    return all.some(el => /Device|Surveillance|Network|Storage|System/i.test(el.textContent));
                })()
            """)
            if menu_open:
                break
            print(f"[read] gear click attempt {attempt+1} did not open menu, retrying", file=sys.stderr)

    # Step 2: Wait for "Alarm Settings" to be visible, then click
    page.locator("text=Alarm Settings").first.click(timeout=15000)
    time.sleep(3)


def read_all_inputs(page) -> list:
    """Enumerate every visible input/select/button on the Alarm Settings page."""
    return page.evaluate("""
        (() => {
            const all = Array.from(document.querySelectorAll('input, select'));
            return all.map(el => {
                const box = el.getBoundingClientRect();
                return {
                    tag: el.tagName,
                    type: el.type || '',
                    id: el.id,
                    cls: (el.className || '').toString().slice(0, 50),
                    value: el.value,
                    min: el.min,
                    max: el.max,
                    checked: el.checked,
                    x: Math.round(box.x), y: Math.round(box.y),
                    w: Math.round(box.width), h: Math.round(box.height),
                    visible: el.offsetParent !== null
                };
            }).filter(o => o.visible);
        })()
    """)


def read_visible_text(page) -> list:
    """Get all visible text on the page for context."""
    return page.evaluate("""
        (() => {
            const all = Array.from(document.querySelectorAll('*'));
            return all.map(el => {
                const box = el.getBoundingClientRect();
                if (box.width < 5) return null;
                const ownText = Array.from(el.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent.trim())
                    .join('');
                if (!ownText || ownText.length > 60) return null;
                return {
                    text: ownText,
                    tag: el.tagName,
                    cls: (el.className || '').toString().slice(0, 30),
                    x: Math.round(box.x), y: Math.round(box.y)
                };
            }).filter(o => o !== null);
        })()
    """)


def expand_section(page, label: str) -> bool:
    """Click a section header (Motion Detection / Smart Detection) to expand.
    Returns True if successful."""
    try:
        page.locator(f"text={label}").first.click()
        time.sleep(2.5)
        return True
    except Exception as e:
        print(f"[read] could not expand '{label}': {e}", file=sys.stderr)
        return False


def scroll_config_box(page, position: int) -> None:
    """Scroll the config-scroll-box container to a position.
    position: 0 = top, 99999 = bottom."""
    page.evaluate(f"""
        const el = document.querySelector('.config-scroll-box');
        if (el) el.scrollTop = {position};
    """)
    time.sleep(1.5)


def read_camera(ip: str, user: str, password: str, headless: bool = True) -> dict:
    """Read the Alarm Settings page on one camera. Returns dict."""
    result = {
        "ip": ip,
        "name": "",
        "login_ok": False,
        "motion_detection": {},
        "smart_detection": {},
        "alarm_delay": {},
        "object_size": {},
        "raw_inputs": [],
        "error": None,
    }

    with CamBrowser(headless=headless) as cb:
        try:
            cb.login(ip, user, password)
        except Exception as e:
            result["error"] = f"login failed: {e}"
            return result
        result["login_ok"] = True
        page = cb.page

        try:
            open_alarm_settings(page)
            time.sleep(2)

            # Expand Motion Detection AND Smart Detection so we see their sliders
            expand_section(page, "Motion Detection")
            time.sleep(2)
            expand_section(page, "Smart Detection")
            time.sleep(2)

            # Read inputs with section-aware labeling. Walk each input's
            # ancestor chain and label with the closest section header.
            scroll_config_box(page, 0)
            time.sleep(2)

            labeled = page.evaluate("""
                (() => {
                    const inputs = Array.from(document.querySelectorAll('input[type=range]'));
                    return inputs.map(inp => {
                        const box = inp.getBoundingClientRect();
                        // Discriminate sections by ancestor classes (verified 2026-07-26):
                        //   - Motion Detection:  ancestor is .new-sensitivity-box
                        //     (slider min=1, max=50; current val ~41 on 510A)
                        //   - Smart Detection:   ancestor is .smart-sensitivity-box
                        //     AND the .toogle-controller ancestor has
                        //     class 'toogle-controller show-detail' (NOT just
                        //     'sensitivity-box'). Slider min=0, max=100.
                        //   - Alarm Delay:       ancestor is .smart-sensitivity-box
                        //     AND the .toogle-controller ancestor has
                        //     class 'toogle-controller sensitivity-box'.
                        //     Slider min=0, max=8 (seconds).
                        //   - Volume/playback:   ancestor is .volume-box (ignore).
                        let section = 'unknown';
                        let el = inp;
                        while (el && el !== document.body) {
                            const cls = (el.className || '').toString();
                            if (cls.includes('new-sensitivity-box')) {
                                section = 'motion_detection';
                                break;
                            }
                            if (cls.includes('smart-sensitivity-box')) {
                                // Use both parent AND grandparent to disambiguate.
                                // Verified DOM hierarchy (RLC-510A, 2026-07-26):
                                //
                                // Smart Detection:
                                //   smart-sensitivity-box
                                //     → <div>          (no class)
                                //       → toogle-controller show-detail   ← GP has show-detail
                                //         → sensitivity-box
                                //
                                // Alarm Delay:
                                //   smart-sensitivity-box
                                //     → sensitivity-box                   ← parent has sensitivity-box
                                //       → <div>          (no class)
                                //         → alarm-wrap
                                //
                                // Discriminator: parent has .sensitivity-box ⇒ Alarm Delay
                                //               GP has .show-detail     ⇒ Smart Detection
                                const parent = el.parentElement;
                                const gp = parent ? parent.parentElement : null;
                                const parentCls = parent ? (parent.className || '').toString() : '';
                                const gpCls = gp ? (gp.className || '').toString() : '';
                                if (parentCls.includes('sensitivity-box')) {
                                    section = 'alarm_delay';
                                } else if (gpCls.includes('show-detail')) {
                                    section = 'smart_detection';
                                }
                                break;
                            }
                            el = el.parentElement;
                        }
                        // Detect icon (Person = running, Vehicle = car, Pet = paw)
                        const container = inp.closest('.process-slider, .smart-sensitivity-box, [class*="sensitivity"]') || inp.parentElement;
                        let icon = '';
                        if (container) {
                            const i = container.querySelector('i, [class*="icon"]');
                            if (i) icon = (i.className || '').toString();
                        }
                        return {
                            value: inp.value, min: inp.min, max: inp.max,
                            x: Math.round(box.x), y: Math.round(box.y),
                            section: section, icon: icon,
                            visible: box.width > 0 && box.height > 0
                        };
                    }).filter(o => o.visible);
                })()
            """)

            # Group by section
            for inp in labeled:
                if inp["section"] == "motion_detection":
                    result["motion_detection"].setdefault(
                        "sliders", []).append(inp)
                elif inp["section"] == "smart_detection":
                    result["smart_detection"].setdefault(
                        "sliders", []).append(inp)
                elif inp["section"] == "alarm_delay":
                    result["alarm_delay"].setdefault(
                        "sliders", []).append(inp)
                else:
                    result.setdefault("unknown_section", []).append(inp)

            # Object Size "Set Up" buttons
            scroll_config_box(page, 99999)
            time.sleep(1)
            buttons = page.evaluate("""
                (() => {
                    const all = Array.from(document.querySelectorAll('button'));
                    return all.map(b => ({
                        text: (b.textContent || '').trim(),
                        x: Math.round(b.getBoundingClientRect().x),
                        y: Math.round(b.getBoundingClientRect().y),
                        visible: b.offsetParent !== null
                    })).filter(o => o.visible && o.text === 'Set Up');
                })()
            """)
            result["object_size"]["set_up_buttons"] = buttons

        except Exception as e:
            result["error"] = f"read failed: {e}"
            return result

    return result


def print_human(camera_name: str, result: dict, *, recipe: dict | None = None) -> None:
    """Pretty-print one camera's result.

    Args:
        camera_name: human label for output
        result: dict from read_camera()
        recipe: optional dict of expected slider values (7-key RECIPE).
            When provided, each slider line shows (recipe: X) and an OK/✗ marker.
    """
    ip = result["ip"]
    print(f"\n{'='*72}")
    print(f"{camera_name}  ({ip})")
    print(f"{'='*72}")
    if not result["login_ok"]:
        print(f"  LOGIN FAILED: {result.get('error', '?')}")
        return

    md = result["motion_detection"]
    sd = result["smart_detection"]
    ad = result["alarm_delay"]
    os_ = result["object_size"]

    labels = ["person", "vehicle", "pet"]
    diff_count = 0  # for "ALL VALUES MATCH" summary at end

    print("\n  Motion Detection:")
    md_sliders = md.get("sliders", [])
    md_sliders.sort(key=lambda s: s["y"])
    for i, s in enumerate(md_sliders):
        expected = recipe.get("motion_sensitivity") if recipe else None
        marker = ""
        if expected is not None:
            ok = int(s["value"]) == int(expected)
            marker = " ✓" if ok else " ✗ DIFF"
            if not ok:
                diff_count += 1
            suffix = f" (recipe: {expected}){marker}"
        else:
            suffix = ""
        print(
            f"    slider_{i}: value={s['value']} min={s['min']} max={s['max']} "
            f"icon={s['icon']!r}{suffix}"
        )

    print("\n  Smart Detection:")
    sd_sliders = sd.get("sliders", [])
    sd_sliders.sort(key=lambda s: s["y"])
    for i, s in enumerate(sd_sliders):
        label = labels[i] if i < len(labels) else f"slider_{i}"
        key = f"smart_{label}" if label in labels else None
        expected = recipe.get(key) if (recipe and key) else None
        marker = ""
        if expected is not None:
            ok = int(s["value"]) == int(expected)
            marker = " ✓" if ok else " ✗ DIFF"
            if not ok:
                diff_count += 1
            suffix = f" (recipe: {expected}){marker}"
        else:
            suffix = ""
        print(
            f"    {label}: value={s['value']} (max={s['max']}) icon={s['icon']!r}{suffix}"
        )

    print("\n  Alarm Delay:")
    ad_sliders = ad.get("sliders", [])
    ad_sliders.sort(key=lambda s: s["y"])
    for i, s in enumerate(ad_sliders):
        label = labels[i] if i < len(labels) else f"slider_{i}"
        key = f"delay_{label}" if label in labels else None
        expected = recipe.get(key) if (recipe and key) else None
        marker = ""
        if expected is not None:
            ok = int(s["value"]) == int(expected)
            marker = " ✓" if ok else " ✗ DIFF"
            if not ok:
                diff_count += 1
            suffix = f" (recipe: {expected}){marker}"
        else:
            suffix = ""
        print(
            f"    {label}: value={s['value']}s (max={s['max']}) icon={s['icon']!r}{suffix}"
        )

    print(f"\n  Object Size: {len(os_.get('set_up_buttons', []))} Set Up buttons found")
    for btn in os_.get("set_up_buttons", []):
        print(f"    Set Up button at ({btn['x']}, {btn['y']})")

    if recipe is not None:
        if diff_count == 0:
            print("\n  ALL VALUES MATCH RECIPE.")
        else:
            print(f"\n  {diff_count} value(s) DIFFERS from recipe.")

    if result.get("error"):
        print(f"\n  ERROR: {result['error']}")


def main():
    ap = argparse.ArgumentParser(description=(__doc__ or "").split('\n')[0])
    ap.add_argument("ip", nargs="?",
                    help="[DEPRECATED — Phase 6B.167 §13.4] Single camera IP. "
                         "Use --camera <label> instead.")
    ap.add_argument("--camera", default=None,
                    help="Single camera filter. Accepts either the camera's friendly "
                         "label (from camera-creds.env) or an IP. "
                         "Phase 6B.167 §13.4: CAM1/CAM2 codes land in T2 Commit 5.")
    ap.add_argument("--list-cameras", action="store_true",
                    help="Print the configured camera label/ip/code list as JSON and exit.")
    ap.add_argument("--json", action="store_true", help="Output JSON")
    ap.add_argument("--headed", action="store_true", help="Show browser window")
    ap.add_argument("--label", default="",
                    help="Camera label override (used for per-camera recipe lookup). "
                         "If omitted, derives label from camera-creds.env camera_map.")
    # Phase 6B.166 §11.87.5 — recipe comparison opt-in
    ap.add_argument("--recipe", action="store_true",
                    help="Compare each slider to its expected recipe value "
                         "(from config/motion_recipe.json). Implies a per-camera "
                         "lookup; falls back to fleet values for cameras without "
                         "an override. Default: no comparison.")
    ap.add_argument("--recipe-path", default=None,
                    help="Override JSON recipe path (default: "
                         "infra.paths.MOTION_RECIPE_FILE).")
    args = ap.parse_args()

    # --- Camera list (from infra.camera_creds) ---
    from infra.camera_creds import (
        get_http_password,
        get_http_user,
        load_camera_creds,
    )
    from infra.paths import CAMERA_CREDS_FILE

    cameras: dict[str, dict[str, Any]] = load_camera_creds(str(CAMERA_CREDS_FILE))
    if not cameras:
        print(
            f"FATAL: no cameras found in {CAMERA_CREDS_FILE}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Phase 6B.167 §13.4: enumerate cameras in declaration order. The first
    # camera in camera-creds.env becomes CAM1, the second CAM2, etc. This
    # ordering is the contract that infra.cameras.py (T2 Commit 5) will formalize.
    ordered_cameras: list[tuple[str, str]] = [
        (label, info["ip"])
        for label, info in cameras.items()
        if info.get("ip")
    ]
    label_to_code: dict[str, str] = {
        label: f"CAM{i + 1}" for i, (label, _ip) in enumerate(ordered_cameras)
    }

    if args.list_cameras:
        # Phase 6B.167 §13.4: ergonomic helper. Prints the configured cameras
        # as a list of {code, label, ip} so operators can see what's wired up
        # and so tests can drive the script with synthetic fixtures.
        listing = [
            {"code": label_to_code[label], "label": label, "ip": ip}
            for label, ip in ordered_cameras
        ]
        print(json.dumps(listing, indent=2))
        sys.exit(0)

    # Single-camera filter: --camera wins, then positional `ip` (deprecated).
    filter_value: str | None = args.camera or args.ip
    if filter_value:
        # Try label match first, then IP match (camera labels can contain spaces).
        targets: list[tuple[str, str]] = []
        if filter_value in {label for label, _ip in ordered_cameras}:
            for label, ip in ordered_cameras:
                if label == filter_value:
                    targets.append((label, ip))
                    break
        else:
            for label, ip in ordered_cameras:
                if ip == filter_value:
                    targets.append((label, ip))
                    break
        if not targets:
            print(
                f"FATAL: no camera matches '{filter_value}' in {CAMERA_CREDS_FILE}. "
                f"Try --list-cameras to see what's configured.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        targets = list(ordered_cameras)

    # --- Recipe (only when --recipe) ---
    from infra.recipe import load_recipe
    from infra.recipe import resolve_for_camera as _resolve_for_camera
    recipe_raw: dict | None = None
    if args.recipe:
        try:
            recipe_raw = load_recipe(env_path=args.recipe_path)
        except Exception as e:
            print(
                f"FATAL: could not load JSON recipe ({type(e).__name__}: {e}). "
                f"Pass --recipe-path to point at a different file.",
                file=sys.stderr,
            )
            sys.exit(2)

    results = []
    for label, ip in targets:
        user = get_http_user(ip, env_path=str(CAMERA_CREDS_FILE)) or "admin"
        pw = get_http_password(ip, env_path=str(CAMERA_CREDS_FILE))
        if not pw:
            print(
                f"FATAL: no HTTP_PASS in camera-creds.env for IP {ip}",
                file=sys.stderr,
            )
            sys.exit(2)
        print(f"\n>>> reading {label} ({ip})...", file=sys.stderr)
        result = read_camera(ip, user, pw, headless=not args.headed)
        result["name"] = label
        # Attach the resolved recipe for this camera (only when --recipe)
        per_cam_recipe = None
        if recipe_raw is not None:
            # Use --label if given (override), else default to the camera's label
            lookup_label: str = args.label or label
            per_cam_recipe = _resolve_for_camera(
                label=lookup_label, recipe=recipe_raw
            )
        results.append(result)
        if not args.json:
            print_human(label, result, recipe=per_cam_recipe)

    if args.json:
        print(json.dumps(results, indent=2, default=str))

    # Exit code
    failed = [r for r in results if not r["login_ok"] or r.get("error")]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
