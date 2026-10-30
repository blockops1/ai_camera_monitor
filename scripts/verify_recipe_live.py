"""US-065c: end-to-end live verify of camera recipes against 6 farm cameras.

Runs apply_recipe + verify_recipe on each live camera, captures before/after
state into a JSON evidence file at docs/PHASE-V2-065-verification.json.

Usage::

    python scripts/verify_recipe_live.py

No listener integration — just a standalone verification tool.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Env bootstrap: load camera-creds.env so reolink-cli resolves auth.
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CREDS_FILE = _PROJECT_ROOT / "camera-creds.env"

if _CREDS_FILE.is_file() and "FARM_CAMERA_CREDS_FILE" not in os.environ:
    os.environ["FARM_CAMERA_CREDS_FILE"] = str(_CREDS_FILE)

# Gateway address — use env var or default to local gateway.
_GATEWAY_ADDR = os.environ.get("FARM_REOLINK_GATEWAY_ADDR", "127.0.0.1:9000")

# ---------------------------------------------------------------------------
# Imports from infra.reolink
# ---------------------------------------------------------------------------

from infra.reolink import (
    ReolinkClient,
    ReolinkCommandError,
    ReolinkTimeoutError,
    apply_recipe,
    verify_recipe,
)

RECIPE_PATH = str(_PROJECT_ROOT / "config" / "camera_recipes.toml")
EVIDENCE_PATH = str(_PROJECT_ROOT / "docs" / "PHASE-V2-065-verification.json")

CAMERA_META: dict[str, dict[str, str]] = {
    "front": {"ip": "[REDACTED-LAN-IP]", "model": "RLC-833A"},
    "back": {"ip": "[REDACTED-LAN-IP]", "model": "RLC-833A"},
    "outside_front_garage": {"ip": "[REDACTED-LAN-IP]", "model": "RLC-510A"},
    "outside_front_solar": {"ip": "[REDACTED-LAN-IP]", "model": "RLC-510A"},
    "outside_front_power": {"ip": "[REDACTED-LAN-IP]", "model": "RLC-510A"},
    "outside_back_solar": {"ip": "[REDACTED-LAN-IP]", "model": "RLC-510A"},
}


def _read_live_state(client: ReolinkClient, camera: str) -> dict:
    """Read the full detect_ai state for one camera (before/after snapshot)."""
    state: dict = {"detect_ai": {}}
    for atype in ("person", "vehicle", "dog_cat"):
        try:
            cfg = client.detect_ai_get(atype=atype, camera=camera)
            state["detect_ai"][atype] = {
                "sensitivity": cfg.sensitivity,
                "stay_time": (
                    cfg.person_stay_time
                    if atype == "person"
                    else cfg.vehicle_stay_time
                    if atype == "vehicle"
                    else cfg.dog_cat_stay_time
                ),
            }
        except (ReolinkCommandError, ReolinkTimeoutError):
            state["detect_ai"][atype] = {"error": "read failed"}
    return state


def _scrub_sensitive(data: dict | list) -> dict | list:
    """Return a copy with all password/token/secret/key fields removed.

    Keys are filtered by substring containment: any key containing
    'password', 'token', 'secret', 'key', 'pass', or 'passwd'
    (case-insensitive) is removed.
    """
    sensitive_patterns = ("password", "token", "secret", "key", "pass", "passwd")
    if isinstance(data, dict):
        return {
            k: _scrub_sensitive(v) if isinstance(v, (dict, list)) else v
            for k, v in data.items()
            if not any(p in k.lower() for p in sensitive_patterns)
        }
    if isinstance(data, list):
        return [_scrub_sensitive(item) for item in data]
    return data


def main() -> int:
    """Run live verification and write evidence JSON.

    Returns 0 if all cameras match the recipe, 1 if any mismatch.
    """
    client = ReolinkClient(gateway_addr=_GATEWAY_ADDR)
    cameras = list(CAMERA_META.keys())
    evidence: dict = {
        "prd": "PHASE-V2-065",
        "ran_at": datetime.now(UTC).isoformat(),
        "recipe_path": RECIPE_PATH,
        "cameras": [],
        "summary": {"total": len(cameras), "matched": 0, "mismatched": 0},
    }

    for cam in cameras:
        meta = CAMERA_META[cam]
        print(f"[1/4] Reading live state for {cam} ({meta['ip']})...", flush=True)
        before = _read_live_state(client, cam)

        print(f"[2/4] Applying recipe for {cam}...", flush=True)
        try:
            apply_recipe(RECIPE_PATH, camera=cam, gateway_addr=_GATEWAY_ADDR)
        except (ReolinkCommandError, ReolinkTimeoutError) as exc:
            print(f"  ERROR applying recipe for {cam}: {exc}", flush=True)
            evidence["cameras"].append({
                "camera": cam,
                "ip": meta["ip"],
                "model": meta["model"],
                "before": before,
                "after": {},
                "match": False,
                "diffs": [f"apply failed: {exc}"],
            })
            continue

        print(f"[3/4] Verifying recipe for {cam}...", flush=True)
        try:
            verify_results = verify_recipe(RECIPE_PATH, camera=cam, gateway_addr=_GATEWAY_ADDR)
        except (ReolinkCommandError, ReolinkTimeoutError) as exc:
            print(f"  ERROR verifying recipe for {cam}: {exc}", flush=True)
            evidence["cameras"].append({
                "camera": cam,
                "ip": meta["ip"],
                "model": meta["model"],
                "before": before,
                "after": {},
                "match": False,
                "diffs": [f"verify failed: {exc}"],
            })
            continue

        # Read after state
        print(f"[4/4] Reading live state after apply for {cam}...", flush=True)
        after = _read_live_state(client, cam)

        # Extract match + diffs from verify results
        match = True
        diffs: list[str] = []
        for vr in verify_results:
            if not vr.match:
                match = False
            diffs.extend(vr.diffs)

        evidence["cameras"].append({
            "camera": cam,
            "ip": meta["ip"],
            "model": meta["model"],
            "before": before,
            "after": after,
            "match": match,
            "diffs": diffs,
        })

    # Summary
    total = len(evidence["cameras"])
    matched = sum(1 for c in evidence["cameras"] if c["match"])
    evidence["summary"]["matched"] = matched
    evidence["summary"]["mismatched"] = total - matched

    # Print per-camera results
    all_match = True
    for cam_data in evidence["cameras"]:
        status = "match" if cam_data["match"] else "MISMATCH"
        print(f"{cam_data['camera']}: {cam_data['ip']} {cam_data['model']} {status}")
        if not cam_data["match"]:
            all_match = False
            for d in cam_data["diffs"]:
                print(f"  diff: {d}")

    summary_line = f"{matched}/{total} cameras match recipe"
    print(summary_line)

    # Scrub sensitive data and write evidence JSON
    safe_evidence = _scrub_sensitive(evidence)
    os.makedirs(os.path.dirname(EVIDENCE_PATH), exist_ok=True)
    with open(EVIDENCE_PATH, "w") as f:
        json.dump(safe_evidence, f, indent=2)
    print(f"Evidence written to {EVIDENCE_PATH}")

    return 0 if all_match else 1


if __name__ == "__main__":
    sys.exit(main())
