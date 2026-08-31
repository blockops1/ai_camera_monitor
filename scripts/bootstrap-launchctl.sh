#!/bin/bash
# bootstrap-launchctl.sh — restart the ai_camera_monitor listener under launchd.
# Run from your terminal, NOT from the agent:
#   bash <install-path>/ai_camera_monitor/scripts/bootstrap-launchctl.sh
set -e

PLIST="$HOME/Library/LaunchAgents/ai.farm.surveillance-listener-refactor.plist"
LABEL="ai.farm.surveillance-listener-refactor"
DOMAIN="gui/$(id -u)"

# Kill any stray manual listener on :8090
STRAY=$(lsof -ti :8090 2>/dev/null || true)
if [ -n "$STRAY" ]; then
    echo "Killing stray listener PID $STRAY on :8090"
    kill "$STRAY" 2>/dev/null || true
    sleep 2
fi

# Bootout any existing launchd job for this label (idempotent)
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
sleep 1

# Bootstrap fresh
echo "Bootstrapping $LABEL..."
launchctl bootstrap "$DOMAIN" "$PLIST"
sleep 3

# Receipts
echo "---"
echo "launchctl list:"
launchctl list | grep -i farm || echo "  (no farm jobs listed)"

NEW_PID=$(lsof -ti :8090 2>/dev/null || true)
if [ -n "$NEW_PID" ]; then
    echo "---"
    echo "ps for new listener:"
    ps -o pid,etime,command -p "$NEW_PID"
    echo "---"
    echo "/health:"
    curl -s http://127.0.0.1:8090/health
    echo ""
    echo "---"
    echo "/status uptime_seconds:"
    curl -s http://127.0.0.1:8090/status | python3 -c "import json,sys; print(json.load(sys.stdin).get('uptime_seconds'))"
else
    echo "ERROR: listener did not bind :8090 within 3s"
    echo "---"
    echo "LaunchAgent stdout:"
    tail -30 <install-path>/ai_camera_monitor/logs/launchctl-stdout.log 2>/dev/null || echo "  (no stdout log)"
    echo "---"
    echo "LaunchAgent stderr:"
    tail -30 <install-path>/ai_camera_monitor/logs/launchctl-stderr.log 2>/dev/null || echo "  (no stderr log)"
    exit 1
fi