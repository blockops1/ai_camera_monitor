"""US-032d: privacy regression test for data/known/people/.

Asserts that tracked repo files do NOT contain operator names, license plates,
or other PII.  The known_people.json placeholder must be an empty list.
"""

from __future__ import annotations

import json
from pathlib import Path

from infra.paths import PERSON_KNOWN_FILE


def test_known_people_json_is_empty_list():
    """AC: known_people.json contains only [] (no real enrollments)."""
    p = Path(PERSON_KNOWN_FILE)
    assert p.exists(), f"PERSON_KNOWN_FILE {p} does not exist"
    data = json.loads(p.read_text())
    assert data == [], (
        f"known_people.json contains data: {data}. "
        "Repository must only track the empty placeholder."
    )


def test_no_pii_in_known_people():
    """AC: no operator names, plates, or other PII in the tracked file."""
    p = Path(PERSON_KNOWN_FILE)
    text = p.read_text().strip()
    # Known PII patterns that must NOT appear
    pii_patterns = ["plate", "license", "ssn", "phone", "address"]
    text_lower = text.lower()
    for pattern in pii_patterns:
        assert pattern not in text_lower, (
            f"PII pattern '{pattern}' found in {p}: {text}"
        )
