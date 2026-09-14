"""Tests for telegram_formatter.match_alert — status_wording_for (6 combos).

Covers all 3 classifications x matched/unmatched = 6 combinations.
"""

from __future__ import annotations

from telegram_formatter.match_alert import status_wording_for


def test_vehicle_unrecognized():
    assert status_wording_for("vehicle", False) == "unrecognized vehicle"


def test_vehicle_recognized():
    assert status_wording_for("vehicle", True) == "recognized vehicle"


def test_person_unrecognized():
    assert status_wording_for("person", False) == "unrecognized person"


def test_person_recognized():
    assert status_wording_for("person", True) == "recognized person"


def test_animal_unrecognized():
    assert status_wording_for("animal", False) == "unrecognized animal"


def test_animal_recognized():
    assert status_wording_for("animal", True) == "recognized animal"
