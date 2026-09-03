#!/usr/bin/env python3
"""
configure_webhook.py — Configure webhook URL on a Reolink camera via the persistent browser.

Phase.166 §11.87.6: cameras and credentials come from infra.camera_creds
(the canonical source — reads <repo>/camera-creds.env). Webhook URL is
configurable via --webhook-url or WEBHOOK_URL env var.

Form structure (verified 2026-07-18 on RLC-833A):
  - Surveillance > Push > Webhook > [Add]
  - Form: Content (Default/Customize select) | URL (text input) | Test | Cancel | Save
  - Default payload is Reolink's stock JSON shape.
  - Save is disabled until URL is non-empty.

[STATUS / module header sections follow below — see refactor-module-header skill]

STATUS: stable
THREAD SAFETY: single-threaded (drives a headless Chromium browser)

INPUTS:
    - camera-creds.env (via infra.camera_creds) — source of camera list
    - CLI flags: [ip], --all, --headed, --webhook-url URL, --creds-env PATH
    - Env var: WEBHOOK_URL (overridden by --webhook-url)

OUTPUTS:
    - per-camera dict {ip, steps, ok, webhook_visible_in_ui, error?}
    - exit codes: 0 = all configured, 1 = at least one failed, 2 = fatal
      config error (no creds file, unknown IP, no webhook URL)

PUBLIC API:
    configure_webhook(ip: str, user: str, password: str, webhook_url: str,
                      *, headed: bool = False) -> dict
        Drive the browser through the webhook config flow on one camera.
        Returns the state dict (ok=True on success).

DOES NOT DO:
    - Verify an existing webhook — that's verify_webhook.py.
    - Apply recipe to motion/smart/alarm-delay sliders — that's
      apply_all_tuning.py and tune_510a_motion_sensitivity.py.
    - Edit camera-creds.env — that's a setup operation.

CALLED BY:
    - operator shell: scripts/configure_webhook.py --camera <label|ip>
    - operator shell: scripts/configure_webhook.py --all

Camera labels and IPs are sourced from camera-creds.env via infra.camera_creds.
Phase.167 §13.4: positional `ip` arg deprecated; use `--camera <label>`
(or `--camera CAMn` once infra.cameras.py lands in T2 Commit 5).

CALLS INTO:
    - infra.camera_creds: load_camera_creds, get_http_user, get_http_password
    - infra.paths: CAMERA_CREDS_FILE
    - cam_browser.CamBrowser
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).parent))
from cam_browser import CamBrowser

# Webhook endpoint. Resolved at runtime from --webhook-url flag > $WEBHOOK_URL
# env var > empty (operator MUST set one of these — see main()).
#
# Phase.167 §13.4: removed the hardcoded operator listener IP that used to
# live here. Webhook endpoint is per-deploy config; not a project default.
_DEFAULT_WEBHOOK_URL = ""  # empty = require --webhook-url or $WEBHOOK_URL


def _get_webhook_url(args: argparse.Namespace) -> str:
    """Resolve webhook URL: CLI flag > env var > hardcoded default."""
    if args.webhook_url:
        return args.webhook_url
    env_url = os.environ.get("WEBHOOK_URL", "").strip()
    if env_url:
        return env_url
    return _DEFAULT_WEBHOOK_URL


def configure_webhook(
    ip: str,
    user: str,
    password: str,
    webhook_url: str,
    *,
    headed: bool = False,
) -> dict:
    """Drive the browser through webhook config on one camera."""
    state: dict[str, Any] = {"ip": ip, "steps": [], "ok": False}
    with CamBrowser(headless=not headed) as cb:
        if not cb.login(ip, user, password):
            state["error"] = "login failed"; return state
        state["steps"].append("login")
        time.sleep(2)

        cb.gear()
        state["steps"].append("gear")
        time.sleep(2)

        cb.click_tab("Push")
        state["steps"].append("Push tab")
        time.sleep(2)

        # Click "Webhook" sub-tab (sub-tab inside Push page)
        wh_tab = cb.evaluate("""
            (() => {
                // Webhook is a tab-button (sub-tab). Find by text.
                const tabs = Array.from(document.querySelectorAll('.tab-button, .tab-item'));
                const wh = tabs.find(t => t.textContent.trim() === 'Webhook');
                if (wh) { wh.click(); return true; }
                // Fallback: any visible element with text 'Webhook'
                const all = Array.from(document.querySelectorAll('*')).filter(e =>
                    e.offsetParent !== null && e.textContent.trim() === 'Webhook' && e.tagName !== 'BODY');
                if (all.length > 0) { all[0].click(); return true; }
                return false;
            })()
        """)
        if not wh_tab:
            state["error"] = "Webhook sub-tab not found"; return state
        state["steps"].append("Webhook sub-tab")
        time.sleep(2)

        # Click Add
        add_clicked = cb.evaluate("""
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const add = btns.find(b => b.textContent.trim() === 'Add' && b.offsetParent !== null);
                if (add) { add.click(); return true; }
                return false;
            })()
        """)
        if not add_clicked:
            state["error"] = "Add button not found"; return state
        state["steps"].append("Add clicked")
        time.sleep(2)

        # Fill URL field — the only text input with placeholder 'Type Here'
        # Use single quotes around URL to avoid f-string brace conflict in JS
        url_filled = cb.evaluate(f"""
            (() => {{
                const inp = document.querySelector('input.button-text.label-input');
                if (!inp) return 'no URL input found';
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, '{webhook_url}');
                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return 'filled: ' + inp.value;
            }})()
        """)
        if "no URL" in url_filled:
            state["error"] = url_filled; return state
        state["steps"].append(url_filled)
        time.sleep(1)

        # Click Save — there are two Save buttons (alarm section + webhook form).
        # The webhook form's Save is rendered LATER in the DOM, so pick the last one.
        save_clicked = cb.evaluate("""
            (() => {
                const btns = Array.from(document.querySelectorAll('button.save-button'))
                    .filter(b => b.offsetParent !== null && !b.disabled && b.textContent.trim() === 'Save');
                if (btns.length === 0) return false;
                btns[btns.length - 1].click();
                return true;
            })()
        """)
        if not save_clicked:
            state["error"] = "Save button disabled or not found"; return state
        state["steps"].append("Save clicked")
        time.sleep(3)

        # Verify: read back the webhook list
        # Match the host:port portion of the URL, not the full path
        match_str = webhook_url.split("//", 1)[-1].split("/", 1)[0]  # host:port
        webhook_list = cb.evaluate(f"""
            (() => {{
                const all = Array.from(document.querySelectorAll('*'));
                const matches = all.filter(e => e.offsetParent !== null &&
                    (e.textContent || '').includes('{match_str}'));
                return matches.map(e => e.tagName + ': ' + e.textContent.trim().slice(0, 100)).slice(0, 5);
            }})()
        """)
        state["webhook_visible_in_ui"] = webhook_list
        state["ok"] = bool(webhook_list)
        return state


def main():
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").split('\n')[0],
    )
    ap.add_argument("ip", nargs="?",
                    help="[DEPRECATED — Phase.167 §13.4] Single camera IP. "
                         "Use --camera <label> instead.")
    ap.add_argument("--camera", default=None,
                    help="Single camera filter. Accepts either the camera's "
                         "friendly label (from camera-creds.env) or an IP. "
                         "Phase.167 §13.4: CAM1/CAM2 codes land in T2 Commit 5.")
    ap.add_argument("--all", action="store_true",
                    help="Configure webhook on all cameras in camera-creds.env")
    ap.add_argument("--list-cameras", action="store_true",
                    help="Print {code, label, ip} for all configured cameras as JSON and exit.")
    ap.add_argument("--headed", action="store_true",
                    help="Show browser window")
    ap.add_argument("--webhook-url", default="",
                    help="Webhook URL (default: $WEBHOOK_URL; required if neither set)")
    ap.add_argument("--creds-env", default=None,
                    help="Override camera-creds.env path (default: infra.paths.CAMERA_CREDS_FILE)")
    args = ap.parse_args()

    # Imports inside main so module import stays cheap for tests
    import json
    from infra.camera_creds import get_http_password, get_http_user, load_camera_creds
    from infra.paths import CAMERA_CREDS_FILE

    creds_env = args.creds_env or str(CAMERA_CREDS_FILE)
    cameras = load_camera_creds(creds_env)

    # Phase.167 §13.4: enumerate cameras in declaration order.
    ordered_cameras: list[tuple[str, dict]] = [
        (label, info) for label, info in cameras.items() if info.get("ip")
    ]
    label_to_code: dict[str, str] = {
        label: f"CAM{i + 1}" for i, (label, _info) in enumerate(ordered_cameras)
    }

    if args.list_cameras:
        listing = [
            {"code": label_to_code[label], "label": label, "ip": info["ip"]}
            for label, info in ordered_cameras
        ]
        print(json.dumps(listing, indent=2))
        sys.exit(0)

    if not args.camera and not args.ip and not args.all:
        ap.error("provide --camera <label>, --all, or an IP (positional `ip` is deprecated)")

    if args.all:
        targets = list(ordered_cameras)
    else:
        filter_value = args.camera or args.ip
        match: tuple[str, dict] | None = None
        for label, info in ordered_cameras:
            if label == filter_value or info["ip"] == filter_value:
                match = (label, info)
                break
        if match is None:
            print(
                f"ERROR: no camera matches '{filter_value}' in {creds_env}. "
                f"Try --list-cameras to see what's configured.",
                file=sys.stderr,
            )
            sys.exit(2)
        targets = [match]

    webhook_url = _get_webhook_url(args)
    if not webhook_url:
        print("ERROR: --webhook-url or WEBHOOK_URL env var required", file=sys.stderr)
        sys.exit(2)

    print(f"Webhook URL: {webhook_url}", file=sys.stderr)
    print(f"Targets: {[label for label, _ in targets]}", file=sys.stderr)

    any_failed = False
    for label, info in targets:
        ip = info["ip"]
        user = get_http_user(ip, creds_env)
        password = get_http_password(ip, creds_env)
        if not password or not user:
            print(f"  ERROR: missing HTTP_USER/HTTP_PASS for {ip} ({label})",
                  file=sys.stderr)
            any_failed = True
            continue
        print(f"\n=== {label} ({ip}) ===")
        try:
            result = configure_webhook(
                ip, user, password, webhook_url, headed=args.headed,
            )
            for k, v in result.items():
                print(f"  {k}: {v}")
            if not result.get("ok"):
                any_failed = True
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            traceback.print_exc()
            any_failed = True

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
