"""Tests for vehicle_matcher.match — match_vehicle AC verification."""

from __future__ import annotations

from vehicle_matcher.match import match_vehicle


def test_ac1_import_and_name():
    assert match_vehicle.__name__ == "match_vehicle"


def test_ac2_plate_match_case_insensitive_whitespace():
    r = match_vehicle(
        {"license_plate": "ABC123", "distinctive_features": []},
        [{"license_plate": "ABC 123", "distinctive_features": []}],
    )
    assert r["matched"] is True


def test_ac3_jaccard_ge_05():
    # roof_rack is 1/2 = 0.5
    r = match_vehicle(
        {"license_plate": None, "distinctive_features": ["roof_rack"]},
        [{"license_plate": "XYZ789", "distinctive_features": ["roof_rack", "sticker"]}],
    )
    assert r["matched"] is True


def test_ac4_jaccard_lt_05():
    # 1/4 = 0.25 < 0.5
    r = match_vehicle(
        {"license_plate": None, "distinctive_features": ["a"]},
        [{"license_plate": "XYZ789", "distinctive_features": ["b", "c", "d"]}],
    )
    assert r["matched"] is False


def test_ac5_no_match():
    r = match_vehicle(
        {"license_plate": None, "distinctive_features": []},
        [{"license_plate": "XYZ789", "distinctive_features": []}],
    )
    assert r == {"matched": False}


def test_ac6_empty_candidates():
    r = match_vehicle(
        {"license_plate": "ABC123", "distinctive_features": []},
        [],
    )
    assert r == {"matched": False}


def test_ac7_plate_none_skips_plate_match():
    r = match_vehicle(
        {"license_plate": None, "distinctive_features": ["sticker"]},
        [{"license_plate": "XYZ789", "distinctive_features": ["sticker", "dent"]}],
    )
    assert r["matched"] is True  # sticker/sticker+dent = 1/2 = 0.5


def test_ac8_multiple_candidates_first_match():
    r = match_vehicle(
        {"license_plate": "ABC123", "distinctive_features": []},
        [
            {"license_plate": "XXX", "distinctive_features": []},
            {"license_plate": "ABC 123", "distinctive_features": []},
        ],
    )
    assert r["matched"] is True
    assert r["license_plate"] == "ABC 123"
