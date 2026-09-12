"""test_pipeline.py - Vehicle matcher scoring tests (US-034e).

Covers the v1-style scored matching path in vehicle_matcher.match_vehicle:
  (a) above-threshold: scored path yields a match >= 3.0 - returns correct id
  (b) below-threshold: scored path yields < 3.0 - no match
"""

from __future__ import annotations

import os
from unittest.mock import patch

from vehicle_matcher.match import match_vehicle

# ---------------------------------------------------------------------------
# Fixtures - known-vehicle entries in v1 shape
# ---------------------------------------------------------------------------

_WHITE_SILVERADO = {
    'id': 'v_white_silverado_helper_a',
    'label': 'Operator-owned white pickup (previously helper-A vehicle)',
    'color': 'white',
    'type': 'pickup',
    'make': 'Chevrolet',
    'model': 'Silverado 1500',
    'owner': 'operator',
    'vehicle_features': {
        'body_color': 'white',
        'wheel_style': 'stock',
        'wheel_arch': 'outside_flare',
        'cab_marker_lights': False,
    },
    'distinctive_features': [
        'wheel flares mounted on outside of bed',
        'bumper sticker (text not legible)',
    ],
    'match_priority': 'color_type_then_make_model',
}

_BROWN_F150 = {
    'id': 'v_brown_f150',
    'label': 'Brown F150 pickup (camper top)',
    'color': 'black',
    'colors_alt': ['gray', 'brown', 'silver'],
    'type': 'pickup',
    'make': 'Ford',
    'model': 'F-150',
    'owner': 'operator',
    'vehicle_features': {
        'wheel_style': 'aftermarket_alloy',
        'wheel_arch': 'raptor_style_flare',
        'tire_size': 'large',
        'body_trim': 'two_tone',
        'grille': 'aftermarket',
        'lights': 'aftermarket',
        'cab_marker_lights': False,
        'bed_cover': 'camper_shell',
    },
    'distinctive_features': [
        'two-tone paint',
        'camper shell',
        'large off-road tires',
    ],
    'match_priority': 'color_type_then_make_model',
}

_TESLA_Y = {
    'id': 'v_darkblue_tesla_y_operator',
    'label': 'Operator-owned dark-blue Tesla Model Y',
    'color': 'dark blue',
    'colors_alt': ['navy', 'dark navy', 'blue', 'black',
                   'midnight blue', 'dark blue or black'],
    'type': 'suv',
    'make': 'Tesla',
    'model': 'Model Y',
    'owner': 'operator',
    'vehicle_features': {
        'wheel_style': 'aero_cover',
        'wheel_color': 'dark_charcoal',
        'roofline_style': 'fastback',
        'front_grille_style': 'closed_blank',
        'headlight_signature': 'slim_led',
        'body_trim': 'black',
        'rear_lights_signature': 'full_width_led',
        'body_color': 'dark navy',
        'cab_marker_lights': False,
    },
    'distinctive_features': [
        'deep dark navy blue paint',
        'Tesla 19-inch Gemini-style aero wheel covers',
    ],
    'match_priority': 'color_type_then_make_model',
}

_ALL_CANDIDATES = [_WHITE_SILVERADO, _BROWN_F150, _TESLA_Y]


class TestScoredAboveThreshold:
    """AC(a): vm2_result with make+model returns correct id."""

    def test_exact_make_model_color_match(self):
        """White Chevrolet Silverado 1500 pickup matches v_white_silverado."""
        vm2 = {
            'class': 'vehicle',
            'make': 'Chevrolet',
            'model': 'Silverado 1500',
            'color': 'white',
            'body_style_hint': 'pickup',
            'vehicle_features': {
                'wheel_style': 'stock',
                'wheel_arch': 'outside_flare',
            },
            'confidence': 0.92,
            'notable_details': [],
        }
        result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_white_silverado_helper_a'
        assert result['score'] >= 3.0

    def test_make_model_with_model_aliases(self):
        """F-150 signature matches F-150 via model_aliases substring."""
        f150_entry = dict(_BROWN_F150)
        f150_entry['model_aliases'] = ['F-250', 'F-450', 'Super Duty']
        vm2 = {
            'class': 'vehicle',
            'make': 'Ford',
            'model': 'F-150',
            'color': 'black',
            'body_style_hint': 'pickup',
            'vehicle_features': {
                'wheel_style': 'aftermarket_alloy',
            },
            'confidence': 0.88,
        }
        result = match_vehicle(vm2, [_WHITE_SILVERADO, f150_entry, _TESLA_Y])
        assert result['matched'] is True
        assert result['id'] == 'v_brown_f150'

    def test_color_fallback_no_make_model(self):
        """Only color+type known: score=1.0 < 3.0 threshold -> not matched."""
        vm2 = {
            'class': 'vehicle',
            'color': 'white',
            'body_style_hint': 'pickup',
            'confidence': 0.7,
        }
        result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is False

    def test_tesla_color_normalization(self):
        """dark navy matches dark blue Tesla via color normalization."""
        vm2 = {
            'class': 'vehicle',
            'make': 'Tesla',
            'model': 'Model Y',
            'color': 'dark navy',
            'body_style_hint': 'suv',
            'vehicle_features': {
                'front_grille_style': 'closed_blank',
                'rear_lights_signature': 'full_width_led',
            },
            'confidence': 0.95,
        }
        result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_darkblue_tesla_y_operator'


