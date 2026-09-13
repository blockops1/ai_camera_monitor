"""Privacy regression test for data/known/people/known_people.json.

Verifies that the tracked known_people.json file contains ONLY an empty
list placeholder -- no real-person enrollment entries have leaked into
version control.

Mirrors the pattern in tests/test_check_no_private_data.py (git-tracked
privacy scanner) and tests/test_known_vehicles.py (anonymization checks).
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve the tracked placeholder path relative to this repo root.
# The real enrollment data lives at the same path but is gitignored; the
# tracked placeholder is what we assert on.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
KNOWN_PEOPLE_FILE = ROOT / "data" / "known" / "people" / "known_people.json"


def test_known_people_is_empty_list():
    """AC1-3: known_people.json is a top-level list of length 0."""
    assert KNOWN_PEOPLE_FILE.exists(), (
        f"{KNOWN_PEOPLE_FILE} does not exist -- US-032a placeholder missing"
    )
    data = json.loads(KNOWN_PEOPLE_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, list), (
        f"known_people.json top-level is {type(data).__name__}, expected list"
    )
    assert len(data) == 0, (
        f"known_people.json contains {len(data)} entries; should be empty list "
        f"placeholder. Leaked entries: {data}"
    )
