"""Tests for data/vehicles/known_vehicles.json import + vehicle_matcher integration.

Verifies that the 12 v1 known-vehicle entries port correctly to v2 and that
match_vehicle returns >=1 candidate when querying against them.
"""

from __future__ import annotations

import json
from pathlib import Path

from infra.paths import VEHICLE_KNOWN_FILE
from listener.pipeline import _load_candidates
from vehicle_matcher.match import match_vehicle

# ---------------------------------------------------------------------------
# AC: file exists and is valid JSON with 12 entries
# ---------------------------------------------------------------------------

def test_file_exists_and_has_12_entries():
    """AC: known_vehicles.json has 12 entries."""
    p = Path(VEHICLE_KNOWN_FILE)
    assert p.exists(), f"VEHICLE_KNOWN_FILE {p} does not exist"
    entries = json.loads(p.read_text())
    assert len(entries) == 12


def test_candidates_loads_12():
    """AC: _load_candidates() returns 12 entries."""
    candidates = _load_candidates()
    assert len(candidates) == 12


def test_path_is_v2_not_refactor():
    """AC: VEHICLE_KNOWN_FILE resolves to farm-surveillance-v2, not refactor."""
    assert "farm-surveillance-v2" in str(VEHICLE_KNOWN_FILE)
    assert "farm-surveillance-refactor" not in str(VEHICLE_KNOWN_FILE)


# ---------------------------------------------------------------------------
# Synthetic match tests: query known vehicles via Jaccard
# ---------------------------------------------------------------------------

def test_match_carson_white():
    """Carson's white pickup — Jaccard match on distinctive_features.

    v_carson_white has 2 features; querying with both gives Jaccard=1.0.
    """
    candidates = _load_candidates()
    carson = next(c for c in candidates if c["id"] == "v_carson_white")
    result = match_vehicle(
        {
            "license_plate": None,
            "distinctive_features": [
                "wheel flares mounted on outside of bed (NOT a dent — see vehicle_features.wheel_arch_note)",
                "bumper sticker (text not legible from BFC frame)",
            ],
        },
        candidates,
    )
    assert result["matched"] is True
    assert result["id"] == carson["id"]


def test_match_tesla():
    """Tesla Model Y — Jaccard match on distinctive_features.

    v_rolf_darkblue_tesla_y has 4 features; querying with all gives Jaccard=1.0.
    """
    candidates = _load_candidates()
    tesla = next(c for c in candidates if c["id"] == "v_rolf_darkblue_tesla_y")
    result = match_vehicle(
        {
            "license_plate": None,
            "distinctive_features": [
                "deep dark navy blue paint (appears almost black-blue in low light)",
                "Tesla 19-inch Gemini-style aero wheel covers (dark charcoal, multi-spoke appearance)",
                "black window trim / black side mirror caps",
                "no visible badging on rear (clean Tesla body)",
            ],
        },
        candidates,
    )
    assert result["matched"] is True
    assert result["id"] == tesla["id"]


def test_match_tractor():
    """Red Yanmar 60hp — Jaccard match on distinctive_features.

    v_red_yanmar_tractor has 6 features; querying with 3 gives 3/6 = 0.5 Jaccard.
    """
    candidates = _load_candidates()
    tractor = next(c for c in candidates if c["id"] == "v_red_yanmar_tractor")
    result = match_vehicle(
        {
            "license_plate": None,
            "distinctive_features": [
                "enclosed cab with tinted windows (defining structural feature — strongest signal)",
                "red Yanmar hood with 'YANMAR' branding visible on grille (structural)",
                "cream/tan wheels (front and rear) (structural)",
            ],
        },
        candidates,
    )
    assert result["matched"] is True
    assert result["id"] == tractor["id"]
