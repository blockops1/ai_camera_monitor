"""Configure webhook step-by-step with screenshots at each phase."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cam_browser import CamBrowser

WEBHOOK_URL = "http://192.168.1.111:8090/alert"


def _load_creds(ip):
    cred_path = Path.home() / "farm-surveillance" / "camera-creds.env"
    creds = {}
    for line in cred_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            creds[k] = v.strip()
    if ip == "192.168.1.39":
        user = creds.get("FRONT_HTTP_USER", "admin")
        pw = creds.get("FRONT_HTTP_PASS", "")
    elif ip == "192.168.1.85":
        user = creds.get("BACK_HTTP_USER", "admin")
        pw = creds.get("BACK_HTTP_PASS", "")
    else:
        raise SystemExit(f"unknown IP: {ip}")
    if not pw:
        raise SystemExit(
            f"missing HTTP password for {ip} in {cred_path} "
            f"(need FRONT_HTTP_PASS or BACK_HTTP_PASS)"
        )
    return user, pw


def shot(cb, name):
    p = Path(f"/tmp/cam-{name}.png")
    cb.page.screenshot(path=str(p))
    print(f"  [shot] {p}")


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.39"
    user, pw = _load_creds(ip)
    print(f"=== Configuring webhook on {ip} ===")
    with CamBrowser(headless=False) as cb:
        # Login
        if not cb.login(ip, user, pw):
            print("LOGIN FAILED"); return
        time.sleep(3)
        shot(cb, "01-logged-in")

        # Settings gear
        cb.gear(); time.sleep(3)
        shot(cb, "02-settings")

        # Push tab
        cb.click_tab("Push"); time.sleep(3)
        shot(cb, "03-push")

        # Click Webhook sub-tab (button tag, class gray-button)
        cb.evaluate("""
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const wh = btns.find(b => b.textContent.trim() === 'Webhook' && b.offsetParent !== null);
                if (wh) wh.click();
                return wh ? 'clicked' : 'not found';
            })()
        """)
        time.sleep(3)
        shot(cb, "04-webhook-tab")

        # Click Add
        cb.evaluate("""
            (() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const add = btns.find(b => b.textContent.trim() === 'Add' && b.offsetParent !== null);
                if (add) add.click();
                return add ? 'clicked' : 'not found';
            })()
        """)
        time.sleep(3)
        shot(cb, "05-add-clicked")

        # Fill URL
        result = cb.evaluate(f"""
            (() => {{
                const inp = document.querySelector('input.button-text.label-input');
                if (!inp) return 'NO_INPUT';
                inp.focus();
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, '{WEBHOOK_URL}');
                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                inp.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', bubbles: true }}));
                return 'OK: ' + inp.value;
            }})()
        """)
        print(f"  fill URL: {result}")
        time.sleep(2)
        shot(cb, "06-url-filled")

        # Examine all save buttons in detail before clicking
        print("  Save buttons before click:")
        print("  " + cb.evaluate("""
            (() => JSON.stringify(Array.from(document.querySelectorAll('button.save-button')).map((b, i) => ({
                idx: i, text: b.textContent.trim(), disabled: b.disabled, visible: b.offsetParent !== null,
                rect: b.getBoundingClientRect()
            }))))
        """).replace("\n", "\n  "))

        # Click the LAST save-button (the webhook form's Save, since the alarm-section Save is rendered first)
        result = cb.evaluate("""
            (() => {
                const btns = Array.from(document.querySelectorAll('button.save-button'))
                    .filter(b => b.offsetParent !== null && !b.disabled && b.textContent.trim() === 'Save');
                if (btns.length === 0) return 'NO_ENABLED_SAVE';
                // Click the LAST one (most likely the webhook form's save, which is appended after the alarm section)
                const target = btns[btns.length - 1];
                const rect = target.getBoundingClientRect();
                target.click();
                return JSON.stringify({clicked_idx: btns.length - 1, total: btns.length, rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height}});
            })()
        """)
        print(f"  Save click result: {result}")
        time.sleep(4)
        shot(cb, "07-save-clicked")

        # Final verification — dump all visible text after save
        print("\n=== POST-SAVE BODY ===")
        print(cb.snapshot_text()[-1500:])
        shot(cb, "08-final")


if __name__ == "__main__":
    main()
