"""
Tests for infra/vision_cache.py — cached vision-result + person-seen state.

Pure unit tests. We override DATA_DIR per-test via the _DATA_DIR_PRIVATE
fixture (monkeypatched) so cache files land in tmp_path, not in the real
infra.paths.DATA_DIR. Each test starts with an empty cache.

Covered:
    - _parse_iso handles Reolink format (no colon in offset) and standard
    - set_last_vision / get_last_vision round-trip
    - get_all_cached_vision returns all entries
    - clear_last_vision(camera) and clear_last_vision() (all)
    - record_person_seen / get_last_person_seen / seconds_since_last_person
    - seconds_since_last_person returns None when no record exists
    - clear_last_person_seen(camera) and clear_last_person_seen() (all)
    - corrupt cache file → graceful fallback to {}
    - missing cache file → graceful empty cache
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infra import vision_cache


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Redirect the cache files to tmp_path for the duration of the test.

    vision_cache imports DATA_DIR at module-load time and re-exports it.
    We patch both the module-level DATA_DIR and the underlying _DATA_DIR
    import so all read/write paths resolve under tmp_path.
    """
    monkeypatch.setattr(vision_cache, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(vision_cache, "CACHE_FILE", str(tmp_path / "last_vision.json"))
    monkeypatch.setattr(vision_cache, "PERSON_SEEN_FILE", str(tmp_path / "last_person_seen.json"))
    return tmp_path


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


def test_parse_iso_reolink_format() -> None:
    """Reolink webhooks send `+0000` (no colon in offset). Standard
    datetime.fromisoformat rejects this. vision_cache normalizes first."""
    result = vision_cache._parse_iso("2026-07-18T17:36:08.000+0000")
    assert result is not None
    assert result.year == 2026
    assert result.hour == 17


def test_parse_iso_standard_iso() -> None:
    """Standard `+00:00` form also works."""
    result = vision_cache._parse_iso("2026-07-18T17:36:08.000+00:00")
    assert result is not None
    assert result.hour == 17


def test_parse_iso_negative_offset() -> None:
    """Negative UTC offsets work too (e.g. `-0600`)."""
    result = vision_cache._parse_iso("2026-07-18T17:36:08.000-0600")
    assert result is not None


def test_parse_iso_none_returns_none() -> None:
    assert vision_cache._parse_iso(None) is None


def test_parse_iso_empty_returns_none() -> None:
    assert vision_cache._parse_iso("") is None


def test_parse_iso_garbage_returns_none() -> None:
    assert vision_cache._parse_iso("not-a-timestamp") is None


# ---------------------------------------------------------------------------
# set_last_vision / get_last_vision round-trip
# ---------------------------------------------------------------------------


def test_set_then_get_last_vision(isolated_cache: Path) -> None:
    vision_cache.set_last_vision(
        "Front Door",
        {"primary_subject": "person", "objects_detected": ["person"]},
        frame_path="/tmp/frame.jpg",
        timestamp="2026-08-13T10:00:00.000-0400",
    )
    result = vision_cache.get_last_vision("Front Door")
    assert result is not None
    assert result["timestamp"] == "2026-08-13T10:00:00.000-0400"
    assert result["vision_result"]["primary_subject"] == "person"
    assert result["frame_path"] == "/tmp/frame.jpg"
    assert "saved_at" in result


def test_get_last_vision_missing_returns_none(isolated_cache: Path) -> None:
    assert vision_cache.get_last_vision("never-seen") is None


def test_set_last_vision_default_timestamp(isolated_cache: Path) -> None:
    """When timestamp=None, it's auto-populated with now()."""
    vision_cache.set_last_vision("Door", {"primary_subject": "person"})
    result = vision_cache.get_last_vision("Door")
    assert result is not None
    assert result["timestamp"] is not None
    # ISO format includes a T separator
    assert "T" in result["timestamp"]


# ---------------------------------------------------------------------------
# get_all_cached_vision
# ---------------------------------------------------------------------------


def test_get_all_cached_vision_empty(isolated_cache: Path) -> None:
    assert vision_cache.get_all_cached_vision() == {}


def test_get_all_cached_vision_multiple_cameras(isolated_cache: Path) -> None:
    vision_cache.set_last_vision("A", {"primary_subject": "person"})
    vision_cache.set_last_vision("B", {"primary_subject": "car"})
    all_cached = vision_cache.get_all_cached_vision()
    assert set(all_cached.keys()) == {"A", "B"}
    assert all_cached["A"]["vision_result"]["primary_subject"] == "person"
    assert all_cached["B"]["vision_result"]["primary_subject"] == "car"


# ---------------------------------------------------------------------------
# clear_last_vision
# ---------------------------------------------------------------------------


def test_clear_last_vision_specific_camera(isolated_cache: Path) -> None:
    vision_cache.set_last_vision("A", {"primary_subject": "person"})
    vision_cache.set_last_vision("B", {"primary_subject": "car"})
    vision_cache.clear_last_vision("A")
    assert vision_cache.get_last_vision("A") is None
    assert vision_cache.get_last_vision("B") is not None


def test_clear_last_vision_all_when_none(isolated_cache: Path) -> None:
    vision_cache.set_last_vision("A", {"primary_subject": "person"})
    vision_cache.set_last_vision("B", {"primary_subject": "car"})
    vision_cache.clear_last_vision(None)
    assert vision_cache.get_last_vision("A") is None
    assert vision_cache.get_last_vision("B") is None


def test_clear_last_vision_missing_camera_no_error(isolated_cache: Path) -> None:
    """Clearing a camera that's not in the cache should not raise."""
    vision_cache.clear_last_vision("never-existed")


# ---------------------------------------------------------------------------
# record_person_seen / get_last_person_seen / seconds_since_last_person
# ---------------------------------------------------------------------------


def test_record_and_get_person_seen(isolated_cache: Path) -> None:
    vision_cache.record_person_seen("Door", "2026-08-13T10:00:00.000-0400")
    assert vision_cache.get_last_person_seen("Door") == "2026-08-13T10:00:00.000-0400"


def test_record_person_seen_default_now(isolated_cache: Path) -> None:
    """when_iso=None → auto-populated."""
    vision_cache.record_person_seen("Door")
    result = vision_cache.get_last_person_seen("Door")
    assert result is not None
    assert "T" in result


def test_get_last_person_seen_missing_returns_none(isolated_cache: Path) -> None:
    assert vision_cache.get_last_person_seen("never-seen") is None


def test_seconds_since_last_person_when_never_seen(isolated_cache: Path) -> None:
    """No prior record → None (treated as first arrival downstream)."""
    assert vision_cache.seconds_since_last_person("never-seen") is None


def test_seconds_since_last_person_known(isolated_cache: Path) -> None:
    """2-hour gap with explicit now_iso → 7200 seconds."""
    vision_cache.record_person_seen("Door", "2026-08-13T08:00:00.000-0400")
    elapsed = vision_cache.seconds_since_last_person(
        "Door", now_iso="2026-08-13T10:00:00.000-0400"
    )
    assert elapsed == pytest.approx(7200.0, abs=1.0)


def test_seconds_since_last_person_with_bad_timestamp(isolated_cache: Path) -> None:
    """Garbage in cache returns None, not a crash."""
    isolated_cache.joinpath("last_person_seen.json").write_text(
        json.dumps({"Door": "garbage"})
    )
    assert vision_cache.seconds_since_last_person("Door") is None


# ---------------------------------------------------------------------------
# clear_last_person_seen
# ---------------------------------------------------------------------------


def test_clear_last_person_seen_specific_camera(isolated_cache: Path) -> None:
    vision_cache.record_person_seen("A")
    vision_cache.record_person_seen("B")
    vision_cache.clear_last_person_seen("A")
    assert vision_cache.get_last_person_seen("A") is None
    assert vision_cache.get_last_person_seen("B") is not None


def test_clear_last_person_seen_all(isolated_cache: Path) -> None:
    vision_cache.record_person_seen("A")
    vision_cache.record_person_seen("B")
    vision_cache.clear_last_person_seen(None)
    assert vision_cache.get_last_person_seen("A") is None
    assert vision_cache.get_last_person_seen("B") is None


# ---------------------------------------------------------------------------
# Corruption / missing-file resilience
# ---------------------------------------------------------------------------


def test_corrupt_cache_file_returns_empty(isolated_cache: Path) -> None:
    """If last_vision.json is unparseable, get_* returns empty/None gracefully."""
    isolated_cache.joinpath("last_vision.json").write_text("{not json")
    assert vision_cache.get_last_vision("Door") is None
    assert vision_cache.get_all_cached_vision() == {}


def test_corrupt_person_seen_file_returns_empty(isolated_cache: Path) -> None:
    isolated_cache.joinpath("last_person_seen.json").write_text("{not json")
    assert vision_cache.get_last_person_seen("Door") is None