class TestScoredBelowThreshold:
    """AC(b): vm2_result for an unmatched vehicle returns not matched."""

    def test_completely_unmatched_vehicle(self):
        """Honda Civic not in candidates -> no match."""
        vm2 = {
            'class': 'vehicle',
            'make': 'Honda',
            'model': 'Civic',
            'color': 'red',
            'body_style_hint': 'sedan',
            'vehicle_features': {'wheel_style': 'alloy'},
            'confidence': 0.91,
        }
        result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is False

    def test_low_confidence_partial_match(self):
        """Partial make match but model does not overlap -> score too low."""
        vm2 = {
            'class': 'vehicle',
            'make': 'Ford',
            'model': 'Mustang',
            'color': 'red',
            'body_style_hint': 'coupe',
            'confidence': 0.65,
        }
        result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is False

    def test_no_candidates(self):
        """Empty candidate list -> no match."""
        vm2 = {
            'class': 'vehicle',
            'make': 'Chevrolet',
            'model': 'Silverado 1500',
            'color': 'white',
            'body_style_hint': 'pickup',
        }
        result = match_vehicle(vm2, [])
        assert result['matched'] is False


class TestFallbacks:
    """Ensure Jaccard fallback still functions."""

    def test_jaccard_high_similarity(self):
        """Jaccard >= 0.5 on distinctive_features returns candidate."""
        vm2 = {
            'class': 'vehicle',
            'distinctive_features': [
                'wheel flares mounted on outside of bed',
                'bumper sticker (text not legible)',
                'white paint',
            ],
        }
        result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_white_silverado_helper_a'

    def test_below_jaccard_threshold(self):
        """Jaccard < 0.5 -> no match."""
        vm2 = {
            'class': 'vehicle',
            'distinctive_features': ['red racing stripes', 'spoiler'],
        }
        result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is False


class TestMinScoreEnv:
    """MATCH_MIN_SCORE env var overrides the default 3.0."""

    def test_custom_min_score_allows_partial_match(self):
        """Set MATCH_MIN_SCORE=1.0 -> color_type match (score=1.0) succeeds."""
        vm2 = {
            'class': 'vehicle',
            'color': 'white',
            'body_style_hint': 'pickup',
            'confidence': 0.7,
        }
        with patch.dict(os.environ, {'MATCH_MIN_SCORE': '1.0'}):
            result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_white_silverado_helper_a'

    def test_high_min_score_blocks_match(self):
        """Set MATCH_MIN_SCORE=10.0 -> no match even with strong signal."""
        vm2 = {
            'class': 'vehicle',
            'make': 'Chevrolet',
            'model': 'Silverado 1500',
            'color': 'white',
            'body_style_hint': 'pickup',
            'vehicle_features': {
                'wheel_style': 'stock',
                'wheel_arch': 'outside_flare',
            },
            'confidence': 0.92,
        }
        with patch.dict(os.environ, {'MATCH_MIN_SCORE': '10.0'}):
            result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is False

    def test_invalid_env_falls_back_to_default(self):
        """Non-numeric MATCH_MIN_SCORE -> falls back to default 3.0."""
        vm2 = {
            'class': 'vehicle',
            'make': 'Chevrolet',
            'model': 'Silverado 1500',
            'color': 'white',
            'body_style_hint': 'pickup',
            'vehicle_features': {
                'wheel_style': 'stock',
                'wheel_arch': 'outside_flare',
            },
            'confidence': 0.92,
        }
        with patch.dict(os.environ, {'MATCH_MIN_SCORE': 'not-a-number'}):
            result = match_vehicle(vm2, _ALL_CANDIDATES)
        assert result['matched'] is True
        assert result['id'] == 'v_white_silverado_helper_a'
