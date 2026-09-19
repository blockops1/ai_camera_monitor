"""Tests for VM1 response validation in vision_analyzer.

Covers parse_vm1_response / _validate_vm1 hard-fail on missing keys,
happy-path passthrough, and the INFO log line.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from infra.vision_analyzer import (
    VisionAnalyzerError,
    _validate_vm1,
    parse_vm1_response,
    verify_class,
)

# ---------------------------------------------------------------------------
# Happy path — full schema passes
# ---------------------------------------------------------------------------

_VALID_FULL = {
    "class": "vehicle",
    "subclass": "sedan",
    "confidence": "likely",
    "count": 1,
    "face_visible": False,
    "notes": None,
}

_VALID_ANIMAL = {
    "class": "animal",
    "subclass": "dog",
    "confidence": "definite",
    "count": 2,
    "face_visible": False,
    "notes": "two dogs in yard",
}

_VALID_UNSURE = {
    "class": "unsure",
    "subclass": None,
    "confidence": "unsure",
    "count": None,
    "face_visible": None,
    "notes": "blurry crop",
}

_VALID_PERSON_FACE = {
    "class": "person",
    "subclass": "adult",
    "confidence": "definite",
    "count": 1,
    "face_visible": True,
    "notes": None,
}


@pytest.mark.parametrize("valid_input", [
    _VALID_FULL,
    _VALID_ANIMAL,
    _VALID_UNSURE,
    _VALID_PERSON_FACE,
])
def test_validate_vm1_passes_full_schema(valid_input):
    """_validate_vm1 returns the input dict unchanged when all keys present."""
    result = _validate_vm1(valid_input)
    assert result is valid_input  # returns same object


def test_parse_vm1_response_returns_dict():
    """parse_vm1_response passes through a valid response dict."""
    result = parse_vm1_response(_VALID_FULL)
    assert result is _VALID_FULL


# ---------------------------------------------------------------------------
# AC5: missing required keys → VisionAnalyzerError
# ---------------------------------------------------------------------------

_MISSING_CLASS = {
    "subclass": None, "confidence": "likely",
    "count": None, "face_visible": None, "notes": None,
}

_MISSING_SUBCLASS = dict(_VALID_FULL)
_MISSING_SUBCLASS.pop("subclass")

_MISSING_COUNT = dict(_VALID_FULL)
_MISSING_COUNT.pop("count")

_MISSING_ALL_BUT_CLASS = {"class": "vehicle"}


def test_validate_vm1_missing_class_raises():
    """Missing 'class' key → VisionAnalyzerError with 'required' in message."""
    with pytest.raises(VisionAnalyzerError, match="required"):
        _validate_vm1(_MISSING_CLASS)


def test_validate_vm1_missing_subclass_raises():
    """Missing 'subclass' key → VisionAnalyzerError."""
    with pytest.raises(VisionAnalyzerError, match="required"):
        _validate_vm1(_MISSING_SUBCLASS)


def test_validate_vm1_missing_count_raises():
    """Missing 'count' key → VisionAnalyzerError."""
    with pytest.raises(VisionAnalyzerError, match="required"):
        _validate_vm1(_MISSING_COUNT)


def test_validate_vm1_missing_all_but_class_raises():
    """Missing 5 of 6 keys → VisionAnalyzerError names all missing."""
    with pytest.raises(VisionAnalyzerError) as exc_info:
        _validate_vm1(_MISSING_ALL_BUT_CLASS)
    assert "subclass" in str(exc_info.value)
    assert "count" in str(exc_info.value)


# ---------------------------------------------------------------------------
# INFO log telemetry line
# ---------------------------------------------------------------------------

def test_parse_vm1_response_logs_info(caplog):
    """parse_vm1_response emits an INFO log with class, subclass, confidence."""
    with caplog.at_level(logging.INFO, logger="infra.vision_analyzer"):
        parse_vm1_response(_VALID_FULL)

    assert "vm1:" in caplog.text
    assert "class=vehicle" in caplog.text
    assert "subclass=sedan" in caplog.text
    assert "confidence=likely" in caplog.text


# ---------------------------------------------------------------------------
# verify_class integration — parses response from _post_vision mock
# ---------------------------------------------------------------------------

def test_verify_class_calls_parse_vm1_response():
    """verify_class feeds _post_vision output through parse_vm1_response."""
    mock_post = patch(
        "infra.vision_analyzer._post_vision",
        return_value={
            "class": "person", "subclass": None,
            "confidence": "likely", "count": 1,
            "face_visible": False, "notes": "person near gate",
        },
    )
    mock_b64 = patch(
        "infra.vision_analyzer._b64",
        return_value={"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,fake"}},
    )

    with mock_post as fake_post, mock_b64:
        result = verify_class("/tmp/a.jpg", "/tmp/b.jpg")

    fake_post.assert_called_once()
    assert result["class"] == "person"
    assert result["subclass"] is None
    assert result["face_visible"] is False


def test_verify_class_raises_on_missing_keys():
    """verify_class propagates VisionAnalyzerError when vm1 response is incomplete."""
    mock_post = patch(
        "infra.vision_analyzer._post_vision",
        return_value={"class": "vehicle", "confidence": "definite"},
    )
    mock_b64 = patch(
        "infra.vision_analyzer._b64",
        return_value={"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,fake"}},
    )

    with mock_post, mock_b64, pytest.raises(VisionAnalyzerError, match="required"):
        verify_class("/tmp/a.jpg", "/tmp/b.jpg")
