"""
time_of_day.py — Day/night classification for Resaca, GA farm.

Phase.111 §11.41 (per-plan pair to 6B.110 quick_classifier factory).

PURPOSE
=======
The motion gate wants to load a different YOLO model after dusk (fine-tuned on
ExDark + our nighttime frames) vs during the day (vanilla COCO). Switching time
must track the actual sun, not a fixed hour, because astronomical dusk at our
latitude (~34.6°N) shifts by ~30 min between solstices.

DESIGN
======
Uses `suntime` (offline, no API) for astronomical sunrise/sunset at the farm's
fixed coordinates (34.5782°N, -84.9438°W — Resaca, Georgia). Adds a 30-minute
buffer on each side for "civil twilight" — the moment the sky is noticeably
dark enough that Reolink IR LEDs flip on, which is what we actually want to
detect with a nighttime-tuned model.

WHY A SEPARATE MODULE
=====================
- One place to verify EDT-fixed-UTC-4 logic (per memory: never use
  ZoneInfo("America/New_York"), that auto-switches to EST in November)
- Easy to unit-test in isolation (one function, deterministic for any given date)
- If we ever move cameras, one constant changes

INPUTS:
    - No env vars, no config files. Coordinates are hard-coded module constants.

OUTPUTS:
    is_night_at_edt(dt_utc: datetime | None = None) -> bool
        True iff the given moment is in the civil-twilight night window.
    get_night_window_edt(dt_utc: datetime | None = None) -> (start, end)
        Returns EDT (start, end) of the current/upcoming night window.

PUBLIC API:
    is_night_at_edt(...)        — primary check used by quick_classifier factory
    get_night_window_edt(...)   — returns (start, end) of the relevant window
    next_sunset_edt(...)        — for diagnostics / Telegram messages
    next_sunrise_edt(...)       — for diagnostics / Telegram messages

DOES NOT DO:
    - Call any external API (suntime is purely offline)
    - Account for elevation/atmospheric refraction (good enough for ±10 min)
    - Handle polar circle edge cases (Resaca is 34.6°N, no polar day/night possible)
    - Account for DST transitions (we use fixed UTC-4 year-round per memory rule)

CALLED BY:
    infra/quick_classifier.py: get_classifier_for_time() factory (Phase.111)
    tests/test_time_of_day.py: unit tests

CALLS INTO:
    - suntime.Sun: astronomical sunrise/sunset (offline, ~50 KB install)

RELATED:
    - infra/quick_classifier.py: factory that uses is_night_at_edt()
    - models/yolov8n.onnx: day model, loaded when is_night_at_edt() is False
    - models/yolov8n-night.onnx: night model (Phase.111 build), loaded when True
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from suntime import Sun

# Per memory: EDT = fixed UTC-4, no auto-fallback to EST in November.
# ZoneInfo("America/New_York") auto-switches and reintroduces a UTC-leak bug.
EDT = timezone(timedelta(hours=-4), name="EDT")

# Resaca, Georgia (Gordon County). Suntime takes these straight.
# Verified against suntime 2026-08-23: sunrise 07:07 EDT, sunset 20:16 EDT.
FARM_LATITUDE = 34.5782
FARM_LONGITUDE = -84.9438

# Civil twilight buffer. Astronomical sunrise/sunset is when the sun's center
# is exactly at the horizon. By the time the sky is visibly dark enough for
# IR LEDs to matter, we're ~30 min past sunset. Reverse at dawn.
NIGHT_BUFFER_MINUTES = 30


_cached_sun: Sun | None = None


def _sun() -> Sun:
    """Singleton Sun instance for this farm location."""
    global _cached_sun
    if _cached_sun is None:
        _cached_sun = Sun(FARM_LATITUDE, FARM_LONGITUDE)
    return _cached_sun


def _to_edt(dt: datetime) -> datetime:
    """Convert datetime to EDT. Naive datetimes assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(EDT)


def _today_sunset_edt(edt_moment: datetime) -> datetime:
    """Today's astronomical sunset (EDT) for the calendar day of edt_moment.

    Suntime quirk: get_sunset_time(t) returns the most-recent sunset at or
    before t. To get TODAY's sunset reliably, we need to pass a reference
    instant that's AFTER today's sunset — which is 8 PM EDT of the same day
    (= 24:00 UTC, or midnight UTC of the next calendar day). Suntime's
    "most-recent sunset" at midnight UTC is today's sunset.

    For edt_moment before today's sunset (e.g., noon), this still returns
    today's sunset (8 PM EDT) — that's correct because astronomical sunset
    happens later that day.
    """
    ref_edt = edt_moment.replace(hour=20, minute=0, second=0, microsecond=0)
    ref_utc = ref_edt.astimezone(UTC)
    # suntime lacks type stubs; get_sunset_time returns Any at type level
    sett_utc = _sun().get_sunset_time(ref_utc)
    return sett_utc.astimezone(EDT)  # type: ignore[no-any-return]


