"""
daemon.py — Flask webhook server + launchd plist generator for listener.

STATUS: stable
THREAD SAFETY: single-threaded (Flask dev server, single-worker)

INPUTS:
    - env var LISTEN_PORT (optional, default 9999) — TCP port to bind
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
    - Auto-start the service (no auto-start)
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
    - os.environ: LISTEN_PORT
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
    ~/Library/LaunchAgents/com.farm.surveillance.daemon.plist.
    """
    port = os.environ.get("LISTEN_PORT", "9999")
    lines = [
        "Label=com.farm.surveillance.daemon",
        "ProgramArguments=python3 -m listener.daemon --port " + port,
        "RunAtLoad=true",
        "StandardOutPath=/Users/jill/logs/daemon.log",
        "StandardErrorPath=/Users/jill/logs/daemon-error.log",
        "EnvironmentVariables={ LISTEN_PORT=" + port + " }",
        "KeepAlive=false",
        "SoftResourceLimits=256",
    ]
    return "\n".join(lines) + "\n"


def main():
    """Entry point for `python -m listener.daemon`."""
    port = int(os.environ.get("LISTEN_PORT", "9999"))
    app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
