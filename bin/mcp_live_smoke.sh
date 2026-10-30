#!/usr/bin/env bash
# mcp_live_smoke.sh — Fast smoke harness for the MCP server.
#
# Boots listener.daemon in smoke mode (zero cameras), waits for the MCP
# server on :8091, sends a JSON-RPC tools/call(list_cameras) request, and
# asserts the response is well-formed JSON with a content array.
#
# Usage:  bash bin/mcp_live_smoke.sh
# Exit:   0 on pass, 1 on failure (including port conflict).
#
# This is a CI-friendly check — no real cameras, no launchd.
# Runs in ~5-10 seconds.

set -euo pipefail

cd "$(dirname "$0")/.."

# -----------------------------------------------------------------------
# Env: smoke-mode knobs
# -----------------------------------------------------------------------
export FARMSURV_SMOKE_MODE=1
export FARM_CAMERA_CREDS_FILE="${FARM_CAMERA_CREDS_FILE:-}"  # empty → no cameras
export FARMSURV_MCP_ENABLED=1
export FARMSURV_MCP_TOKEN="smoke_test_token_abcdef0123456789abcd"   # 40 chars
export FARMSURV_MCP_BIND=127.0.0.1
export LISTEN_PORT=8091

SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

# -----------------------------------------------------------------------
# 1. Pre-flight: assert cwd is v2 (inline, not via preflight script to
#    avoid infinite recursion when preflight calls us back)
# -----------------------------------------------------------------------
if [ ! -f "pyproject.toml" ]; then
  echo "ERROR: pyproject.toml not found — not in the v2 project root" >&2
  exit 1
fi

# -----------------------------------------------------------------------
# 2. Port-conflict check: is :8091 already in use by something else?
# -----------------------------------------------------------------------
echo "Checking port :8091..."
if command -v lsof >/dev/null 2>&1; then
  if lsof -i :8091 >/dev/null 2>&1; then
    echo "FAIL: port :8091 is already in use" >&2
    exit 1
  fi
elif command -v ss >/dev/null 2>&1; then
  if ss -tlnp | grep -q ':8091 '; then
    echo "FAIL: port :8091 is already in use" >&2
    exit 1
  fi
fi
echo "Port :8091 is free."

# -----------------------------------------------------------------------
# 3. Spawn listener.daemon as a background subprocess
# -----------------------------------------------------------------------
echo "Booting listener in smoke mode..."
.venv/bin/python -m listener.daemon >"$SCRATCH/stdout.log" 2>"$SCRATCH/stderr.log" &
LISTENER_PID=$!

# -----------------------------------------------------------------------
# 4. Wait for MCP healthz on :8091 (max 10s)
# -----------------------------------------------------------------------
echo "Waiting for MCP server on :8091..."
for _ in $(seq 1 50); do
  if curl -sf http://127.0.0.1:8091/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

# Verify the healthz actually returns ok
HEALTH=$(curl -sf http://127.0.0.1:8091/healthz || true)
if ! echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('ok'), f'not ok: {d}'"; then
  echo "FAIL: MCP healthz did not return ok" >&2
  echo "MCP healthz response: $HEALTH" >&2
  kill "$LISTENER_PID" 2>/dev/null || true
  wait "$LISTENER_PID" 2>/dev/null || true
  echo "--- stderr ---" >&2
  cat "$SCRATCH/stderr.log" >&2
  exit 1
fi
echo "MCP server healthy."

# -----------------------------------------------------------------------
# 5. Send a JSON-RPC tools/call(list_cameras) to /mcp
# -----------------------------------------------------------------------
echo "Sending tools/call(list_cameras)..."
RESP=$(curl -sf -X POST http://127.0.0.1:8091/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${FARMSURV_MCP_TOKEN}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_cameras","arguments":{}}}')

# -----------------------------------------------------------------------
# 6. Assert well-formed response with content array
# -----------------------------------------------------------------------
echo "$RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin)
assert 'result' in data, 'missing result key'
content = data['result'].get('content', [])
assert isinstance(content, list) and len(content) > 0, 'no content array'
print(f'OK: {len(content)} tools registered')
"

# -----------------------------------------------------------------------
# 7. Teardown
# -----------------------------------------------------------------------
echo "Cleaning up..."
kill "$LISTENER_PID" 2>/dev/null || true
# SIGTERM → SIGKILL escalation after 3s timeout
for _ in $(seq 1 15); do
  if ! kill -0 "$LISTENER_PID" 2>/dev/null; then break; fi
  sleep 0.2
done
if kill -0 "$LISTENER_PID" 2>/dev/null; then
  kill -9 "$LISTENER_PID" 2>/dev/null || true
  wait "$LISTENER_PID" 2>/dev/null || true
fi
echo "Smoke test passed."
exit 0
