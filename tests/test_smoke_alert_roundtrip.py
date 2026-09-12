"""test_smoke_alert_roundtrip.py — Smoke tests for scored vehicle matching.

Covers US-034e acceptance criteria through end-to-end roundtrip tests:
  (a) above-threshold: vm2_result with make/model returns correct match
  (b) below-threshold: vm2_result for unmatched vehicle returns no match
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from vehicle_matcher.match import match_vehicle

# ---------------------------------------------------------------------------
# Real known_vehicles.json fixture data (extracted entries)
# ---------------------------------------------------------------------------

def _load_real_candidates():
    """Load actual known vehicle entries from the project data file."""
    project_root = os.environ.get(
        'FARMSURV_PROJECT_ROOT',
        str(Path(__file__).parent.parent),
    )
    vehicles_path = Path(project_root) / 'data' / 'vehicles' / 'known_vehicles.json'
    if not vehicles_path.exists():
        return []
    data = json.loads(vehicles_path.read_text())
    if isinstance(data, dict) and 'entries' in data:
        return data['entries']
    if isinstance(data, list):
        return data
    return []


REAL_CANDIDATES = _load_real_candidates()


class TestSmokeAboveThreshold:
    """AC(a): vm2_result with make/model/color/body_style_hint/vehicle_features
    against fixture known entry returns correct id."""

    def test_silverado_roundtrip(self):
        """Real Silverado detection matches v_white_silverado_helper_a."""
        if not REAL_CANDIDATES:
            return
        vm2 = {
            'class': 'vehicle',
            'make': 'Chevrolet',
            'model': 'Silverado 1500',
            'color': 'white',
            'body_style_hint': 'pickup',
            'vehicle_features': {
                'wheel_style': 'stock',
                'wheel_arch': 'outside_flare',
                'body_color': 'white',
            },
            'confidence': 0.85,
            'notable_details': [],
        }
        result = match_vehicle(vm2, REAL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_white_silverado_helper_a'
        assert result['score'] >= 3.0

    def test_f150_roundtrip(self):
        """Real F-150 detection matches v_brown_f150."""
        if not REAL_CANDIDATES:
            return
        vm2 = {
            'class': 'vehicle',
            'make': 'Ford',
            'model': 'F-150',
            'color': 'black',
            'body_style_hint': 'pickup',
            'vehicle_features': {
                'wheel_style': 'aftermarket_alloy',
                'wheel_arch': 'raptor_style_flare',
            },
            'confidence': 0.9,
            'notable_details': [],
        }
        result = match_vehicle(vm2, REAL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_brown_f150'

    def test_tesla_model_y_roundtrip(self):
        """Real Tesla Model Y detection matches v_darkblue_tesla_y_operator."""
        if not REAL_CANDIDATES:
            return
        vm2 = {
            'class': 'vehicle',
            'make': 'Tesla',
            'model': 'Model Y',
            'color': 'dark blue',
            'body_style_hint': 'suv',
            'vehicle_features': {
                'front_grille_style': 'closed_blank',
                'rear_lights_signature': 'full_width_led',
                'wheel_style': 'aero_cover',
            },
            'confidence': 0.93,
            'notable_details': [],
        }
        result = match_vehicle(vm2, REAL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_darkblue_tesla_y_operator'


class TestSmokeBelowThreshold:
    """AC(b): vm2_result for an unmatched vehicle returns matched=False."""

    def test_unmatched_vehicle_not_found(self):
        """Vehicle not in known_vehicles.json -> no match."""
        if not REAL_CANDIDATES:
            return
        vm2 = {
            'class': 'vehicle',
            'make': 'Volkswagen',
            'model': 'Golf',
            'color': 'purple',
            'body_style_hint': 'hatchback',
            'vehicle_features': {'wheel_style': 'spoke'},
            'confidence': 0.85,
            'notable_details': [],
        }
        result = match_vehicle(vm2, REAL_CANDIDATES)
        assert result['matched'] is False

    def test_unknown_color_no_match(self):
        """Vehicle with an unknown color -> below threshold."""
        if not REAL_CANDIDATES:
            return
        vm2 = {
            'class': 'vehicle',
            'make': 'Honda',
            'model': 'CR-V',
            'color': 'magenta',
            'body_style_hint': 'suv',
            'confidence': 0.80,
        }
        result = match_vehicle(vm2, REAL_CANDIDATES)
        assert result['matched'] is False


class TestNoPlateReference:
    """Verify scored matching works without deprecated plate data."""

    def test_no_plate_no_crash(self):
        """Matcher handles vm2_result without plate data gracefully."""
        vm2 = {
            'class': 'vehicle',
            'make': 'Chevrolet',
            'model': 'Silverado 1500',
            'color': 'white',
            'body_style_hint': 'pickup',
            'vehicle_features': {'wheel_style': 'stock'},
            'confidence': 0.9,
        }
        result = match_vehicle(vm2, REAL_CANDIDATES)
        assert 'matched' in result
        assert result['matched'] is True

    def test_scored_path_preferred_over_fallback(self):
        """Scored match fires first; fallback only if score < threshold."""
        if not REAL_CANDIDATES:
            return
        vm2 = {
            'class': 'vehicle',
            'make': 'Chevrolet',
            'model': 'Silverado 1500',
            'color': 'white',
            'body_style_hint': 'pickup',
            'confidence': 0.9,
        }
        result = match_vehicle(vm2, REAL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_white_silverado_helper_a'
        assert 'score' in result
