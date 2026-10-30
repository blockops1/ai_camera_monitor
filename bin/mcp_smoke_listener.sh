#!/usr/bin/env bash
# mcp_smoke_listener.sh — Wrapper to boot the listener in smoke mode.
#
# Used by launchd (com.farm.surveillance.v2.smoke.plist) to start
# the listener with FARMSURV_SMOKE_MODE=1, which disables real
# camera connections. Intended for CI and quick sanity checks.
#
# Usage:  bash bin/mcp_smoke_listener.sh
# Exit:   inherits from python -m listener.daemon

set -euo pipefail

cd "$(dirname "$0")/.."

export FARMSURV_SMOKE_MODE=1
export FARMSURV_MCP_ENABLED=1
exec .venv/bin/python -m listener.daemon
