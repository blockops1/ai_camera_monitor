"""Unit tests for infra.timezone — Phase.99 EDT conversion helpers.

the operator directive (2026-08-19): UTC timestamps from Reolink webhooks must be
displayed everywhere (Telegram bodies, alert queue log lines, audit records)
as fixed EDT (UTC-4) year-round, NOT ZoneInfo auto-fallback.

These tests pin the conversion contract end-to-end and prevent regressions
to the pre-fix behavior (raw UTC strings flowing through).
"""

from datetime import UTC, datetime

import pytest

from infra.timezone import EDT, format_dt_edt, parse_iso, to_edt_string

# --- parse_iso: robust ISO-8601 parser ---


def test_parse_iso_reolink_format_no_colon() -> None:
    """Reolink ``+0000`` (no colon) is the dominant production format."""
    result = parse_iso("2026-08-19T16:12:33.000+0000")
    assert result is not None
    assert result.year == 2026 and result.month == 8 and result.day == 19
    assert result.hour == 16 and result.minute == 12 and result.second == 33
    assert result.tzinfo is not None


def test_parse_iso_standard_iso_format() -> None:
    """Standard ``+00:00`` (with colon) — sanity for the helper."""
    result = parse_iso("2026-08-19T16:12:33.000+00:00")
    assert result is not None
    assert result.hour == 16 and result.minute == 12


def test_parse_iso_z_suffix() -> None:
    """Z-suffix occasionally appears on newer Reolink firmware."""
    result = parse_iso("2026-08-19T16:12:33Z")
    assert result is not None
    assert result.tzinfo is not None
    assert result.hour == 16


def test_parse_iso_negative_offset() -> None:
    """``-0600`` and ``-06:00`` both parse; one is Reolink, one is standard."""
    for s in ("2026-08-19T10:12:33.000-0600", "2026-08-19T10:12:33.000-06:00"):
        result = parse_iso(s)
        assert result is not None
        assert result.tzinfo is not None
        assert result.hour == 10


def test_parse_iso_garbage_returns_none() -> None:
    """Garbage input must NEVER raise — returns None per API contract."""
    for bad in ("", None, "not a date", "2026-13-50T99:99:99+0000"):
        result = parse_iso(bad)
        assert result is None, f"parse_iso({bad!r}) should be None, got {result!r}"


# --- format_dt_edt: tz-aware datetime → "YYYY-MM-DD HH:MM:SS EDT" ---


def test_format_dt_edt_basic() -> None:
    """The canonical case: 16:12:33 UTC → 12:12:33 EDT."""
    utc = datetime(2026, 8, 19, 16, 12, 33, tzinfo=UTC)
    assert format_dt_edt(utc) == "2026-08-19 12:12:33 EDT"


def test_format_dt_edt_microseconds_truncated() -> None:
    """Microseconds are dropped from the output (we don't need them in a Telegram body)."""
    utc = datetime(2026, 8, 19, 16, 12, 33, 123456, tzinfo=UTC)
    assert format_dt_edt(utc) == "2026-08-19 12:12:33 EDT"


def test_format_dt_edt_asserts_naive() -> None:
    """Naive datetimes should fail loudly — they indicate a caller bug."""
    naive = datetime(2026, 8, 19, 12, 12, 33)  # noqa: DTZ001  ← test fixture, intentionally naive
    with pytest.raises(AssertionError, match="tz-aware"):
        format_dt_edt(naive)


def test_format_dt_edt_offset_handling() -> None:
    """A datetime already in EDT (UTC-4) is formatted without further shifts."""
    already_edt = datetime(2026, 8, 19, 12, 12, 33, tzinfo=EDT)
    assert format_dt_edt(already_edt) == "2026-08-19 12:12:33 EDT"


# --- to_edt_string: the production entry point ---


def test_to_edt_string_reolink_payload() -> None:
    """The exact string Reolink sends goes in, the EDT string comes out."""
    assert (
        to_edt_string("2026-08-19T16:12:33.000+0000")
        == "2026-08-19 12:12:33 EDT"
    )


def test_to_edt_string_z_suffix() -> None:
    """Z-suffix variant should also convert correctly."""
    assert to_edt_string("2026-08-19T16:12:33Z") == "2026-08-19 12:12:33 EDT"


def test_to_edt_string_no_colon_offset() -> None:
    """Negative offset without colon works the same way.

    The whole timestamp is in UTC-6 (already local for that zone). Adding
    that offset to UTC gives 20:00 UTC, which in EDT (UTC-4) is 16:00.
    """
    result = to_edt_string("2026-08-19T14:00:00.000-0600")
    assert result == "2026-08-19 16:00:00 EDT"


def test_to_edt_string_preserves_garbage() -> None:
    """Garbage input returns the input as-is — never raises, never blanks it."""
    for bad in ("", None, "garbage", "2026-13-99Txx+xxxx"):
        result = to_edt_string(bad)
        if bad is None:
            assert result == ""
        else:
            assert result == bad


def test_to_edt_string_round_trip_farm_vision_payload() -> None:
    """Real-world Reolink payload from a 2026-08-19 CAM2 capture (12:14 EDT)."""
    # 16:14:32 UTC = 12:14:32 EDT
    assert (
        to_edt_string("2026-08-19T16:14:32.000+0000")
        == "2026-08-19 12:14:32 EDT"
    )


# --- Big behavior pin: the exact bug the user reported ---


def test_bug_reolink_utc_no_longer_leaks_through() -> None:
    """Pre-fix, Reolink ``+0000`` strings flowed through Telegram bodies as-is.
    Post-fix, every conversion produces a string that:
      - contains the literal " EDT" suffix
      - does NOT contain any UTC marker like "+0000" or "Z"
      - does NOT contain the raw hour from the UTC source (we shifted -4h)
    """
    raw = "2026-08-19T16:12:33.000+0000"
    out = to_edt_string(raw)
    assert " EDT" in out
    assert "+0000" not in out
    assert "+00:00" not in out
    assert not out.endswith("Z")
    # and the hour must be 12, not 16
    assert out.startswith("2026-08-19 12:12:33 EDT")
