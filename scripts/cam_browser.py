"""
Persistent Reolink camera browser session.

Why this exists:
- The Reolink web UI sessions expire after ~30s of inactivity.
- Re-opening the browser via browser_navigate forces a fresh login each time.
- Camera config work involves many sequential clicks (login → gear → menu → tab →
  expand panel → set values → save), and getting logged out mid-flow kills the work.

Solution: a Playwright-driven Chrome instance with a persistent user-data-dir.
The browser stays alive across invocations; cookies persist between calls.
We expose a small CLI for the common operations.

Usage:
    from cam_browser import CamBrowser
    with CamBrowser() as cb:
        cb.login("192.168.1.39", "admin", "pass...")
        cb.gear()
        cb.click_tab("Push")
        cb.click("Webhook")
        ...

CLI (one-shot operations):
    python3 cam_browser.py login 192.168.1.39
    python3 cam_browser.py goto 192.168.1.39 /cgi-bin/...
    python3 cam_browser.py exec "document.title"

Phase.166 §11.87.2 (2026-08-30):
  - CHROME_PATH now reads from infra.paths.BROWSER_CHROME_PATH
    (env var BROWSER_CHROME_PATH), defaults to system Chrome.
  - CLI login() resolves HTTP_USER / HTTP_PASS by IP via
    infra.camera_creds.get_http_user / get_http_password — replaces
    the hardcoded {if ip == 192.168.1.39: FRONT_HTTP_PASS} / {elif
    192.168.1.85: BACK_HTTP_PASS} branching that broke for every
    other camera.
  - env_path defaults to infra.paths.CAMERA_CREDS_FILE — was hardcoded
    to <legacy-repo>/camera-creds.env which broke
    in the refactor (which lives at ~/ai_camera_monitor).
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

# --- Config (Phase.166 §11.87.2) ---
# PROFILE_DIR stays as-is (user-level cache, not project-tied).
PROFILE_DIR = Path.home() / ".cache" / "reolink-browser-profile"

# CHROME_PATH now reads from infra.paths.BROWSER_CHROME_PATH (env var
# BROWSER_CHROME_PATH). Default falls back to the system Chrome binary.
# Import is module-level so the value is computed once at startup;
# scripts that need to override should set BROWSER_CHROME_PATH in the
# environment before importing cam_browser.
from infra.paths import BROWSER_CHROME_PATH as _BROWSER_CHROME_PATH

CHROME_PATH = _BROWSER_CHROME_PATH


class CamBrowser:
    """
    Persistent Chrome session for Reolink camera config.

    Profile dir holds cookies + localStorage. Reuse across calls to stay logged in.
    Pass headless=False during debugging to see what's happening.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> CamBrowser:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        # Try system Chrome first (best canvas/Reolink UI support).
        # Fall back to bundled chromium-headless-shell if Chrome is missing
        # (installed by `playwright install chromium`). Verified 2026-07-22.
        try:
            self._browser = self._playwright.chromium.launch(
                channel="chrome",
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        except Exception as chrome_err:
            # Chrome not installed — fall back to bundled chromium.
            print(f"[cam_browser] system Chrome unavailable ({chrome_err}); using bundled chromium")
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        # Persistent context — cookies survive between sessions
        self._context = self._browser.new_context(
            viewport={"width": 1600, "height": 1000},
        )
        # Try to load cookies from a saved state file if it exists
        state_file = PROFILE_DIR / "state.json"
        if state_file.exists():
            try:
                self._context.add_cookies(_load_cookies(state_file))
                print(f"[cam_browser] loaded {len(_load_cookies(state_file))} cookies", file=sys.stderr)
            except Exception as e:
                print(f"[cam_browser] cookie load failed: {e}", file=sys.stderr)
        self._page = self._context.new_page()
        return self

    def __exit__(self, *args):
        # Save cookies before exit so next launch can reuse them
        try:
            state_file = PROFILE_DIR / "state.json"
            cookies = self._require_context().cookies()
            _save_cookies(cookies, state_file)
        except Exception as e:
            print(f"[cam_browser] cookie save failed: {e}", file=sys.stderr)
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    # --- High-level navigation helpers ---

    def _require_page(self) -> Page:
        """Return the active Playwright page, asserting it was initialized.

        All public methods that touch the browser call this first so that
        pyright can see _page as a non-None Page (it is None only between
        object construction and login() running).
        """
        assert self._page is not None, "CamBrowser used before login()"
        return self._page

    def _require_context(self) -> BrowserContext:
        """Return the active Playwright context, asserting it was initialized."""
        assert self._context is not None, "CamBrowser used before login()"
        return self._context

    @property
    def page(self) -> Page:
        return self._require_page()

    def goto(self, url: str) -> Page:
        """Navigate to URL. Auto-retries on timeout."""
        page = self._require_page()
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                return page
            except Exception as e:
                last_err = e
                print(f"[cam_browser] goto retry {attempt+1}: {e}", file=sys.stderr)
                time.sleep(2)
        assert last_err is not None  # loop ran 3 times; last_err must be set
        raise last_err

    def login(self, ip: str, username: str, password: str) -> bool:
        """Log into camera. Returns True if login UI was visible (already or just done)."""
        url = f"http://{ip}/"
        self.goto(url)
        time.sleep(2)

        # Check if already logged in (no login form)
        login_btn = self._require_page().query_selector('button:has-text("Login")')
        if login_btn is None:
            print(f"[cam_browser] {ip}: already logged in (no Login button visible)", file=sys.stderr)
            return True

        # Some cameras (e.g. back camera) require the privacy policy checkbox
        # Note: just clicking checkboxes unconditionally works because the camera's
        # handler fires on any click event in the checkbox tree (visibility check
        # actually filtered out the right one in some builds).
        pp_clicked = self._require_page().evaluate("""
            (() => {
                let clicked = 0;
                document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                    if (!cb.checked) { cb.click(); clicked++; }
                });
                return clicked;
            })()
        """)
        if pp_clicked:
            print(f"[cam_browser] {ip}: clicked {pp_clicked} checkbox(es)", file=sys.stderr)
            time.sleep(0.5)

        # Fill password (username is usually prefilled as "admin")
        pwd_input = self._require_page().query_selector('input[type="password"]')
        if pwd_input is None:
            print(f"[cam_browser] {ip}: password input not found", file=sys.stderr)
            return False

        # Clear and type
        pwd_input.click()
        pwd_input.fill(password)
        time.sleep(0.5)

        # Click Login
        login_btn = self._require_page().query_selector('button:has-text("Login")')
        if login_btn is None:
            print(f"[cam_browser] {ip}: login button disappeared", file=sys.stderr)
            return False

        # Check if login button is still disabled (privacy policy not accepted?)
        is_disabled = login_btn.is_disabled()
        if is_disabled:
            print(f"[cam_browser] {ip}: login button disabled, retrying with PP checkbox", file=sys.stderr)
            # Try clicking ALL visible unchecked checkboxes
            self._require_page().evaluate("""
                (() => {
                    document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                        if (cb.offsetParent !== null && !cb.checked) cb.click();
                    });
                })()
            """)
            time.sleep(0.5)
            login_btn = self._require_page().query_selector('button:has-text("Login")')
            is_disabled = login_btn.is_disabled() if login_btn else True

        if is_disabled:
            print(f"[cam_browser] {ip}: login button still disabled", file=sys.stderr)
            return False

        if login_btn is None:
            print(f"[cam_browser] {ip}: login button not found after privacy click", file=sys.stderr)
            return False
        login_btn.click()
        # Wait for the preview UI to appear (no Login button means logged in)
        try:
            self._require_page().wait_for_selector('button:has-text("Login")', state="hidden", timeout=10_000)
            print(f"[cam_browser] {ip}: logged in", file=sys.stderr)
            return True
        except Exception:
            print(f"[cam_browser] {ip}: login may have failed (Login button still visible)", file=sys.stderr)
            return False

    def gear(self) -> None:
        """Click the device settings gear (top-right of preview).

        The Reolink top nav has links: Preview, Playback, Device Settings (gear), Logout.
        The gear has title='Device Settings' and class 'func-btns'.
        """
        clicked = self._require_page().evaluate("""
            (() => {
                // Primary: find by title='Device Settings'
                const gear = document.querySelector('a[title="Device Settings"]');
                if (gear) {
                    gear.click();
                    return 'title selector';
                }
                // Fallback: by class
                const fallback = document.querySelector('a.func-btns:not(.logout-btn)');
                if (fallback) {
                    fallback.click();
                    return 'class selector';
                }
                return null;
            })()
        """)
        if not clicked:
            raise RuntimeError("could not find settings gear")
        time.sleep(3)

    def click_tab(self, tab_name: str) -> None:
        """Click a config tab by visible text (Push, Network, etc.)."""
        # Reolink uses li.config-tab elements
        clicked = self._require_page().evaluate(f"""
            (() => {{
                const tabs = Array.from(document.querySelectorAll('li.config-tab'));
                const t = tabs.find(li => li.textContent.trim() === '{tab_name}');
                if (t) {{ t.click(); return true; }}
                return false;
            }})()
        """)
        if not clicked:
            raise RuntimeError(f"tab not found: {tab_name}")
        time.sleep(2)

    def click(self, selector: str, timeout_ms: int = 5000) -> None:
        """Click an element matching the selector."""
        self._require_page().click(selector, timeout=timeout_ms)
        time.sleep(1)

    def click_text(self, text: str, exact: bool = True, timeout_ms: int = 5000) -> bool:
        """Click an element with matching visible text. Returns True if clicked."""
        if exact:
            sel = f'text="{text}"'
        else:
            sel = f'text="{text}"'
        try:
            self._require_page().click(sel, timeout=timeout_ms)
            time.sleep(1)
            return True
        except Exception:
            return False

    def fill(self, selector: str, value: str) -> None:
        """Fill an input."""
        el = self._require_page().query_selector(selector)
        if el is None:
            raise RuntimeError(f"input not found: {selector}")
        el.click()
        el.fill(value)
        time.sleep(0.5)

    def evaluate(self, expression: str):
        """Run JS in page context. Returns the result."""
        return self._require_page().evaluate(expression)

    def wait_for_text(self, text: str, timeout_ms: int = 8000) -> bool:
        try:
            self._require_page().wait_for_selector(f'text="{text}"', timeout=timeout_ms)
            return True
        except Exception:
            return False

    def snapshot_text(self) -> str:
        """Return the visible text of the page (truncated)."""
        return self._require_page().inner_text("body")[:2000]


# --- Cookie persistence helpers ---

def _save_cookies(cookies, path: Path):
    import json
    path.write_text(json.dumps(cookies, indent=2))


def _load_cookies(path: Path):
    import json
    return json.loads(path.read_text())


# --- CLI ---

def _cli_login(args):
    """CLI: log into a camera using creds from camera-creds.env.

    Replaces the Phase-6B.166-§11.87.1 hardcoded FRONT_HTTP_PASS /
    BACK_HTTP_PASS branching. Now resolves HTTP user + password by IP
    via infra.camera_creds, which walks the env file once and finds
    the {PREFIX}_IP == ip entry.

    Supports the full fleet (all 6 cameras), not just the 833As.
    """
    ip = args[0]
    # Lazy imports — scripts/cam_browser.py is run as a one-shot CLI
    # and we want to defer the infra import until after arg parsing.
    from infra.camera_creds import get_http_password, get_http_user
    from infra.paths import CAMERA_CREDS_FILE

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

    headless = "--headed" not in sys.argv
    with CamBrowser(headless=headless) as cb:
        ok = cb.login(ip, user, pw)
        sys.exit(0 if ok else 1)


def main():
    if len(sys.argv) < 2:
        print("usage: cam_browser.py login <ip>")
        print("       cam_browser.py goto <ip> <path>")
        print("       cam_browser.py profile-path")
        print("       cam_browser.py reset  (wipe cookies)")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "login":
        _cli_login(sys.argv[2:])
    elif cmd == "profile-path":
        print(PROFILE_DIR)
    elif cmd == "reset":
        if PROFILE_DIR.exists():
            shutil.rmtree(PROFILE_DIR)
            print(f"wiped {PROFILE_DIR}")
        else:
            print("no profile dir to wipe")
    else:
        print(f"unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
