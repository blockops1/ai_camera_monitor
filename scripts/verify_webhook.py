#!/usr/bin/env python3
"""
verify_webhook.py — Re-open the Push → Webhook page on a camera and report what's there.

Phase 6B.166 §11.87.6: cameras and credentials come from infra.camera_creds
(the canonical source — reads <repo>/camera-creds.env). The IP defaults to
the first camera in the creds file (replaces the old hardcoded FRONT).

Primarily a debugging tool — dumps tab-button state and visible page text
to help diagnose "the webhook form isn't appearing" issues.

[STATUS / module header sections follow below — see refactor-module-header skill]

STATUS: stable
THREAD SAFETY: single-threaded (drives a headless Chromium browser)

INPUTS:
    - camera-creds.env (via infra.camera_creds) — source of camera list
    - CLI flags: [ip], --creds-env PATH
    - Env var: VERIFY_WEBHOOK_DEFAULT_IP (optional override)

OUTPUTS:
    - prints tab-button dump, attempt result, after-click snapshot,
      and current button list to stdout
    - exit codes: 0 = ran successfully, 2 = fatal config error

PUBLIC API:
    verify_webhook(ip: str, user: str, password: str, *, headed: bool = False)
        -> None
        Drive the browser through webhook verification on one camera.

DOES NOT DO:
    - Configure a webhook — that's configure_webhook.py.
    - Apply recipe to motion/smart/alarm-delay sliders.
    - Persist anything — purely a debug/inspection script.

CALLED BY:
    - operator shell: scripts/verify_webhook.py --camera <label|ip>  # single camera

Camera labels and IPs are sourced from camera-creds.env via infra.camera_creds.
Phase 6B.167 §13.4: positional `ip` arg deprecated; use `--camera <label>`
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
from pathlib import Path

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).parent))
from cam_browser import CamBrowser


def verify_webhook(ip: str, user: str, password: str, *, headed: bool = False) -> None:
    with CamBrowser(headless=not headed) as cb:
        if not cb.login(ip, user, password): return
        time.sleep(3)
        cb.gear(); time.sleep(3)
        cb.click_tab("Push"); time.sleep(3)
        # Click Webhook sub-tab
        cb.evaluate("""
            (() => {
                const tabs = Array.from(document.querySelectorAll('.tab-button, .tab-item'));
                const wh = tabs.find(t => t.textContent.trim() === 'Webhook');
                if (wh) wh.click();
            })()
        """)
        time.sleep(3)
        # Dump webhook & alarm tab buttons with their state
        print("=== TAB BUTTONS ===")
        print(cb.evaluate("""
            (() => JSON.stringify(Array.from(document.querySelectorAll('.tab-button, .gray-button')).filter(e => e.offsetParent !== null).map(e => ({
                text: e.textContent.trim().slice(0, 30),
                cls: e.className,
                disabled: e.disabled || e.classList.contains('disabled'),
                style: window.getComputedStyle(e).cssText.slice(0, 100)
            }))))
        """))
        # Try clicking the Webhook tab again with explicit selector
        print("=== ATTEMPT WEBHOOK TAB CLICK ===")
        result = cb.evaluate("""
            (() => {
                const results = [];
                // Try each tab-button by text
                const tabs = Array.from(document.querySelectorAll('.tab-button'));
                results.push('tab-button count: ' + tabs.length);
                const wh = tabs.find(t => t.textContent.trim() === 'Webhook');
                if (wh) {
                    results.push('found Webhook tab-button, class: ' + wh.className);
                    wh.click();
                    return results;
                }
                // Try by span text inside button
                const allBtns = Array.from(document.querySelectorAll('button'));
                const whBtn = allBtns.find(b => b.textContent.trim() === 'Webhook' && b.offsetParent !== null);
                if (whBtn) {
                    results.push('found Webhook via button tag, class: ' + whBtn.className);
                    whBtn.click();
                    return results;
                }
                return results.concat('not found');
            })()
        """)
        print(result)
        time.sleep(3)
        print("=== AFTER CLICK ===")
        print(cb.snapshot_text()[-1200:])
        print("=== BUTTONS ===")
        print(cb.evaluate("""
            (() => JSON.stringify(Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null).map(b => ({
                text: b.textContent.trim().slice(0, 30), cls: b.className.slice(0, 60)
            }))))
        """))


def main():
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").split('\n')[0],
    )
    ap.add_argument("ip", nargs="?",
                    help="[DEPRECATED — Phase 6B.167 §13.4] Camera IP. "
                         "Use --camera <label> instead.")
    ap.add_argument("--camera", default=None,
                    help="Single camera filter. Accepts either the camera's "
                         "friendly label (from camera-creds.env) or an IP. "
                         "Default: first camera in camera-creds.env (or "
                         "$VERIFY_WEBHOOK_DEFAULT_IP for backwards compat).")
    ap.add_argument("--list-cameras", action="store_true",
                    help="Print {code, label, ip} for all configured cameras as JSON and exit.")
    ap.add_argument("--headed", action="store_true", help="Show browser window")
    ap.add_argument("--creds-env", default=None,
                    help="Override camera-creds.env path (default: infra.paths.CAMERA_CREDS_FILE)")
    args = ap.parse_args()

    import json
    from infra.camera_creds import get_http_password, get_http_user, load_camera_creds
    from infra.paths import CAMERA_CREDS_FILE

    creds_env = args.creds_env or str(CAMERA_CREDS_FILE)
    cameras = load_camera_creds(creds_env)

    # Phase 6B.167 §13.4: enumerate cameras in declaration order. CAM1 = first,
    # CAM2 = second, etc. (contract formalized by infra/cameras.py in T2 Commit 5).
    ordered_cameras: list[tuple[str, str]] = [
        (label, info["ip"])
        for label, info in cameras.items()
        if info.get("ip")
    ]
    label_to_code: dict[str, str] = {
        label: f"CAM{i + 1}" for i, (label, _ip) in enumerate(ordered_cameras)
    }

    if args.list_cameras:
        listing = [
            {"code": label_to_code[label], "label": label, "ip": ip}
            for label, ip in ordered_cameras
        ]
        print(json.dumps(listing, indent=2))
        sys.exit(0)

    filter_value: str | None = args.camera or args.ip
    if filter_value:
        # Try label match first, then IP match.
        ip: str | None = None
        for label, cip in ordered_cameras:
            if label == filter_value or cip == filter_value:
                ip = cip
                break
        if ip is None:
            print(
                f"ERROR: no camera matches '{filter_value}' in {creds_env}. "
                f"Try --list-cameras to see what's configured.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        # Default: first camera in the creds file (or VERIFY_WEBHOOK_DEFAULT_IP env var)
        env_default = os.environ.get("VERIFY_WEBHOOK_DEFAULT_IP", "").strip()
        if env_default:
            ip = env_default
        else:
            ip = ordered_cameras[0][1] if ordered_cameras else None
        if ip is None:
            print(f"ERROR: no cameras configured in {creds_env}", file=sys.stderr)
            sys.exit(2)

    user = get_http_user(ip, creds_env)
    password = get_http_password(ip, creds_env)
    if not password or not user:
        print(f"ERROR: missing HTTP_USER/HTTP_PASS for {ip} in {creds_env}", file=sys.stderr)
        sys.exit(2)

    verify_webhook(ip, user, password, headed=args.headed)


if __name__ == "__main__":
    main()
