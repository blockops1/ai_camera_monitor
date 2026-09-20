"""Tests for infra/launchd/com.farm.surveillance.v2-cleanup.plist."""
from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PLIST_PATH = (
    Path(__file__).resolve().parent.parent
    / "infra" / "launchd" / "com.farm.surveillance.v2-cleanup.plist"
)


def _parse_plist_xml() -> ET.Element:
    """Parse the plist XML, stripping any leading HTML comment block."""
    raw = PLIST_PATH.read_text(encoding="utf-8")
    # Strip leading <!-- ... --> comments and any whitespace before <?xml
    stripped = raw.lstrip()
    while stripped.startswith("<!--"):
        end = stripped.index("-->", 2) + 3
        stripped = stripped[end:].lstrip()
    return ET.fromstring(stripped)


def _find_value(root: ET.Element, key_name: str) -> ET.Element | None:
    """Find the value sibling of a <key> element (searches <dict> children)."""
    for el in root:
        if el.tag == "dict":
            children = list(el)
            for i, child in enumerate(children):
                if child.tag == "key" and child.text == key_name:
                    if i + 1 < len(children):
                        return children[i + 1]
    return None


# ---- existence ----

def test_plist_file_exists():
    assert PLIST_PATH.exists(), f"{PLIST_PATH} does not exist"


# ---- XML validity ----

def test_plist_xml_parses():
    """The plist must be valid XML that plutil and ElementTree can parse."""
    result = subprocess.run(
        ["plutil", "-lint", str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"plutil -lint failed: {result.stderr}"
    _parse_plist_xml()


# ---- required keys ----

def test_plist_has_startinterval_3600():
    root = _parse_plist_xml()
    val = _find_value(root, "StartInterval")
    assert val is not None, "StartInterval key missing"
    assert val.text == "3600", f"StartInterval is {val.text}, expected 3600"


def test_plist_program_arguments_contains_infra_cleanup():
    root = _parse_plist_xml()
    args_key = _find_value(root, "ProgramArguments")
    assert args_key is not None, "ProgramArguments key missing"
    # args_key is the value (array) returned by _find_value
    arr = args_key
    strings = [s.text for s in arr.findall("string")]
    assert "-m" in strings
    assert "infra.cleanup" in strings


def test_plist_label():
    root = _parse_plist_xml()
    val = _find_value(root, "Label")
    assert val is not None
    assert val.text == "com.farm.surveillance.v2-cleanup"


def test_plist_run_at_load_false():
    root = _parse_plist_xml()
    val = _find_value(root, "RunAtLoad")
    assert val is not None
    assert val.tag == "false"


def test_plist_environment_variables():
    root = _parse_plist_xml()
    env_key = _find_value(root, "EnvironmentVariables")
    assert env_key is not None
    # env_key is the value (dict) returned by _find_value
    env_dict = env_key
    keys = [k.text for k in env_dict.findall("key")]
    idx = keys.index("FARMSURV_CLEANUP_DISABLED")
    strs = env_dict.findall("string")
    assert strs[idx].text == "0"


def test_plist_log_paths():
    root = _parse_plist_xml()
    out_val = _find_value(root, "StandardOutPath")
    err_val = _find_value(root, "StandardErrorPath")
    assert out_val is not None
    assert err_val is not None
    assert out_val.text is not None
    assert err_val.text is not None
    assert "cleanup-stdout.log" in out_val.text
    assert "cleanup-stderr.log" in err_val.text
