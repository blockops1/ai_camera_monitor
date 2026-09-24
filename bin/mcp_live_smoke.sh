#!/usr/bin/env bash
# mcp_live_smoke.sh — Fast smoke harness for the MCP server.
#
# Boots listener.daemon in smoke mode (zero cameras), waits for the MCP
# server on :8091, sends a JSON-RPC tools/list request, and asserts
# the response is well-formed JSON with at least one tool registered.
#
# Usage:  bash bin/mcp_live_smoke.sh
# Exit:   0 on pass, 1 on failure.
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
export LISTEN_PORT=9999       # non-standard to avoid port 8090 conflict

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
# 2. Spawn listener.daemon as a background subprocess
# -----------------------------------------------------------------------
echo "Booting listener in smoke mode..."
.venv/bin/python -m listener.daemon >"$SCRATCH/stdout.log" 2>"$SCRATCH/stderr.log" &
LISTENER_PID=$!

# -----------------------------------------------------------------------
# 3. Wait for MCP healthz on :8091 (max 10s)
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
if ! echo "$HEALTH" | python -c "import sys,json; d=json.load(sys.stdin); assert d.get('ok'), f'not ok: {d}'"; then
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
# 4. Hit the MCP tools/list endpoint
# -----------------------------------------------------------------------
RESP=$(curl -sf -X POST http://127.0.0.1:8091/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${FARMSURV_MCP_TOKEN}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')

# -----------------------------------------------------------------------
# 5. Assert well-formed response
# -----------------------------------------------------------------------
echo "$RESP" | python -c "
import sys, json
data = json.load(sys.stdin)
assert 'result' in data, 'missing result key'
tools = data['result'].get('tools', [])
assert len(tools) > 0, 'no tools registered'
print(f'OK: {len(tools)} tools registered')
for t in tools:
    print(f'  - {t[\"name\"]}')
"

# -----------------------------------------------------------------------
# 6. Teardown
# -----------------------------------------------------------------------
kill "$LISTENER_PID" 2>/dev/null || true
wait "$LISTENER_PID" 2>/dev/null || true
echo "Smoke test passed."
exit 0