def _next_sunrise_edt(edt_moment: datetime) -> datetime:
    """The next astronomical sunrise at or after edt_moment (EDT).

    Suntime quirk: get_sunrise_time(t) returns the next sunrise at or after t.
    For edt_moment at, say, 8 PM EDT (= 24:00 UTC), suntime returns tomorrow's
    sunrise — exactly what we want.

    For edt_moment at noon EDT (= 16:00 UTC), suntime returns THIS evening's
    not-yet-happening sunrise which doesn't exist. To force tomorrow's sunrise,
    we pass an instant strictly after today's sunset — 9 PM EDT is safe even
    on the night we already started.
    """
    # Use 9 PM EDT (= 01:00 UTC next day) as reference. By then today's
    # sunset has just happened and suntime returns tomorrow's sunrise.
    ref_edt = edt_moment.replace(hour=21, minute=0, second=0, microsecond=0)
    ref_utc = ref_edt.astimezone(UTC)
    # suntime lacks type stubs; see comment in _next_sunset_edt
    rise_utc = _sun().get_sunrise_time(ref_utc)
    return rise_utc.astimezone(EDT)  # type: ignore[no-any-return]


def get_night_window_edt(
    dt_utc: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return (night_start, night_end) — the relevant civil-twilight window in EDT.

    Window is always:
        night_start = today's astronomical sunset + NIGHT_BUFFER_MINUTES  (EDT)
        night_end   = the next astronomical sunrise - NIGHT_BUFFER_MINUTES (EDT)

    Note: a night window spans EDT midnight, so night_end's date is the day
    after night_start's date.

    If dt_utc is during the day, this returns the upcoming tonight window.
    If dt_utc is during the night, this returns the current window.
    """
    now_edt = _to_edt(dt_utc or datetime.now(UTC))

    today_sunset = _today_sunset_edt(now_edt)
    next_sunrise = _next_sunrise_edt(now_edt)

    night_start = today_sunset + timedelta(minutes=NIGHT_BUFFER_MINUTES)
    night_end = next_sunrise - timedelta(minutes=NIGHT_BUFFER_MINUTES)

    return (night_start, night_end)


def is_night_at_edt(dt_utc: datetime | None = None) -> bool:
    """True iff the given moment is in the civil-twilight night window.

    Examples for Resaca, GA in late August (sunset ~20:16 EDT, sunrise ~07:07 EDT):
        EDT 19:00                  → False (1 hour before sunset)
        EDT 20:46 (20:16 + 30 min) → True (civil twilight has begun, IR LEDs on)
        EDT 02:00                  → True (deep night)
        EDT 06:37 (07:07 - 30 min) → True (civil twilight still — IR LEDs still on)
        EDT 07:07                  → False (sunrise — IR LEDs flip off)

    Logic: a moment is "night" iff it's past tonight's start (after sunset+buffer)
    OR it's in the pre-dawn twilight (after midnight EDT, before tomorrow's
    sunrise-buffer). Anything between pre-dawn end and tonight's sunset start
    is daytime.

    Args:
        dt_utc: Optional UTC-aware datetime. Defaults to now.

    Returns:
        True if it's nighttime (civil twilight or later), False otherwise.
    """
    now_edt = _to_edt(dt_utc or datetime.now(UTC))
    night_start, night_end = get_night_window_edt(now_edt)

    # Case 1: After tonight's start → night.
    if now_edt >= night_start:
        return True

    # Case 2: Before tonight's start AND it's still before the morning
    # civil twilight end (i.e., we passed midnight EDT). Compare time-of-day:
    # now_edt.time() < night_end.time() means we're in the post-midnight
    # pre-dawn window, not in the late afternoon.
    if now_edt < night_end and now_edt.time() < night_end.time():  # noqa: SIM103 (readable as one pre-dawn check)
        return True

    return False


def next_sunset_edt(dt_utc: datetime | None = None) -> datetime:
    """Next astronomical sunset (EDT) strictly after the given moment."""
    now_edt = _to_edt(dt_utc or datetime.now(UTC))
    today_sunset = _today_sunset_edt(now_edt)
    if now_edt < today_sunset:
        return today_sunset
    # Past today's sunset — get tomorrow's
    tomorrow_edt = now_edt + timedelta(days=1)
    tomorrow_sunset = _today_sunset_edt(tomorrow_edt)
    return tomorrow_sunset


def next_sunrise_edt(dt_utc: datetime | None = None) -> datetime:
    """Next astronomical sunrise (EDT) strictly after the given moment."""
    now_edt = _to_edt(dt_utc or datetime.now(UTC))
    next_sunrise = _next_sunrise_edt(now_edt)
    if now_edt < next_sunrise:
        return next_sunrise
    # Past tomorrow's sunrise — get the day after
    day_after_edt = now_edt + timedelta(days=2)
    day_after_sunrise = _next_sunrise_edt(day_after_edt)
    return day_after_sunrise


def now_edt_iso(dt_utc: datetime | None = None) -> str:
    """Format the current moment (or the given UTC moment) as
    "YYYY-MM-DD HH:MM:SS EDT" for use in user-facing text.

    Phase.114 (2026-08-25) — Note wants the actual Telegram-send
    time in the footer of every Telegram alert body, not the
    captured_at event time (which is in the header). This helper
    gives a single canonical formatting call for that footer.

    Note: callers should pass the datetime captured AT THE MOMENT
    the Telegram send was attempted, not the moment the event was
    detected. The pipeline captures this in a local variable right
    before calling send_photo_with_caption() / send_message().
    """
    now_edt = _to_edt(dt_utc or datetime.now(UTC))
    return now_edt.strftime("%Y-%m-%d %H:%M:%S EDT")
