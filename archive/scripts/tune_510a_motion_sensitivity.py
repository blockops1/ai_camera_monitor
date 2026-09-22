#!/usr/bin/env python3
"""
tune_510a_motion_sensitivity.py — Read or apply the Reolink RLC-510A motion recipe.

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

The recipe applied / verified by this script:

| Section         | Slider     | Target | Notes                                |
|-----------------|------------|--------|---------------------------------------|
| Motion Detection| sensitivity | 15/50  | 1–50 scale, single period 24×7        |
| Smart Detection | Person     | 50/100 | 0–100 scale (AI confidence threshold)|
| Smart Detection | Vehicle    | 50/100 | (corrected from a prior doc's 30)     |
| Smart Detection | Pet        | 30/100 | low — no pets roam this property      |
| Alarm Delay     | Person     | 0s     | 0–8 sec scale                         |
| Alarm Delay     | Vehicle    | 0s     |                                       |
| Alarm Delay     | Pet        | 2s     | debounce — animals re-enter frames    |

Usage:
    # Read current state from one camera (non-destructive)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py <ip> --read

    # Apply recipe to one camera (CONFIRM target)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py <ip> --apply

    # Apply to all four 510As (CONFIRM via --confirm-all)
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --apply-all --confirm-all

    # Print just the recipe
    .venv/bin/python scripts/tune_510a_motion_sensitivity.py --recipe

Cameras on this property (all four 510As run firmware v3.2.0.5180_2507241368;
verified 2026-08-02 via ONVIF GetDeviceInformation):
    Outside Front Solar  — [REDACTED-LAN-IP]  (gatekeeper)
    Outside Front Garage — [REDACTED-LAN-IP]
    Outside Front Power  — [REDACTED-LAN-IP]
    Outside Back Solar   — [REDACTED-LAN-IP]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

# Load cam_browser from sibling directory
sys.path.insert(0, str(Path(__file__).parent))
from cam_browser import CamBrowser  # noqa: E402


# --- Recipe (single source of truth) ---
#
# Updated 2026-08-06 after the OFS false-positive / missed-departure
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
# Person + Pet sliders unchanged from 2026-08-02 recipe.
#
# Verification plan: apply to OFP first (Outside Front Power,
# [REDACTED-LAN-IP]), monitor 24–48h, then roll to OFS + the other two
# 510As. Do NOT touch detection zones — burned 2026-07-20.

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


# --- All four 510A cameras (verified 2026-08-02 firmware inventory) ---

CAMERAS_510A = {
    "Outside Front Solar":   "[REDACTED-LAN-IP]",
    "Outside Front Garage":  "[REDACTED-LAN-IP]",
    "Outside Front Power":   "[REDACTED-LAN-IP]",
    "Outside Back Solar":    "[REDACTED-LAN-IP]",
}


# --- Credential loading ---

CREDS_FILE = Path(__file__).parent.parent / "camera-creds.env"


def load_creds() -> dict:
    """Parse camera-creds.env into a {key: value} dict. Never `source` into venv python."""
    creds = {}
    if not CREDS_FILE.exists():
        raise FileNotFoundError(
            f"camera-creds.env not found at {CREDS_FILE}."
        )
    with open(CREDS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    return creds


def get_password_for(ip: str, creds: dict) -> Optional[str]:
    """Return the HTTP password for the camera at `ip`, or None if not in creds."""
    for k, v in creds.items():
        if k.endswith("_HTTP_PASS"):
            prefix = k[:-len("_HTTP_PASS")]
            ip_key = f"{prefix}_IP"
            if ip_key in creds and creds[ip_key] == ip:
                return v
    return None


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

        # Upper-LEFT quadrant (per Mr. V 2026-08-02)
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
                          index_in_group: int) -> Optional[dict]:
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


def apply_recipe(page) -> dict:
    """Apply RECIPE to the camera. Caller must have called open_alarm_settings(page)."""
    result: dict = {"motion": None, "smart_person": None, "smart_vehicle": None,
                    "smart_pet": None, "delay_person": None, "delay_vehicle": None,
                    "delay_pet": None}

    # Motion Detection — single slider
    motion = _find_slider_in_group(page, "Motion Detection", 50, 0)
    if not motion:
        return {**result, "error": "no Motion Detection slider found"}
    ok = _set_slider_in_group(page, "Motion Detection", 50, 0,
                              RECIPE["motion_sensitivity"])
    result["motion"] = (RECIPE["motion_sensitivity"], ok)

    # Smart Detection (Person, Vehicle, Pet by DOM order)
    for idx, key in enumerate(["smart_person", "smart_vehicle", "smart_pet"]):
        target = RECIPE[key]
        ok = _set_slider_in_group(page, "Smart Detection", 100, idx, target)
        result[key] = (target, ok)

    # Alarm Delay (Person, Vehicle, Pet by DOM order)
    for idx, key in enumerate(["delay_person", "delay_vehicle", "delay_pet"]):
        target = RECIPE[key]
        ok = _set_slider_in_group(page, "Alarm Delay", 8, idx, target)
        result[key] = (target, ok)

    return result


def compare_to_recipe(state: dict) -> tuple:
    """Return (matches, diffs). matches: bool. diffs: list of (key, actual, expected)."""
    diffs = []
    for key, expected in RECIPE.items():
        if key not in state:
            diffs.append((key, "MISSING", expected))
        elif state[key] != expected:
            diffs.append((key, state[key], expected))
    return len(diffs) == 0, diffs


# --- Camera session drivers ---

def do_read(ip: str, label: str) -> int:
    """Read current motion settings from one camera.

    Returns exit code: 0 = matches recipe, 1 = differs, 2 = error.
    """
    creds = load_creds()
    pw = get_password_for(ip, creds)
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
            for key in RECIPE.keys():
                v = state.get(key, "—")
                match = "✓" if v == RECIPE[key] else "✗"
                print(f"  {match} {key:25} = {v}  (recipe: {RECIPE[key]})")
            matches, diffs = compare_to_recipe(state)
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


def do_apply(ip: str, label: str) -> int:
    """Apply motion settings to one camera.

    Returns exit code: 0 = applied + verified, 1 = verify failed, 2 = apply error.
    """
    creds = load_creds()
    pw = get_password_for(ip, creds)
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
            result = apply_recipe(cb.page)
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
            matches, diffs = compare_to_recipe(state)
            print("\n  Re-read after apply:")
            for key in RECIPE.keys():
                v = state.get(key, "—")
                match = "✓" if v == RECIPE[key] else "✗"
                print(f"    {match} {key:25} = {v}  (expected {RECIPE[key]})")
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
        description="Read or apply motion sensitivity recipe on Reolink RLC-510A cameras.")
    parser.add_argument("ip", nargs="?",
                        help="Camera IP address (omit with --apply-all).")
    parser.add_argument("--read", action="store_true",
                        help="Read current state and compare to recipe (non-destructive).")
    parser.add_argument("--apply", action="store_true",
                        help="Apply the recipe to ONE camera. CONFIRM this is the intended target.")
    parser.add_argument("--apply-all", action="store_true",
                        help="Apply the recipe to all four 510As. Requires --confirm-all.")
    parser.add_argument("--confirm-all", action="store_true",
                        help="Required safety flag when using --apply-all.")
    parser.add_argument("--label", default="",
                        help="Human label for the camera (used in output).")
    parser.add_argument("--recipe", action="store_true",
                        help="Print the recipe and exit.")
    args = parser.parse_args()

    if args.recipe:
        print("Motion sensitivity recipe (applied 2026-08-02 across all 4 510As):")
        for k, v in RECIPE.items():
            print(f"  {k:25} = {v}")
        return 0

    if not args.ip and not args.apply_all:
        parser.print_help()
        print("\nCamera IP map:")
        for label, ip in CAMERAS_510A.items():
            print(f"  {label:25} {ip}")
        return 0

    if args.apply_all:
        if not args.confirm_all:
            print("ERROR: --apply-all requires --confirm-all as a safety flag.",
                  file=sys.stderr)
            return 2
        print("=== Applying recipe to all 4 RLC-510As ===")
        all_ok = True
        for label, ip in CAMERAS_510A.items():
            print(f"\n----- {label} ({ip}) -----")
            rc = do_apply(ip, label)
            if rc != 0:
                all_ok = False
        if all_ok:
            print("\n=== ALL 4 CAMERAS APPLIED SUCCESSFULLY ===")
            return 0
        else:
            print("\n=== One or more cameras had errors — inspect above ===",
                  file=sys.stderr)
            return 1

    # Single-camera path
    label = args.label or args.ip

    # Auto-derive label from the camera map if user didn't pass it
    for known_label, known_ip in CAMERAS_510A.items():
        if known_ip == args.ip:
            label = f"{known_label} ({args.ip})"
            break

    if args.read:
        return do_read(args.ip, label)
    elif args.apply:
        return do_apply(args.ip, label)
    else:
        # Default if just an IP is given: read
        return do_read(args.ip, label)


if __name__ == "__main__":
    sys.exit(main())
