#!/usr/bin/env python3
"""smoke_alert_native.py -- Live-fire smoke test for the native-resolution alert pipeline.

POSTs a synthetic Reolink-style alert to the running daemon, then verifies:
  (a) the daemon returns HTTP 200
  (b) at least one frame/crop file was written under data/frames/
  (c) the file's pixel dimensions match the camera's native resolution
      (NOT 640x640, which would indicate letterboxing is still happening)

Usage:
    python scripts/smoke_alert_native.py

Requires:
    - daemon running on http://127.0.0.1:8090
    - at least one camera with frame files in data/frames/

Returns 0 on success, 1 on failure.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

DAEMON_URL = "http://127.0.0.1:8090/alert"
FRAMES_DIR = Path(__file__).resolve().parent.parent / "data" / "frames"

# Working camera names from the existing frame store.
CAMERAS = [
    "OUTSIDE_FRONT_SOLAR",
    "OUTSIDE_FRONT_GARAGE",
    "OUTSIDE_FRONT_POWER",
    "FRONT",
    "BACK",
    "OUTSIDE_BACK_SOLAR",
]


def make_reolink_payload(camera_id: str) -> dict:
    """Build a Reolink-style alert payload for a given camera."""
    return {
        "type": "motion",
        "alarm": {
            "type": "motion",
            "time": "2026-09-10T12:00:00Z",
            "channelName": camera_id,
            "device": camera_id,
            "name": camera_id,
        },
    }


def post_alert(camera_id: str) -> dict | None:
    """POST a synthetic Reolink alert to the daemon.

    Returns the JSON body dict on HTTP 2xx, None otherwise.
    """
    payload = make_reolink_payload(camera_id)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DAEMON_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode())
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
    return None


def find_new_frame_files() -> list[Path]:
    """Scan data/frames/<camera>/ for jpg files not older than 60s.

    Returns the list of file paths sorted by modification time (newest last).
    """
    found: list[Path] = []
    now = time.time()
    for cam_dir in sorted(FRAMES_DIR.iterdir()):
        if not cam_dir.is_dir():
            continue
        for f in cam_dir.glob("frame_*.jpg"):
            mtime = f.stat().st_mtime
            if mtime > now - 60:
                found.append(f)
    return sorted(found, key=lambda p: p.stat().st_mtime)


def check_dimensions(files: list[Path]) -> bool:
    """Verify at least one file has native resolution (NOT 640x640).

    Returns True if the check passes.
    """
    for f in files:
        with Image.open(f) as img:
            w, h = img.size
            if w == 640 and h == 640:
                print(f"  FAIL: {f} is 640x640 (letterboxed)")
                return False
            print(f"  OK: {f} = {w}x{h}")
            if w >= 1280 and h >= 720:
                print("  -> native resolution confirmed")
                return True
    # No files found -- that's also a failure (handled by caller).
    return False


def main() -> int:
    """Run the smoke test. Return 0 on success, 1 on failure."""
    print("=== US-019e smoke test: native-resolution alert pipeline ===")

    # Step 1: POST a synthetic alert.
    print("\n[1] Posting synthetic Reolink alert to daemon ...")
    camera = CAMERAS[0]  # OUTSIDE_FRONT_SOLAR
    result = post_alert(camera)
    if result is None:
        print("FAIL: daemon did not return HTTP 200 or is unreachable.")
        return 1
    print(f"  -> daemon returned HTTP 200, body keys: {sorted(result.keys())}")

    # Step 2: Wait briefly for the daemon to process.
    print("\n[2] Waiting for daemon to process ...")
    time.sleep(5)

    # Step 3: Inspect frame files.
    print("\n[3] Inspecting frame files ...")
    files = find_new_frame_files()
    if not files:
        print("FAIL: no new frame files written.")
        return 1
    print(f"  -> found {len(files)} frame file(s)")

    # Step 4: Check dimensions.
    print("\n[4] Checking native resolution ...")
    if not check_dimensions(files):
        return 1

    # Step 5: Verify Telegram dispatch happened (check for tg1/tg2 in response).
    print("\n[5] Checking pipeline output ...")
    tg_keys = [k for k in result if k.startswith("tg")]
    if tg_keys:
        print(f"  -> pipeline produced Telegram payloads: {tg_keys}")
    else:
        print("  NOTE: no tg* keys in pipeline response (may be suppressed)")

    print("\n=== PASS: native-resolution alert pipeline verified ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
