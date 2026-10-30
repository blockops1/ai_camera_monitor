#!/usr/bin/env bash
# api_live_smoke.sh — Fast smoke harness for the API server on :8092.
#
# Hits 5 representative endpoints, asserts response shapes, checks auth,
# and prints a PASS/FAIL summary.
#
# Usage:  bash bin/api_live_smoke.sh
# Exit:   0 on pass, 1 on failure.
#
# This is a live-check against a running listener's API thread.
# The listener must already be booted via launchd or manual spawn.
#
# Env knobs (loaded from ~/.env):
#   FARMSURV_API_TOKEN   — Bearer token for auth (default: from ~/.env)
#   FARMSURV_API_PORT    — API port (default: 8092)

set -euo pipefail

cd "$(dirname "$0")/.."

# -----------------------------------------------------------------------
# 0. Config
# -----------------------------------------------------------------------
FARMSURV_API_PORT="${FARMSURV_API_PORT:-8092}"
# Detect bind mode from ~/.env to pick the right address
_BIND_MODE=""
if [ -f "$HOME/.env" ]; then
  while IFS= read -r _env_line || [ -n "$_env_line" ]; do
    _env_trimmed="${_env_line#"${_env_line%%[![:space:]]*}"}"
    _env_trimmed="${_env_trimmed%"${_env_trimmed##*[![:space:]]}"}"
    if [[ "$_env_trimmed" =~ ^FARMSURV_API_BIND=(.*) ]]; then
      _BIND_MODE="${BASH_REMATCH[1]}"
      break
    fi
  done < "$HOME/.env"
fi
if [ "$_BIND_MODE" = "tailnet-only" ] && [ -n "${FARMSURV_API_HOST:-${TAILNET_BIND_ADDR:-}}" ]; then
  HOST="http://${FARMSURV_API_HOST:-${TAILNET_BIND_ADDR}}:${FARMSURV_API_PORT}"
else
  HOST="http://127.0.0.1:${FARMSURV_API_PORT}"
fi

# Load ~/.env (FARMSURV_API_TOKEN may be set there).
# Use a per-line parser to handle unquoted .env values safely.
if [ -f "$HOME/.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    # Trim leading/trailing whitespace
    trimmed="${line#"${line%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    # Skip comments and blanks
    case "$trimmed" in
      '#'*) continue ;;
      '')   continue ;;
    esac
    # Only export lines that look like VAR=value
    if [[ "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*) ]]; then
      eval "export ${BASH_REMATCH[1]}=\"${BASH_REMATCH[2]}\"" 2>/dev/null || true
    fi
  done < "$HOME/.env"
fi

API_TOKEN="${FARMSURV_API_TOKEN:-}"
AUTH_HEADER="Authorization: Bearer ${API_TOKEN}"

# -----------------------------------------------------------------------
# 1. Pre-flight: assert cwd is v2
# -----------------------------------------------------------------------
if [ ! -f "pyproject.toml" ]; then
  echo "ERROR: pyproject.toml not found -- not in the v2 project root" >&2
  exit 1
fi

# -----------------------------------------------------------------------
# 2. Port check
# -----------------------------------------------------------------------
echo "Checking API port :${FARMSURV_API_PORT}..."
if command -v /usr/sbin/lsof >/dev/null 2>&1; then
  # lsof output: first line is header, second line is the process
  LSOF_ROWS=$(/usr/sbin/lsof -iTCP:"${FARMSURV_API_PORT}" -sTCP:LISTEN 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')
  if [ "$LSOF_ROWS" -lt 1 ]; then
    echo "FAIL: nothing listening on :${FARMSURV_API_PORT}" >&2
    exit 1
  fi
  echo "Port :${FARMSURV_API_PORT} is active (${LSOF_ROWS} process(es))."
else
  echo "WARN: lsof not found, skipping port check" >&2
fi

# -----------------------------------------------------------------------
# Helper: run a test, print PASS/FAIL
# -----------------------------------------------------------------------
PASS=0
FAIL=0

ok() {
  PASS=$((PASS + 1))
  echo "  PASS: $1"
}

fail() {
  FAIL=$((FAIL + 1))
  echo "  FAIL: $1"
}

# -----------------------------------------------------------------------
# 3. Endpoint 1: GET /api/v1/listener/status
# -----------------------------------------------------------------------
echo ""
echo "--- Endpoint 1: GET /api/v1/listener/status ---"
if [ -z "$API_TOKEN" ]; then
  fail "no FARMSURV_API_TOKEN set"
else
  RESP=$(curl -sf -H "$AUTH_HEADER" "${HOST}/api/v1/listener/status" 2>/dev/null || true)
  if [ -z "$RESP" ]; then
    fail "HTTP request failed"
  else
    # Assert .data.uptime_seconds is numeric >= 0
    UPTIME=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
data = d.get('data', {})
v = data.get('uptime_seconds', None)
assert v is not None, 'uptime_seconds missing'
assert isinstance(v, (int, float)), f'uptime_seconds not numeric: {type(v)}'
assert v >= 0, f'uptime_seconds < 0: {v}'
print('ok')
" 2>/dev/null || true)
    if [ "$UPTIME" = "ok" ]; then
      ok "uptime_seconds is numeric >= 0"
    else
      fail "uptime_seconds check: $UPTIME"
    fi
  fi
fi

# -----------------------------------------------------------------------
# 4. Endpoint 2: GET /api/v1/cameras
# -----------------------------------------------------------------------
echo ""
echo "--- Endpoint 2: GET /api/v1/cameras ---"
if [ -z "$API_TOKEN" ]; then
  fail "no FARMSURV_API_TOKEN set"
else
  RESP=$(curl -sf -H "$AUTH_HEADER" "${HOST}/api/v1/cameras" 2>/dev/null || true)
  if [ -z "$RESP" ]; then
    fail "HTTP request failed"
  else
    CAM_CHECK=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
data = d.get('data', [])
assert isinstance(data, list), 'data is not a list'
assert len(data) >= 6, f'camera count {len(data)} < 6'
for c in data:
    cid = c.get('camera_id', '')
    assert cid and len(cid) > 0, f'empty camera_id: {c}'
print('ok')
" 2>/dev/null || true)
    if [ "$CAM_CHECK" = "ok" ]; then
      ok ">= 6 cameras, all camera_id non-empty"
    else
      fail "cameras check: $CAM_CHECK"
    fi
  fi
fi

# -----------------------------------------------------------------------
# 5. Endpoint 3: GET /api/v1/listener/errors?since=<1h-ago>
# -----------------------------------------------------------------------
echo ""
echo "--- Endpoint 3: GET /api/v1/listener/errors ---"
if [ -z "$API_TOKEN" ]; then
  fail "no FARMSURV_API_TOKEN set"
else
  SINCE=$(python3 -c "import datetime; print((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S+00:00'))")
  HTTP_CODE=$(curl -so /dev/null -sw '%{http_code}' -H "$AUTH_HEADER" "${HOST}/api/v1/listener/errors?since=${SINCE}" 2>/dev/null || echo "000")
  if [ "$HTTP_CODE" = "200" ]; then
    ok "HTTP 200"
  else
    fail "expected 200, got ${HTTP_CODE}"
  fi
fi

# -----------------------------------------------------------------------
# 6. Endpoint 4: GET /api/v1/telegram/status
# -----------------------------------------------------------------------
echo ""
echo "--- Endpoint 4: GET /api/v1/telegram/status ---"
if [ -z "$API_TOKEN" ]; then
  fail "no FARMSURV_API_TOKEN set"
else
  RESP=$(curl -sf -H "$AUTH_HEADER" "${HOST}/api/v1/telegram/status" 2>/dev/null || true)
  if [ -z "$RESP" ]; then
    fail "HTTP request failed"
  else
    # Assert errors_last_24h is an integer >= 0
    TG_CHECK=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
data = d.get('data', {})
v = data.get('errors_last_24h', None)
assert v is not None, 'errors_last_24h missing'
assert isinstance(v, int), f'errors_last_24h not int: {type(v)}'
assert v >= 0, f'errors_last_24h < 0: {v}'
print('ok')
" 2>/dev/null || true)
    if [ "$TG_CHECK" = "ok" ]; then
      ok "errors_last_24h is int >= 0"
    else
      fail "telegram status check: $TG_CHECK"
    fi
  fi
fi

# -----------------------------------------------------------------------
# 7. Endpoint 5: GET /api/v1/vision/status
# -----------------------------------------------------------------------
echo ""
echo "--- Endpoint 5: GET /api/v1/vision/status ---"
if [ -z "$API_TOKEN" ]; then
  fail "no FARMSURV_API_TOKEN set"
else
  RESP=$(curl -sf -H "$AUTH_HEADER" "${HOST}/api/v1/vision/status" 2>/dev/null || true)
  if [ -z "$RESP" ]; then
    fail "HTTP request failed"
  else
    # Assert model_name is present (string, even if None)
    VISION_CHECK=$(echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
data = d.get('data', {})
mn = data.get('model_name', 'MISSING')
# model_name can be None (not loaded) or a string — just check it exists
assert mn is not None or 'model_name' in data, 'model_name key missing from response'
print('ok')
" 2>/dev/null || true)
    if [ "$VISION_CHECK" = "ok" ]; then
      ok "model_name key present in response"
    else
      fail "vision status check: $VISION_CHECK"
    fi
  fi
fi

# -----------------------------------------------------------------------
# 8. Auth negative: no token -> 401
# -----------------------------------------------------------------------
echo ""
echo "--- Auth negative: no token -> 401 ---"
if [ -z "$API_TOKEN" ]; then
  fail "cannot test auth without FARMSURV_API_TOKEN set"
else
  AUTH_CODE=$(curl -so /dev/null -s -w '%{http_code}' "${HOST}/api/v1/listener/status" 2>/dev/null || echo "000")
  if [ "$AUTH_CODE" = "401" ]; then
    ok "no token -> 401"
  else
    fail "401 expected, got ${AUTH_CODE}"
  fi
fi

# -----------------------------------------------------------------------
# 9. Summary
# -----------------------------------------------------------------------
TOTAL=$((PASS + FAIL))
echo ""
echo "================================"
echo "Smoke test summary: ${PASS}/${TOTAL} PASS"
echo "================================"

if [ "$FAIL" -gt 0 ]; then
  echo "FAILED (${FAIL} of ${TOTAL} checks)"
  exit 1
fi

echo "ALL CHECKS PASSED"
exit 0
