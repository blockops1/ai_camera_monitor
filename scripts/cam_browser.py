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
        cb.login("10.0.0.1", "admin", "pass...")
        cb.gear()
        cb.click_tab("Push")
        cb.click("Webhook")
        ...

CLI (one-shot operations):
    python3 cam_browser.py --camera CAM1 login
    python3 cam_browser.py --camera CAM1 goto /cgi-bin/...
    python3 cam_browser.py --camera CAM1 exec "document.title"
    python3 cam_browser.py --list-cameras

Phase.166 §11.87.2 (2026-08-30):
  - CHROME_PATH now reads from infra.paths.BROWSER_CHROME_PATH
    (env var BROWSER_CHROME_PATH), defaults to system Chrome.
  - CLI login() resolves HTTP_USER / HTTP_PASS by IP via
    infra.camera_creds.get_http_user / get_http_password — replaces
    the hardcoded {if ip == <CAM_IP_REDACTED>: FRONT_HTTP_PASS} / {elif
    <CAM_IP_REDACTED_2>: BACK_HTTP_PASS} branching that broke for every
    other camera.
  - env_path defaults to infra.paths.CAMERA_CREDS_FILE — was hardcoded
    to <legacy-repo>/camera-creds.env which broke
    in the refactor (which lives at ~/ai_camera_monitor).

Phase.167 §13.5 (2026-08-30, T2 Commit 7):
  - CLI accepts `--camera <code>` (CAM1/CAM2/...) instead of raw IP.
    Resolves via infra.cameras.by_code() which knows the operator's
    camera-creds.env. Removes the operator's habit of typing the
    camera's IP on the command line (which leaks into shell history
    + tmux scrollback + bot-notification logs).
  - Bare-IP positional kept as `--ip <addr>` for one-shot scripts
    and backwards compat (deprecated, will be removed in a later
    release).
  - `--list-cameras` prints the camera registry for discovery.
  - `--headed` / `--headless` flags replace the bare `--headed`
    positional hack.
"""

from __future__ import annotations

import argparse
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


# --- CLI (Phase.167 §13.5 Commit 7) ---
#
# Usage forms:
#   cam_browser.py --camera CAM1 login
#   cam_browser.py --camera CAM1 goto /cgi-bin/...
#   cam_browser.py --camera CAM1 exec "document.title"
#   cam_browser.py --ip 10.0.0.1 login       # legacy bare-IP form (deprecated)
#   cam_browser.py --list-cameras            # print known camera registry
#   cam_browser.py profile-path
#   cam_browser.py reset
#
# Either --camera or --ip is required for commands that touch a specific
# camera (login/goto/exec). --list-cameras, profile-path, reset take no
# camera selector.


def _resolve_camera(args: argparse.Namespace):
    """Resolve --camera or --ip to (ip, user, password).

    Returns (ip, user, password) tuple. Raises SystemExit on lookup failure.

    Phase.167 §13.5: prefer --camera <code> (e.g. CAM1). Falls back to
    --ip <addr> for legacy scripts. Both lookups route through infra.cameras
    so the same registry handles operator-flavored legacy prefixes AND the
    new CAM{N} convention.
    """
    # Lazy import — scripts/cam_browser.py runs as one-shot CLI; defer
    # infra import until after arg parsing completes.
    from infra.cameras import by_code, by_ip, all_codes
    from infra.camera_creds import get_http_password, get_http_user
    from infra.paths import CAMERA_CREDS_FILE

    if args.camera:
        try:
            spec = by_code(args.camera)
        except KeyError:
            codes = ", ".join(all_codes()) or "(none — env file empty or missing)"
            raise SystemExit(
                f"unknown camera code: {args.camera!r}. Known codes: {codes}"
            )
        ip = spec.ip
    elif args.ip:
        try:
            spec = by_ip(args.ip)
        except KeyError:
            raise SystemExit(
                f"unknown IP: {args.ip} (no _IP entry found in {CAMERA_CREDS_FILE})"
            )
        ip = spec.ip
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


def _cli_login(args: argparse.Namespace):
    """CLI: log into a camera using creds from camera-creds.env.

    Phase.166 §11.87.1: replaced hardcoded FRONT_HTTP_PASS /
    BACK_HTTP_PASS branching with infra.camera_creds IP lookup.
    Phase.167 §13.5 (Commit 7): --camera <code> added; --ip <addr>
    kept for legacy one-shot scripts.
    """
    ip, user, pw = _resolve_camera(args)
    with CamBrowser(headless=args.headless) as cb:
        ok = cb.login(ip, user, pw)
        sys.exit(0 if ok else 1)


def _cli_goto(args: argparse.Namespace):
    """CLI: navigate a logged-in browser session to a camera URL path."""
    ip, _, _ = _resolve_camera(args)
    url = f"http://{ip}{args.path}"
    with CamBrowser(headless=args.headless) as cb:
        cb.goto(url)
        print(f"navigated to {url}", file=sys.stderr)


def _cli_exec(args: argparse.Namespace):
    """CLI: execute a JS expression in the camera's page context."""
    ip, _, _ = _resolve_camera(args)
    with CamBrowser(headless=args.headless) as cb:
        cb.goto(f"http://{ip}/")
        result = cb.evaluate(args.expression)
        print(result)


