"""
daemon.py — Flask webhook server + launchd plist generator for listener.

STATUS: stable
THREAD SAFETY: single-threaded (Flask dev server, single-worker)

INPUTS:
    - env var LISTEN_HOST (default 0.0.0.0) — bind interface
    - env var LISTEN_PORT (default 8090) — TCP port to bind (matches v1)
    - env var TELEGRAM_BOT_TOKEN, TELEGRAM_HOME_CHAT_ID, VISION_LLM_URL
    - POST body: JSON dict — camera alert payload

OUTPUTS:
    - HTTP 200 with JSON response from handle_webhook()
    - generate_plist() returns a str: a launchd plist XML

PUBLIC API:
    app — Flask app instance with POST /webhook route
    generate_plist() -> str
        Return a plist XML string for manual installation.

DOES NOT DO:
    - Install or start the daemon (operator copies plist manually)
    - Auto-start the service (no auto-start, but plist has KeepAlive=true
      so launchd restarts on crash)
    - Validate camera request signatures (listener handles that)
    - Run as a multi-worker server (dev server only)

WHY HERE:
    The operator needs a ready-to-review daemon before activation.
    Plist generation lives in the same module so the operator can
    pipe `python -m listener.daemon` to stdout and inspect before
    copying to ~/Library/LaunchAgents/.

CALLED BY:
    (external: Reolink camera sends POST /webhook)

CALLS INTO:
    - listener.listener: handle_webhook() for the pipeline
    - flask: HTTP server + request parsing
    - os.environ: LISTEN_HOST, LISTEN_PORT, TELEGRAM_BOT_TOKEN,
      TELEGRAM_HOME_CHAT_ID, VISION_LLM_URL
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route("/webhook", methods=["POST"])
def webhook():
    """Accept camera alerts from Reolink webhooks.

    Parses the JSON body, passes it to the pipeline via
    handle_webhook(), and returns the result as JSON 200.
    """
    from listener.listener import handle_webhook

    alert = request.get_json(silent=True)
    if alert is None:
        return jsonify({"status": "error", "reason": "invalid json"}), 400

    result = handle_webhook(alert)
    return jsonify(result), 200


def generate_plist() -> str:
    """Return a launchd plist XML string for manual installation.

    The operator copies this output to
    ~/Library/LaunchAgents/com.farm.surveillance.v2.plist.

    Includes full environment so the daemon finds Telegram + LLM creds.
    """
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = os.environ.get("LISTEN_PORT", "8090")

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = os.environ.get("TELEGRAM_HOME_CHAT_ID", "")
    vision_url = os.environ.get("VISION_LLM_URL", "http://127.0.0.1:8080")

    # WorkingDirectory must be the v2 repo root so relative imports resolve.
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.farm.surveillance.v2</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/jill/farm-surveillance-v2/.venv/bin/python3.11</string>
        <string>-m</string>
        <string>listener.daemon</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{workdir}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/jill/farm-surveillance-v2/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LISTEN_HOST</key>
        <string>{host}</string>
        <key>LISTEN_PORT</key>
        <string>{port}</string>
        <key>TELEGRAM_BOT_TOKEN</key>
        <string>{tg_token}</string>
        <key>TELEGRAM_HOME_CHAT_ID</key>
        <string>{tg_chat_id}</string>
        <key>VISION_LLM_URL</key>
        <string>{vision_url}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/Users/jill/farm-surveillance-v2/logs/daemon.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/jill/farm-surveillance-v2/logs/daemon-error.log</string>

    <key>SoftResourceLimits</key>
    <integer>256</integer>
</dict>
</plist>
"""


def main():
    """Entry point for `python -m listener.daemon`."""
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("LISTEN_PORT", "8090"))
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
