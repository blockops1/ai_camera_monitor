"""
Probe: compare the two matchers on a small set of canonical signatures.

- Matcher A (production):  infra.vehicle_matcher.match_vehicle_scored
- Matcher B (paused):      vehicle_matcher.matcher.match_signature

Run them on the same input dicts, show the verdict each returns, the
scored breakdown, and whether they agree.

NOTE: This is a structural probe, not a quality benchmark. "Better"
is a judgment about match quality on real-world signatures, which
the match path hasn't exercised since 6B.96 (2026-08-19). The probe
documents the *behavior difference* between the two engines on
hand-crafted fixtures, so a future session can decide whether to
resume the migration.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

ROOT = Path("<install-path>/ai_camera_monitor")
sys.path.insert(0, str(ROOT))

# Load the 2 known vehicles from the live data dir
data = json.loads((ROOT / "data/vehicles/known_vehicles.json").read_text())
known_vehicles = data["vehicles"]
print(f"Loaded {len(known_vehicles)} known vehicles from data/vehicles/known_vehicles.json")
for kv in known_vehicles:
    print(f"  - {kv['id']:<25} {kv.get('color','?'):<8} {kv.get('type','?'):<10} {kv.get('make','?'):<12} {kv.get('model','?')}")
print()

# Canonical signature fixtures — these are what the vision pipeline
# WOULD produce for a close match, a partial match, and a no-match.
# The fixtures are heuristic, not real production data.
FIXTURES = [
    {
        "name": "v_carson_white perfect match",
        "signature": {
            "color": "white",
            "type": "pickup",
            "make": "Chevrolet",
            "model": "Silverado 1500",
            "vehicle_features": {"cab_marker_lights": True, "bed_cover": "none"},
        },
    },
    {
        "name": "v_carson_white alt color",
        "signature": {
            "color": "silver",  # close to white via color_normalization
            "type": "pickup",
            "make": "Chevrolet",
            "model": "Silverado",
            "vehicle_features": {},
        },
    },
    {
        "name": "v_carson_white make mismatch",
        "signature": {
            "color": "white",
            "type": "pickup",
            "make": "Ford",
            "model": "F-150",
            "vehicle_features": {},
        },
    },
    {
        "name": "unknown vehicle",
        "signature": {
            "color": "blue",
            "type": "sedan",
            "make": "Honda",
            "model": "Civic",
            "vehicle_features": {},
        },
    },
]

# Production matcher (infra.vehicle_matcher.match_vehicle_scored)
from infra.vehicle_matcher import match_vehicle_scored
from infra.vehicle_matcher import score_top_n as prod_score_top_n

# Paused-migration matcher (vehicle_matcher.matcher.match_signature)
from vehicle_matcher.matcher import (
    MatchVerdict,
    NoMatch,
    match_signature,
)
from vehicle_matcher.matcher import (
    score_top_n as new_score_top_n,
)

# Run both against each fixture
for fx in FIXTURES:
    sig = cast(dict, fx["signature"])
    print(f"=== {fx['name']} ===")
    print(f"  sig: {json.dumps(sig, default=str)}")

    # Production matcher
    prod_result = match_vehicle_scored(sig, known_vehicles)
    prod_top_n = prod_score_top_n(sig, known_vehicles, n=3)
    print("  PRODUCTION (match_vehicle_scored):")
    if prod_result is None:
        print("    verdict: NO MATCH")
    else:
        kv, score, gap, breakdowns = prod_result
        print(f"    verdict: MATCH {kv['id']} (score={score:.3f}, gap={gap:.3f})")
    print("    top-3:")
    for kv, score, bd in prod_top_n:
        print(f"      {kv['id']:<25} score={score:.3f}  bd={list(bd.keys())[:5]}...")

    # Paused-migration matcher
    new_result = match_signature(sig, known_vehicles)
    new_top_n = new_score_top_n(sig, known_vehicles, n=3)
    print("  PAUSED-MIGRATION (match_signature):")
    if isinstance(new_result, NoMatch):
        print(f"    verdict: NO MATCH ({new_result.reason})")
    else:
        print(f"    verdict: MATCH {new_result.known_vehicle['id']} "
              f"(score={new_result.score:.3f}, gap={new_result.gap:.3f})")
        print(f"    breakdowns: {list(new_result.breakdowns.keys())[:5]}")
    print("    top-3:")
    for kv_id, score, bd in new_top_n:
        print(f"      {kv_id:<25} score={score:.3f}  bd={list(bd.keys())[:5]}...")

    # Verdict comparison
    prod_match = prod_result is not None
    new_match = isinstance(new_result, MatchVerdict)
    print(f"  AGREEMENT: prod={'MATCH' if prod_match else 'NO_MATCH'}"
          f"  new={'MATCH' if new_match else 'NO_MATCH'}"
          f"  {'AGREE' if prod_match == new_match else 'DISAGREE'}")
    print()