def _cli_list_cameras(_args: argparse.Namespace):
    """CLI: print the known camera registry (code / name / ip / zone)."""
    from infra.cameras import load_cameras
    specs = load_cameras()
    if not specs:
        print("(no cameras found in camera-creds.env)")
        return
    print(f"{'CODE':<8} {'NAME':<25} {'IP':<16} {'ZONE'}")
    for s in specs:
        print(f"{s.code:<8} {s.name:<25} {s.ip:<16} {s.zone}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cam_browser.py",
        description=(
            "Persistent Reolink camera browser session. "
            "Use --camera <code> (e.g. CAM1) to target a registered camera, "
            "or --list-cameras to print the registry."
        ),
    )
    target = parser.add_argument_group("camera selector (one required for login/goto/exec)")
    target.add_argument(
        "--camera", metavar="CODE",
        help="Camera code (CAM1, CAM2, ...). Resolves via infra.cameras.",
    )
    target.add_argument(
        "--ip", metavar="ADDR",
        help="[deprecated] Bare IP address. Use --camera instead.",
    )
    target.add_argument(
        "--list-cameras", action="store_true",
        help="Print the camera registry and exit.",
    )
    mode = parser.add_argument_group("display mode")
    mode.add_argument(
        "--headed", action="store_true",
        help="Run browser with a visible window (default: headless).",
    )
    mode.add_argument(
        "--headless", action="store_true",
        help="Run browser headless (default).",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("login", help="Log into the camera and persist cookies.")
    p_goto = sub.add_parser("goto", help="Navigate the browser to a path on the camera.")
    p_goto.add_argument("path", help="Path on the camera (e.g. /cgi-bin/api/v2/...).")
    p_exec = sub.add_parser("exec", help="Evaluate a JS expression in the camera page.")
    p_exec.add_argument("expression", help="JavaScript expression to evaluate.")
    sub.add_parser("profile-path", help="Print the persistent profile directory.")
    sub.add_parser("reset", help="Wipe the persistent profile directory.")

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    # --list-cameras: short-circuit, no camera selector needed
    if args.list_cameras:
        _cli_list_cameras(args)
        return

    headless = not args.headed
    if args.headless:
        headless = True
    args.headless = headless  # store for subcommand helpers

    if args.command is None:
        parser.print_help()
        sys.exit(1)
    elif args.command == "login":
        _cli_login(args)
    elif args.command == "goto":
        _cli_goto(args)
    elif args.command == "exec":
        _cli_exec(args)
    elif args.command == "profile-path":
        print(PROFILE_DIR)
    elif args.command == "reset":
        if PROFILE_DIR.exists():
            shutil.rmtree(PROFILE_DIR)
            print(f"wiped {PROFILE_DIR}")
        else:
            print("no profile dir to wipe")


if __name__ == "__main__":
    main()
