#!/usr/bin/env python3
"""
verify_webhook.py — Re-open the Push → Webhook page on a camera and report what's there.

Phase.166 §11.87.6: cameras and credentials come from infra.camera_creds
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
    - operator shell: scripts/verify_webhook.py 192.168.1.39

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
    ap.add_argument("ip", nargs="?", help="Camera IP (default: first camera in camera-creds.env)")
    ap.add_argument("--headed", action="store_true", help="Show browser window")
    ap.add_argument("--creds-env", default=None,
                    help="Override camera-creds.env path (default: infra.paths.CAMERA_CREDS_FILE)")
    args = ap.parse_args()

    from infra.camera_creds import get_http_password, get_http_user, load_camera_creds
    from infra.paths import CAMERA_CREDS_FILE

    creds_env = args.creds_env or str(CAMERA_CREDS_FILE)
    cameras = load_camera_creds(creds_env)

    if args.ip:
        # Validate the IP is in the creds file
        ip = args.ip
        if not any(info["ip"] == ip for info in cameras.values()):
            print(f"ERROR: IP {ip} not found in {creds_env}", file=sys.stderr)
            print(f"Known IPs: {sorted({i['ip'] for i in cameras.values()})}", file=sys.stderr)
            sys.exit(2)
    else:
        # Default: first camera in the creds file (or VERIFY_WEBHOOK_DEFAULT_IP env var)
        env_default = os.environ.get("VERIFY_WEBHOOK_DEFAULT_IP", "").strip()
        if env_default:
            ip = env_default
        else:
            ip = next(iter(cameras.values()))["ip"]

    user = get_http_user(ip, creds_env)
    password = get_http_password(ip, creds_env)
    if not password or not user:
        print(f"ERROR: missing HTTP_USER/HTTP_PASS for {ip} in {creds_env}", file=sys.stderr)
        sys.exit(2)

    verify_webhook(ip, user, password, headed=args.headed)


if __name__ == "__main__":
    main()
