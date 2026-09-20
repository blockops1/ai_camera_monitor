#!/usr/bin/env bash
# V2-046 superseded by V2-062
#
# Preflight guard: ensures the working directory is the v2 project root.
# Future US-062x stories and later must run in the v2 tree only —
# applying v1/refactor code to v2 (or vice versa) causes silent bugs.
#
# Usage: bash bin/preflight_v2_only.sh
# Exit:  0 when cwd == $V2_ROOT; 1 with ERROR message otherwise.
#
# Env-var driven — no hardcoded magic. Set V2_ROOT to override detection.
# Fallback: resolves V2_ROOT from this script's directory (dirname of $0).

set -euo pipefail

# Resolve the v2 project root from the script's own location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2_ROOT="${V2_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

CWD="$(pwd)"

if [ "$CWD" != "$V2_ROOT" ]; then
  echo "ERROR: cwd is $CWD, expected $V2_ROOT" >&2
  exit 1
fi

echo "OK: confirmed cwd=$CWD is the v2 project root."
