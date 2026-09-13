"""Privacy regression test for data/known/people/known_people.json.

Verifies that the tracked known_people.json file contains ONLY an empty
list placeholder -- no real-person enrollment entries have leaked into
version control.

Mirrors the pattern in tests/test_check_no_private_data.py (git-tracked
privacy scanner) and tests/test_known_vehicles.py (anonymization checks).

Two checks:
  AC1: the tracked placeholder is a top-level list of length 0
  AC2: scanned for operator-PII patterns (names, plates, phone, address)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from infra.paths import PERSON_KNOWN_FILE


def test_known_people_is_empty_list():
    """AC1: known_people.json is a top-level list of length 0."""
    p = Path(PERSON_KNOWN_FILE)
    assert p.exists(), (
        f"{p} does not exist -- US-032a placeholder missing"
    )
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, list), (
        f"known_people.json top-level is {type(data).__name__}, expected list"
    )
    assert len(data) == 0, (
        f"known_people.json contains {len(data)} entries; should be empty "
        f"list placeholder. Leaked entries: {data}"
    )


def test_no_pii_in_known_people():
    """AC2: no operator names, plates, phone, address, SSN in the tracked file."""
    p = Path(PERSON_KNOWN_FILE)
    text = p.read_text(encoding="utf-8").strip()
    pii_patterns = ["plate", "license", "ssn", "phone", "address"]
    text_lower = text.lower()
    for pattern in pii_patterns:
        assert pattern not in text_lower, (
            f"PII pattern '{pattern}' found in {p}: {text}"
        )
