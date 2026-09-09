"""Operator-driven smoke test: synthetic OFS alert reaches Telegram.

US-022d. Mirrors the PRD-V2-019 US-019d / PRD-V2-021 US-021d pattern.
Run after US-022a/b/c land. Confirms the operator's home chat actually
receives TG#1 + TG#2 + TG#3 with lossless PNG attachments.

Usage:
    export DRIVER_REPO_PATH=/Users/jill/farm-surveillance-v2
    export OPERATOR_TELEGRAM_CHAT_ID=374999219
    .venv/bin/python3.11 scripts/smoke_telegram_delivery.py

What it does:
    1. Verify TELEGRAM_HOME_CHAT_ID is non-empty in os.environ.
    2. Verify daemon is reachable on 127.0.0.1:8090.
    3. Stage 4 PNG frames + 1 pairwise_diff.png under data/frames/OFS/.
    4. POST synthetic alert to /alert.
    5. Wait for HTTP 200 response (within 30s).
    6. Print 'check Telegram home chat for 3 messages' + daemon log tail.

Operator manually:
    1. Open Telegram home chat.
    2. Confirm 3 messages arrived with PNG attachments.
    3. Run `file <downloaded-attachment>` -> 'PNG image data'.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

REQUIRED_ENV = ("DRIVER_REPO_PATH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHAT_ID")


def main() -> int:
    # 1. Check env
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: env vars missing: {missing}")
        if "TELEGRAM_HOME_CHAT_ID" in missing:
            print("       Your ~/.env has TELEGRAM_CHAT_ID — rename it to")
            print("       TELEGRAM_HOME_CHAT_ID (the canonical name is")
            print("       TELEGRAM_HOME_CHAT_ID, not TELEGRAM_CHAT_ID).")
        return 2

    repo = Path(os.environ["DRIVER_REPO_PATH"])
    chat_id = os.environ["TELEGRAM_HOME_CHAT_ID"]

    # 2. Check daemon reachable
    print("Checking daemon at 127.0.0.1:8090...")
    try:
        resp = httpx.get("http://127.0.0.1:8090/health", timeout=5.0)
        print(f"  daemon HTTP {resp.status_code}")
    except Exception as exc:
        print(f"  daemon unreachable: {exc}")
        print("  Start it: launchctl load ~/Library/LaunchAgents/com.farm.surveillance.v2.plist")
        return 2

    # 3. Stage synthetic frames
    ofs_dir = repo / "data" / "frames" / "OFS"
    ofs_dir.mkdir(parents=True, exist_ok=True)
    alert_id = "smoke-test-" + str(int(time.time()))
    alert_dir = ofs_dir / alert_id
    alert_dir.mkdir(exist_ok=True)

    # 4 PNG frames (1x1 PNGs)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63f8cfc0500f0000030001f5c4c95e0000000049454e44ae426082"
    )
    frame_paths = []
    for i in range(4):
        p = ofs_dir / f"frame_{i:03d}.png"
        p.write_bytes(png_bytes)
        frame_paths.append(str(p))

    # pairwise_diff.png
    diff_path = alert_dir / "pairwise_diff.png"
    diff_path.write_bytes(png_bytes)

    # crops
    crop_a = alert_dir / "crop_a.png"
    crop_a.write_bytes(png_bytes)
    crop_b = alert_dir / "crop_b.png"
    crop_b.write_bytes(png_bytes)

    print(f"Staged {len(frame_paths)} frames + diff + 2 crops under data/frames/OFS/{alert_id}/")

    # 4. POST synthetic alert
    alert = {
        "camera_id": "OFS",
        "id": alert_id,
        "frames": frame_paths,
        "pairwise_diff_path": str(diff_path),
        "crop_a": str(crop_a),
        "crop_b": str(crop_b),
        "classification_hint": "vehicle",
    }

    print(f"POSTing synthetic alert to /alert (alert_id={alert_id})...")
    t0 = time.time()
    resp = httpx.post(
        "http://127.0.0.1:8090/alert", json=alert, timeout=30.0
    )
    elapsed = time.time() - t0
    print(f"  HTTP {resp.status_code} in {elapsed:.1f}s")
    if resp.status_code != 200:
        print(f"  body: {resp.text[:300]}")
        return 1

    # 5. Tail the daemon log
    log_path = repo / "logs" / "daemon.log"
    if log_path.exists():
        print(f"\nLast 5 lines of logs/daemon.log:")
        for line in log_path.read_text().splitlines()[-5:]:
            print(f"  {line}")

    print(f"\n>>> CHECK TELEGRAM HOME CHAT ({chat_id}) FOR 3 MESSAGES <<<")
    print(f"Expected: TG#1 (caption + 5 PNG attachments), TG#2 (caption + 2 PNG crops), TG#3 (text only).")
    print(f"Verify lossless: open any received photo, run `file <path>` -> expect 'PNG image data'.")

    # Operator confirms manually — no automated verification
    return 0


if __name__ == "__main__":
    sys.exit(main())
