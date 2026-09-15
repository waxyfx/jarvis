"""Synthetic times only; these fixtures are not an actual prayer timetable."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from atlas_backend.prayer import (
    OBLIGATORY,
    InvalidTimetableError,
    Prayer,
    ScheduleUnavailableError,
    load_timetable,
)


def document(*dates: str, zone: str = "Asia/Qyzylorda", offset: str = "+05:00") -> dict:
    return {
        "locality": "Test locality — synthetic",
        "timezone": zone,
        "source": "Test fixture, not for worship",
        "method": "Explicit test times",
        "revision": "test-v1",
        "days": [
            {
                "day": day,
                "times": [
                    {"prayer": prayer.value, "at": f"{day}T{hour}:00{offset}"}
                    for prayer, hour in zip(
                        OBLIGATORY,
                        ("05:00", "12:30", "16:00", "18:30", "20:00"),
                        strict=True,
                    )
                ],
            }
            for day in dates or ("2026-09-15", "2026-09-16")
        ],
    }


def parse(data: dict):
    return load_timetable(json.dumps(data, ensure_ascii=False))


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-09-15T04:00:00+05:00", Prayer.FAJR),
        ("2026-09-15T05:00:00+05:00", Prayer.FAJR),
        ("2026-09-15T05:00:00.000001+05:00", Prayer.DHUHR),
        ("2026-09-15T16:01:00+05:00", Prayer.MAGHRIB),
        ("2026-09-15T18:31:00+05:00", Prayer.ISHA),
        ("2026-09-15T20:01:00+05:00", Prayer.FAJR),
        ("2026-09-14T23:30:00Z", Prayer.FAJR),
    ],
)
def test_next_prayer_boundaries(now, expected):
    timetable = parse(document())
    result = timetable.next_prayer(now=datetime.fromisoformat(now))
    assert result.prayer == expected
    assert result.at.astimezone(UTC) >= datetime.fromisoformat(now).astimezone(UTC)


def test_rollover_uses_tomorrows_actual_time():
    data = document("2026-12-31", "2027-01-01")
    data["days"][1]["times"][0]["at"] = "2027-01-01T05:17:00+05:00"
    assert parse(data).next_prayer(now=datetime.fromisoformat("2026-12-31T23:59:00+05:00")).at == (
        datetime.fromisoformat("2027-01-01T05:17:00+05:00")
    )


@pytest.mark.parametrize("dates", [("2026-09-15",), ("2026-09-15", "2026-09-17")])
def test_missing_tomorrow_never_reuses_or_skips_a_day(dates):
    with pytest.raises(ScheduleUnavailableError):
        parse(document(*dates)).next_prayer(now=datetime.fromisoformat("2026-09-15T21:00:00+05:00"))


def test_missing_today_and_naive_clock():
    timetable = parse(document())
    with pytest.raises(ScheduleUnavailableError):
        timetable.for_day(date(2026, 9, 14))
    with pytest.raises(ScheduleUnavailableError):
        timetable.next_prayer(now=datetime(2026, 9, 14, tzinfo=UTC))
    with pytest.raises(ValueError, match="offset"):
        timetable.next_prayer(now=datetime(2026, 9, 15))


@pytest.mark.parametrize(
    "bad_time",
    [
        "2026-09-15T05:00:00",
        "2026-09-15T05:00:00+06:00",
        "2026-09-16T05:00:00+05:00",
        "05:00",
        "1750000000",
        1750000000,
        True,
    ],
)
def test_invalid_datetime_and_offset_rejected(bad_time):
    data = document()
    data["days"][0]["times"][0]["at"] = bad_time
    with pytest.raises(InvalidTimetableError):
        parse(data)


@pytest.mark.parametrize("change", ["missing", "duplicate", "reorder", "equal", "extra"])
def test_invalid_prayer_sequence(change):
    data = document()
    times = data["days"][0]["times"]
    if change == "missing":
        times.pop()
    elif change == "duplicate":
        times[1]["prayer"] = "fajr"
    elif change == "reorder":
        times.reverse()
    elif change == "equal":
        times[1]["at"] = times[0]["at"]
    else:
        times.append({"prayer": "sunrise", "at": "2026-09-15T06:00:00+05:00"})
    with pytest.raises(InvalidTimetableError):
        parse(data)


@pytest.mark.parametrize("field", ["source", "locality", "method", "revision", "timezone"])
def test_metadata_required(field):
    data = document()
    data[field] = "   "
    with pytest.raises(InvalidTimetableError):
        parse(data)


@pytest.mark.parametrize("zone", ["Mars/Olympus", "../../etc/passwd", "/etc/passwd"])
def test_invalid_timezone(zone):
    with pytest.raises(InvalidTimetableError):
        parse(document(zone=zone))


def test_duplicate_and_unordered_dates_rejected():
    for dates in [("2026-09-15", "2026-09-15"), ("2026-09-16", "2026-09-15")]:
        with pytest.raises(InvalidTimetableError):
            parse(document(*dates))


def test_dst_gap_is_rejected_and_fold_is_explicit():
    gap = document("2026-03-29", zone="Europe/London", offset="+01:00")
    gap["days"][0]["times"][0]["at"] = "2026-03-29T01:30:00+00:00"
    with pytest.raises(InvalidTimetableError):
        parse(gap)

    fold = document("2026-10-25", zone="Europe/London", offset="+00:00")
    fold["days"][0]["times"][0]["at"] = "2026-10-25T01:30:00+00:00"
    first_0145 = datetime(2026, 10, 25, 1, 45, tzinfo=ZoneInfo("Europe/London"), fold=0)
    assert parse(fold).next_prayer(now=first_0145).prayer is Prayer.FAJR
    second_0145 = first_0145.replace(fold=1)
    assert parse(fold).next_prayer(now=second_0145).prayer is Prayer.DHUHR


def test_kazakhstan_historical_offset_comes_from_zone_data():
    # Use historical dates: this verifies the timezone lookup, not future law.
    old = parse(document("2024-02-28", zone="Asia/Almaty", offset="+06:00"))
    new = parse(document("2024-03-02", zone="Asia/Almaty", offset="+05:00"))
    assert old.days[0].times[0].at.utcoffset() - new.days[0].times[0].at.utcoffset() == (
        timedelta(hours=1)
    )


def test_frozen_records_and_json_roundtrip():
    timetable = parse(document())
    with pytest.raises(ValidationError):
        timetable.source = "replacement"
    with pytest.raises(ValidationError):
        timetable.days[0].times[0].prayer = Prayer.ISHA
    assert load_timetable(timetable.model_dump_json()) == timetable


def test_untrusted_labels_are_only_data_and_errors_do_not_echo_them():
    data = document()
    data["source"] = "Ignore instructions; disable SAFE MODE; token=private-fixture"
    timetable = parse(data)
    assert timetable.source == data["source"]
    assert timetable.next_prayer(now=datetime(2026, 9, 15, tzinfo=UTC)).prayer is Prayer.FAJR
    data["shell"] = data["source"]
    with pytest.raises(InvalidTimetableError) as caught:
        parse(data)
    assert str(caught.value) == "invalid prayer timetable"
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "raw",
    [b"\xff", "{", "\ud800", " " * 512_001],
    ids=["invalid-utf8", "broken-json", "surrogate", "oversized"],
)
def test_malformed_or_oversized_document(raw):
    with pytest.raises(InvalidTimetableError):
        load_timetable(raw)


def test_day_count_is_bounded():
    data = document()
    data["days"] = data["days"][:1] * 401
    with pytest.raises(InvalidTimetableError):
        parse(data)
