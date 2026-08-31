"""
probe_6b105c_webhook.py — Phase.105c verification probe (2026-08-21)

Fires a synthetic CAM3 motion webhook at the running listener on :8090 to
confirm the slim `_process_alert` driver correctly delegates to
`listener.vehicle_event_pipeline.process_alert(ctx)` and the full 6-stage
pipeline executes end-to-end.

Use cases:
  - Smoke-test after listener.py / vehicle_event_pipeline.py edits
  - Confirm pipeline module is wired into production (state.py imports
    resolve, AlertContext builds, all 6 stages fire)
  - Verify STATE bumps (total_alerts, by_threat_level, last_alert) via
    /status after the webhook completes (~30s)

Run:
  .venv/bin/python scripts/probe_6b105c_webhook.py

What you'll see:
  - stdout: webhook POST status (202) + alert_id
  - /status after ~30s: total_alerts=1, last_alert populated
  - logs/listener.log: 6 stage log lines (capture → identify → match →
    select_best_frame → generate_alert → emit_result) tagged
    `[vehicle_event_pipeline]`
"""
import datetime
import json
import urllib.request


def send_webhook(payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8090/alert",
        data=data,
        headers={
            "Content-Type": "application/json",
            # Spoof source IP for the listener's XFF check (see listener.py:1139)
            "X-Forwarded-For": "<CAM_IP_REDACTED>",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


payload = {
    "type": "ReolinkMotion",
    "alarm": {
        "channel": 0,
        "channelName": "CAM3",
        "device": "CAM3-RLC510A",
        "deviceModel": "RLC-510A",
        "message": "Motion detected",
        "name": "CAM3",
        # Naive UTC + "+0000" suffix mimics real Reolink webhook format.
        # The listener normalizes to EDT (6B.99); we don't need tz here.
        "time": datetime.datetime.now().isoformat() + "+0000",  # noqa: DTZ005
        "type": "md",
    },
}

status, body = send_webhook(payload)
print(f"Status: {status}")
print(f"Body: {body}")