"""
Probe: verify /status endpoint behavior for prompt_mode field.

Before Phase.102 (current state):
    /status returns prompt_mode={"error": "...VEHICLE_COMBINED_PROMPT_TEMPLATE...ImportError..."}
    because listener/listener.py:988 lazy-imports VEHICLE_COMBINED_PROMPT_TEMPLATE
    which was deleted in Phase.78. The bare `except Exception` swallows it
    silently; /status has been returning this error for ~5 days.

After Phase.102:
    /status should NOT include a prompt_mode field at all (block deleted).

This probe calls /status directly and reports what's present. Run BEFORE the
patch to confirm the broken behavior is reproducible, then AFTER the patch
to confirm prompt_mode is gone.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/probe_status_prompt_mode.py
"""

import json
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8090/status"


def fetch_status() -> dict:
    req = urllib.request.Request(URL, method="GET")
    with urllib.request.urlopen(req, timeout=3) as r:
        body: dict = json.loads(r.read())
        return body


def main() -> int:
    try:
        body = fetch_status()
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        print(f"FAIL: could not reach {URL}: {exc}")
        return 1

    has_prompt_mode = "prompt_mode" in body
    print(f"/status reachable, top-level keys: {sorted(body.keys())}")
    print()

    if has_prompt_mode:
        pm = body["prompt_mode"]
        print(f"prompt_mode present: {json.dumps(pm, indent=2)}")
        if isinstance(pm, dict) and "error" in pm:
            err = pm["error"]
            if "VEHICLE_COMBINED_PROMPT_TEMPLATE" in err or "ImportError" in err:
                print()
                print(">>> EXPECTED broken-state behavior: prompt_mode carries ImportError.")
                print(">>> Phase.102 fix should delete this block entirely.")
                return 0  # broken state is the documented pre-fix state
            else:
                print()
                print(f">>> UNEXPECTED error in prompt_mode: {err!r}")
                return 2
        else:
            print()
            print(">>> prompt_mode present but NOT in expected broken form.")
            print(">>> Either the fix has already shipped, or behavior changed.")
            return 0
    else:
        print("prompt_mode absent from /status response.")
        print()
        print(">>> EXPECTED post-fix behavior: prompt_mode field is gone.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
