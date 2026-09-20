"""test_docs_cleanup_policy.py — Verify docs/cleanup-policy.md exists and contains
all required sections and content.

Tests:
  1. File exists at docs/cleanup-policy.md.
  2. Contains >= 5 top-level section headers (## ...).
  3. Documents the 20 GB cap (references 20 GB / 20GB / FRAME_MAX_BYTES).
  4. Documents the kill switch env var FARMSURV_CLEANUP_DISABLED.
  5. Contains the exact phrase "do not just raise the cap" verbatim.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = PROJECT_ROOT / "docs" / "cleanup-policy.md"


def test_cleanup_policy_file_exists():
    """AC1: The cleanup-policy.md file exists."""
    assert DOC_PATH.exists(), f"{DOC_PATH} does not exist"


def test_cleanup_policy_has_five_sections():
    """AC2: The document has at least 5 section headers."""
    content = DOC_PATH.read_text(encoding="utf-8")
    section_count = sum(1 for line in content.splitlines() if line.startswith("## "))
    assert section_count >= 5, f"Expected >= 5 sections, found {section_count}"


def test_cleanup_policy_documents_cap():
    """AC3: The document references the 20 GB cap."""
    content = DOC_PATH.read_text(encoding="utf-8")
    assert (
        "20 GB" in content or "20GB" in content or "FRAME_MAX_BYTES" in content
    ), "Document must reference the 20 GB cap or FRAME_MAX_BYTES"


def test_cleanup_policy_documents_kill_switch():
    """AC4: The document references FARMSURV_CLEANUP_DISABLED."""
    content = DOC_PATH.read_text(encoding="utf-8")
    assert (
        "FARMSURV_CLEANUP_DISABLED" in content
    ), "Document must reference the FARMSURV_CLEANUP_DISABLED kill switch"


def test_cleanup_policy_recovery_guidance():
    """AC5: The document contains the verbatim recovery guidance."""
    content = DOC_PATH.read_text(encoding="utf-8")
    assert (
        "do not just raise the cap" in content
    ), 'Document must contain the phrase "do not just raise the cap" verbatim'
