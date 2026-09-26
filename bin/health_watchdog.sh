#!/usr/bin/env bash
# health_watchdog.sh -- cron-based operational health monitor.
#
# Runs on a schedule (recommended: every 5 minutes via crontab).
# Checks listener liveness, camera health, disk usage, and remote drift.
# Sends a Telegram alert when any check fails.
#
# Usage:  bash bin/health_watchdog.sh
# Exit:   0 if all checks pass, 1 if any check fails.
#
# Env knobs (loaded from ~/.env and telegram-creds.env):
#   TELEGRAM_BOT_TOKEN    -- Telegram bot token (required for alerts)
#   TELEGRAM_HOME_CHAT_ID -- target chat ID (required for alerts)
#   WATCHDOG_LOG_INTERVAL_MIN -- max age of last log line before alert (default 15)
#   WATCHDOG_DISK_GB_WARN -- disk usage warning threshold in GB (default 150)
#   WATCHDOG_REMOTE_URL   -- Forgejo mirror URL for drift check (default empty = skip)
#   WATCHDOG_SILENT       -- if set to 1, exit 0 immediately. Off-switch for the
#                            whole watchdog without ripping the script. Preferred
#                            over deleting cron entries or moving the file.

if [ "${WATCHDOG_SILENT:-0}" = "1" ]; then
  exit 0
fi

set -euo pipefail

cd "$(dirname "$0")/.."

# -----------------------------------------------------------------------
# 0. Load env from ~/.env (lightweight parser, no eval of secrets)
# -----------------------------------------------------------------------
if [ -f "$HOME/.env" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    _t="${_line#"${_line%%[![:space:]]*}"}"
    _t="${_t%"${_t##*[![:space:]]}"}"
    case "$_t" in
      '#'*) continue ;;
      '')   continue ;;
    esac
    if [[ "$_t" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*) ]]; then
      export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}" 2>/dev/null || true
    fi
  done < "$HOME/.env"
fi

# Load per-app creds
if [ -f "telegram-creds.env" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    _t="${_line#"${_line%%[![:space:]]*}"}"
    _t="${_t%"${_t##*[![:space:]]}"}"
    case "$_t" in
      '#'*) continue ;;
      '')   continue ;;
    esac
    if [[ "$_t" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*) ]]; then
      export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}" 2>/dev/null || true
    fi
  done < "telegram-creds.env"
fi

TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_HOME_CHAT_ID="${TELEGRAM_HOME_CHAT_ID:-}"

WATCHDOG_LOG_INTERVAL_MIN="${WATCHDOG_LOG_INTERVAL_MIN:-15}"
WATCHDOG_DISK_GB_WARN="${WATCHDOG_DISK_GB_WARN:-150}"
WATCHDOG_REMOTE_URL="${WATCHDOG_REMOTE_URL:-}"

# -----------------------------------------------------------------------
# 1. Helpers
# -----------------------------------------------------------------------
PASS=0
FAIL=0
ALERT_LINES=()

ok() {
  PASS=$((PASS + 1))
  echo "  PASS: $1"
}

fail() {
  FAIL=$((FAIL + 1))
  echo "  FAIL: $1" >&2
  ALERT_LINES+=("$1")
}

# -----------------------------------------------------------------------
# 2. Check listener PID
# -----------------------------------------------------------------------
echo "=== Health Watchdog $(date '+%Y-%m-%d %H:%M:%S') ==="
echo ""

echo "--- Check 1: Listener PID ---"
LISTENER_PID=""
if command -v /usr/sbin/lsof >/dev/null 2>&1; then
  LISTENER_PID=$(/usr/sbin/lsof -iTCP:8090 -sTCP:LISTEN -n -P 2>/dev/null \
    | awk 'NR>1 && /python/{print $2}' \
    | head -1 | tr -d ' ')
  if [ -z "$LISTENER_PID" ]; then
    # Fallback: launchctl list
    LISTENER_PID=$(launchctl list 2>/dev/null \
      | awk '$3 == "com.farm.surveillance.v2" {print $1}' \
      | head -1 | tr -d ' ')
  fi
fi

if [ -n "$LISTENER_PID" ]; then
  ok "listener is running (PID $LISTENER_PID)"
else
  fail "listener is NOT running (no process on :8090, no launchd label)"
fi

# -----------------------------------------------------------------------
# 3. Check daemon.log freshness
# -----------------------------------------------------------------------
echo ""
echo "--- Check 2: Log freshness ---"

