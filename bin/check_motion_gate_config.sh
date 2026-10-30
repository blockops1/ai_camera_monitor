#!/usr/bin/env bash
# check_motion_gate_config — validate config/motion_gate_thresholds.json
#
# US-073c: static check that prevents future camera-config edits from
# breaking alerts the way OUTSIDE_FRONT_GARAGE did (US-073a).
#
# Exit 0 when the config is valid; exit 1 and print errors to stderr.
#
# Usage:
#   cd <INSTALL_DIR> && bin/check_motion_gate_config.py
#
# Called by: bin/preflight_v2_only.sh (pre-commit guard)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2_ROOT="${V2_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
CONFIG_PATH="${V2_ROOT}/config/motion_gate_thresholds.json"

# Resolve V2_ROOT from current directory if not set.
if [[ -z "${V2_ROOT:-}" ]]; then
    V2_ROOT="$(pwd)"
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "ERROR: config file not found: $CONFIG_PATH" >&2
    exit 1
fi

ERRORS=0

# Use Python to parse and validate the JSON config.
python3 - "${CONFIG_PATH}" <<'PYEOF'
import json
import sys

config_path = sys.argv[1]

with open(config_path) as f:
    raw = json.load(f)

# Strip metadata keys (start with "_").
cfg = {k: v for k, v in raw.items() if not k.startswith("_")}

errors = []

for camera, cam_cfg in cfg.items():
    if not isinstance(cam_cfg, dict):
        errors.append(f"{camera}: config must be a dict, got {type(cam_cfg).__name__}")
        continue

    # 1. Vehicle in subjects or no subjects key, OR disable_vehicle_detections: true.
    subjects = cam_cfg.get("subjects")
    if subjects is not None:
        if not isinstance(subjects, list):
            errors.append(f"{camera}: 'subjects' must be a list, got {type(subjects).__name__}")
        elif "vehicle" not in subjects:
            # Check for opt-in.
            if cam_cfg.get("disable_vehicle_detections") is not True:
                errors.append(
                    f"{camera}: subjects {subjects} must include 'vehicle' "
                    f"OR have disable_vehicle_detections: true"
                )
        elif "bicycle" not in subjects:
            errors.append(
                f"{camera}: subjects {subjects} must include 'bicycle'"
            )
    # No subjects key — falls through to global default (OK).

    # 2. Per-camera class_thresholds.vehicle in [0.30, 0.80] when populated.
    if "vehicle" in cam_cfg:
        threshold = cam_cfg["vehicle"]
        if not isinstance(threshold, (int, float)):
            errors.append(
                f"{camera}: vehicle threshold must be a number, got {type(threshold).__name__}"
            )
        elif not (0.30 <= threshold <= 0.80):
            errors.append(
                f"{camera}: vehicle threshold {threshold} not in [0.30, 0.80]"
            )

if errors:
    for e in errors:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
    sys.exit(1)

print("OK: motion gate config is valid")
PYEOF

exit $?
