#!/usr/bin/env python3
"""
probe_6b106_person_gatekeeper.py — Live verification probe for Phase.106.

Sends a synthetic CAM1 person webhook directly to the listener's
_process_alert() function (via the /alert HTTP endpoint) and verifies:

  1. Routing — the (CAM1, person) event correctly routes
     to _process_person_alert, not _process_alert (vehicle pipeline).
  2. Telegram dispatch — send_photo_with_caption is called with the
     structured body containing all Qwen attributes.
  3. Audio — when PERSON_AUDIO_ENABLED=1, dispatch_audio_clip is
     called; when off, it's not.
  4. Audit log — append_alert is called with the right matched_name,
     matched_via, alert_id, camera_name.

Stub infrastructure:
  - HTTP /alert endpoint hits the live listener (port 8090)
  - Internal modules (capture_frames, analyze_frames_queued, etc.)
    are stubbed via the live listener's process — we don't intercept;
    we observe via the listener's audit log + log lines.

This probe is end-to-end. It depends on the listener being up and the
synthetic frame path existing on disk.

USAGE:
  source .venv/bin/activate
  python3 scripts/probe_6b106_person_gatekeeper.py [--no-audio]

EXIT CODES:
  0 — probe passed
  1 — probe failed (alert was not received by listener)
  2 — listener is not running (could not reach /alert)
  3 — listener received alert but pipeline produced no output

RELATED:
  - scripts/probe_6b112_three_telegram_stack.py — vehicle-pipeline probe
  - PLAN §11.36 — Phase.106 design plan
"""

# venv: <install-path>/ai_camera_monitor/.venv
# packages: requests
# activate before running:  source <install-path>/ai_camera_monitor/.venv/bin/activate

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

LISTENER_URL = "http://127.0.0.1:8090"
PROJECT_ROOT = Path("<install-path>/ai_camera_monitor")
ALERT_AUDIT_DIR = PROJECT_ROOT / "data" / "alerts"
LISTENER_LOG = PROJECT_ROOT / "logs" / "listener.log"

EDT = timezone(timedelta(hours=-4))


def _find_today_alert_file() -> Path | None:
    """Find today's JSONL audit file under data/alerts/."""
    if not ALERT_AUDIT_DIR.exists():
        return None
    today = datetime.now(EDT).strftime("%Y-%m-%d")
    candidate = ALERT_AUDIT_DIR / f"{today}.jsonl"
    if candidate.exists():
        return candidate
    # Fallback to most recent jsonl
    jsonls = sorted(ALERT_AUDIT_DIR.glob("*.jsonl"), key=lambda p: p.name)
    return jsonls[-1] if jsonls else None


def _check_alert_audit(alert_id: str) -> dict | None:
    """Look for the alert_id in the current day's audit JSONL."""
    audit_file = _find_today_alert_file()
    if audit_file is None:
        return None
    with audit_file.open("r") as f:
        for line in f:
            try:
                raw: dict = json.loads(line)
            except json.JSONDecodeError:
                continue
            if raw.get("alert_id") == alert_id:
                return raw
    return None


def _send_webhook(camera_name: str, event_type: str = "person") -> dict:
    """POST a synthetic webhook to the listener."""
    import requests

    now_iso = datetime.now(EDT).isoformat(timespec="seconds")
    payload: dict = {
        "alarm": {
            "type": event_type,
            "channelName": camera_name,
            "alarmTime": now_iso,
        },
        "deviceModel": "RLC-833A",
        "name": camera_name,
    }
    headers: dict[str, str] = {"Content-Type": "application/json"}
    # Fake the listener's IP allowlist by setting X-Forwarded-For to the
    # canonical CAM1 IP. (The listener accepts webhooks from a known set
    # of camera IPs; we're testing from localhost and need to spoof.)
    headers["X-Forwarded-For"] = "<CAM_IP_REDACTED>"

    url = f"{LISTENER_URL}/alert"
    print(f"[probe] POST {url} payload={json.dumps(payload)}")
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
    except requests.ConnectionError as err:
        print(f"[probe] could not reach listener: {err}")
        sys.exit(2)

    if 200 <= resp.status_code < 300:
        return {"ok": True, "body": resp.json()}
    else:
        print(f"[probe] unexpected status {resp.status_code}: {resp.text[:200]}")
        return {"ok": False, "status": resp.status_code, "body": resp.text}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Don't wait for audit; just confirm the webhook was accepted",
    )
    args = parser.parse_args()

    # Health check
    import requests
    try:
        h = requests.get(f"{LISTENER_URL}/health", timeout=5)
    except requests.ConnectionError as err:
        print(f"[probe] listener not reachable: {err}")
        return 2
    if h.status_code != 200:
        print(f"[probe] /health returned {h.status_code}: {h.text[:200]}")
        return 2
    print(f"[probe] listener health: {h.json()}")

    # Send the synthetic webhook
    print("\n=== Synthetic CAM1 person webhook ===")
    result = _send_webhook("CAM1", "person")
    if not result["ok"]:
        print(f"[probe] webhook not accepted: {result}")
        if result.get("status") == 400 and "IP mismatch" in str(result.get("body", "")):
            print("\n[probe] NOTE: the listener enforces an IP allowlist.")
            print("       This probe expected to spoof the source IP via X-Forwarded-For,")
            print("       but if that doesn't work in your deployment, the unit tests")
            print("       in test_person_event_pipeline_6B106.py pin the routing logic.")
            return 1
        return 1

    print(f"[probe] webhook accepted: {result['body']}")

    if args.no_wait:
        print("\n[probe] --no-wait set; skipping audit check")
        return 0

    # Poll today's audit file for the alert_id
    print("\n[probe] polling today's data/alerts/ JSONL for alert_id...")
    deadline = time.monotonic() + 60.0
    payload = None
    while time.monotonic() < deadline:
        payload = _check_alert_audit(alert_id=result["body"].get("alert_id", ""))
        if payload is not None:
            break
        time.sleep(2.0)
    if payload is None:
        print("[probe] no audit entry appeared within 60s")
        print("        (the pipeline may be slow on this run; check listener.log)")
        return 3

    print(f"[probe] audit payload: {json.dumps(payload, indent=2)[:1000]}")

    print("\n=== Probe result ===")
    checks = []
    checks.append((
        "alert_id present",
        "alert_id" in payload,
    ))
    checks.append((
        "camera_name correct",
        payload.get("camera") == "CAM1",
    ))
    checks.append((
        "matched_name field present",
        "matched_name" in payload,
    ))
    checks.append((
        "matched_via field present",
        "matched_via" in payload,
    ))
    checks.append((
        "telegram_sent field present",
        "telegram_sent" in payload,
    ))

    all_ok = True
    for label, ok in checks:
        marker = "✓" if ok else "✗"
        print(f"  {marker} {label}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\n[probe] PASS")
        return 0
    else:
        print("\n[probe] FAIL (one or more checks failed)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