DAEMON_LOG="$HOME/farm-surveillance-v2/logs/daemon.log"
if [ -f "$DAEMON_LOG" ]; then
  # Get the last line that starts with a timestamp (format: YYYY-MM-DD HH:MM:SS,mmm)
  # Health heartbeat blocks have a timestamped header line followed by content lines
  # without timestamps, so we need to find the last timestamped line.
  LAST_TS=$(grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' "$DAEMON_LOG" \
    | tail -1 | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' || true)
  if [ -z "$LAST_TS" ]; then
    fail "daemon.log has no timestamped lines (log file may be corrupted or empty)"
  else
    # Convert to epoch for age calculation
    LAST_EPOCH=$(date -j -f "%Y-%m-%d %H:%M:%S" "${LAST_TS}" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    AGE_SEC=$((NOW_EPOCH - LAST_EPOCH))
    AGE_MIN=$((AGE_SEC / 60))
    if [ "$AGE_MIN" -gt "$WATCHDOG_LOG_INTERVAL_MIN" ]; then
      fail "daemon.log is stale: last entry ${AGE_MIN}m ago (threshold: ${WATCHDOG_LOG_INTERVAL_MIN}m)"
    else
      ok "daemon.log fresh: last entry ${AGE_MIN}m ago"
    fi
  fi
else
  fail "daemon.log not found at $DAEMON_LOG"
fi

# -----------------------------------------------------------------------
# 4. Check camera health via API endpoint
# -----------------------------------------------------------------------
echo ""
echo "--- Check 3: Camera health ---"

FARMSURV_API_PORT="${FARMSURV_API_PORT:-8092}"
FARMSURV_API_TOKEN="${FARMSURV_API_TOKEN:-}"

# Determine API host address.
# FARMSURV_API_BIND can be "loopback" or "tailnet-only".
# Default to loopback (127.0.0.1) since the API may bind there.
if [ "$FARMSURV_API_BIND" = "tailnet-only" ]; then
  _API_HOST="http://100.64.0.10:${FARMSURV_API_PORT}"
else
  _API_HOST="http://127.0.0.1:${FARMSURV_API_PORT}"
fi

# Probe: if the expected host is unreachable, try the alternative.
if [ -n "$FARMSURV_API_TOKEN" ]; then
  _PROBE=$(curl -sf -o /dev/null -w '%{http_code}' --connect-timeout 2 \
    -H "Authorization: Bearer ${FARMSURV_API_TOKEN}" \
    "${_API_HOST}/api/v1/listener/status" 2>/dev/null || echo "000")
  if [ "$_PROBE" != "200" ]; then
    if [[ "$_API_HOST" == *"127.0.0.1"* ]]; then
      _API_HOST="http://100.64.0.10:${FARMSURV_API_PORT}"
    else
      _API_HOST="http://127.0.0.1:${FARMSURV_API_PORT}"
    fi
  fi
fi

if [ -z "$FARMSURV_API_TOKEN" ]; then
  # Check if API port is even listening
  LSOF_ROWS=$(lsof -iTCP:"${FARMSURV_API_PORT}" -sTCP:LISTEN -n -P 2>/dev/null \
    | wc -l | tr -d ' ')
  if [ "$LSOF_ROWS" -lt 1 ]; then
    fail "API server not listening on :${FARMSURV_API_PORT}"
  else
    ok "API server listening on :${FARMSURV_API_PORT}"

    # Check camera count via debug/rtsp (no auth needed)
    RTSP_RESP=$(curl -sf "${_API_HOST}/debug/rtsp" 2>/dev/null || true)
    if [ -z "$RTSP_RESP" ]; then
      fail "debug/rtsp returned empty or unreachable"
    else
      # Count camera entries in the JSON
      CAM_COUNT=$(echo "$RTSP_RESP" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    cameras = data.get('cameras', [])
    print(len(cameras))
except:
    print(0)
" 2>/dev/null || echo "0")

      if [ "$CAM_COUNT" -ge 6 ]; then
        ok "$CAM_COUNT cameras registered via /debug/rtsp"
      else
        fail "only $CAM_COUNT cameras reported via /debug/rtsp (expected >= 6)"
      fi

      # Check for unhealthy cameras
      UNHEALTHY=$(echo "$RTSP_RESP" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    cameras = data.get('cameras', [])
    unhealthy = [c['camera_id'] for c in cameras if not c.get('healthy', False)]
    print(','.join(unhealthy) if unhealthy else '')
except:
    print('')
" 2>/dev/null || echo '')
      if [ -n "$UNHEALTHY" ]; then
        fail "unhealthy cameras: $UNHEALTHY"
      else
        ok "all cameras healthy"
      fi
    fi
  fi
else
  # With token: check via API endpoint
  RESP=$(curl -sf -H "Authorization: Bearer ${FARMSURV_API_TOKEN}" \
    "${_API_HOST}/api/v1/cameras" 2>/dev/null || true)
  if [ -n "$RESP" ]; then
    CAM_COUNT=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(len(d.get('data', [])))
" 2>/dev/null || echo "0")
    ok "$CAM_COUNT cameras via /api/v1/cameras"
  else
    fail "could not reach /api/v1/cameras"
  fi
fi

# -----------------------------------------------------------------------
# 5. Check disk usage
# -----------------------------------------------------------------------
echo ""
echo "--- Check 4: Disk usage ---"

# Check overall disk usage of the data directory
DATA_DIR="$HOME/farm-surveillance-v2/data"
if [ -d "$DATA_DIR" ]; then
  DATA_SIZE_GB=$(du -sk "$DATA_DIR" 2>/dev/null | awk '{printf "%.0f", $1/1048576}' || echo "0")
  if [ "$DATA_SIZE_GB" -gt "$WATCHDOG_DISK_GB_WARN" ]; then
    fail "data/ is ${DATA_SIZE_GB} GB (threshold: ${WATCHDOG_DISK_GB_WARN} GB)"
  else
    ok "data/ is ${DATA_SIZE_GB} GB (threshold: ${WATCHDOG_DISK_GB_WARN} GB)"
  fi
else
  fail "data/ directory not found at $DATA_DIR"
fi

# -----------------------------------------------------------------------
# 6. Check remote drift (optional, gated by WATCHDOG_REMOTE_URL)
# -----------------------------------------------------------------------
if [ -n "$WATCHDOG_REMOTE_URL" ]; then
  echo ""
  echo "--- Check 5: Remote drift ---"

  # Add or update the remote if it doesn't exist
  git remote get-url forgejo 2>/dev/null || git remote add forgejo "$WATCHDOG_REMOTE_URL" 2>/dev/null || true

  # Fetch and compare
  git fetch --quiet forgejo 2>/dev/null || true
  LOCAL_SHA=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
  REMOTE_SHA=$(git rev-parse forgejo/main 2>/dev/null || echo "unknown")

  if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    ok "local == forgejo/main (no drift)"
  elif [ "$REMOTE_SHA" = "unknown" ]; then
    ok "forgejo remote unreachable, skipping drift check"
  else
    # Check if local is just ahead (normal) or if there's significant divergence
    LOCAL_AHEAD=$(git rev-list --count HEAD..forgejo/main 2>/dev/null || echo "unknown")
    if [ "$LOCAL_AHEAD" = "unknown" ]; then
      ok "drift detected but remote diverged (not a simple ahead)"
    else
      ok "local is $LOCAL_AHEAD commit(s) ahead of forgejo/main"
      # Only alert if behind remote (push might have failed)
      BEHIND=$(git rev-list --count forgejo/main..HEAD 2>/dev/null || echo "unknown")
      if [ "$BEHIND" != "0" ] && [ "$BEHIND" != "unknown" ]; then
        fail "local is $BEHIND commit(s) BEHIND forgejo/main (push may have failed)"
      fi
    fi
  fi
else
  echo ""
  echo "--- Check 5: Remote drift ---"
  echo "  SKIPPED: WATCHDOG_REMOTE_URL not set"
  ok "remote drift check skipped (no remote configured)"
fi

# -----------------------------------------------------------------------
# 7. Summary
# -----------------------------------------------------------------------
TOTAL=$((PASS + FAIL))
echo ""
echo "================================"
echo "Summary: ${PASS} passed, ${FAIL} failed (of ${TOTAL} checks)"
echo "================================"

# -----------------------------------------------------------------------
# 8. Send Telegram alert if any failures
# -----------------------------------------------------------------------
if [ "$FAIL" -gt 0 ] && [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_HOME_CHAT_ID" ]; then
  echo ""
  echo "--- Sending Telegram alert ---"

  ALERT_BODY="Health Watchdog ALERT ($(date '+%H:%M'))

${FAIL} of ${TOTAL} checks failed:

"
  for line in "${ALERT_LINES[@]}"; do
    ALERT_BODY+="$(printf '%s' "$line" | sed 's/^/  - /')
"
  done
  ALERT_BODY+="
PID: $LISTENER_PID
Data size: ${DATA_SIZE_GB:-0} GB
Local: ${LOCAL_SHA:-unknown:0:0}
Remote: ${REMOTE_SHA:-unknown}"

  ALERT_JSON=$(python3 -c "
import json, sys
body = '''${ALERT_BODY}'''.encode('utf-8')
text = body.decode('utf-8', errors='replace')
payload = {
    'chat_id': '${TELEGRAM_HOME_CHAT_ID}',
    'text': text,
    'parse_mode': 'HTML',
}
# Escape backslashes and quotes for bash JSON string
print(json.dumps(payload, ensure_ascii=False).encode('unicode_escape').decode('ascii'))
" 2>/dev/null || echo '')

  curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "$ALERT_JSON" \
    2>/dev/null && echo "  Alert sent." || echo "  FAIL: could not send alert" >&2
else
  if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "--- Telegram not configured: not sending alert ---"
  fi
fi

# -----------------------------------------------------------------------
# 9. Exit
# -----------------------------------------------------------------------
if [ "$FAIL" -gt 0 ]; then
  echo "OVERALL: FAILED"
  exit 1
fi

echo "OVERALL: ALL CHECKS PASSED"
exit 0
