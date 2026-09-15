"""Asking "во сколько намаз?", and being reminded before one.

The arithmetic is tested next door. This is about what the model is handed and
what gets said out loud: that the answer leads with the next prayer rather than
reciting six times, and that a reminder arrives before a prayer rather than at
it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from atlas_backend.notify.rules import Moment, Schedule, decide_all
from atlas_backend.prayer.schedule import OBLIGATORY
from atlas_backend.prayer.times import AsrMethod, compute
from atlas_backend.prayer.tools import PrayerSettings, PrayerToolError, run_prayer_tool
from atlas_shared.enums import NotificationKind
from atlas_shared.tools.catalog import CATALOG

ALMATY = ZoneInfo("Asia/Almaty")
SETTINGS = PrayerSettings(latitude=43.238, longitude=76.889, zone=ALMATY)

#: 2026-09-15 in Almaty. Dhuhr 11:49, Asr 15:18, Maghrib 18:03, Isha 19:34.
#: These moved by a minute when the engine became adhanpy. The two agreed to
#: within a minute at Almaty across a year, so neither was wrong; these are
#: simply the numbers the shipped implementation produces.
TODAY = datetime(2026, 9, 15, 12, 0, tzinfo=ALMATY)


def at(hour: int, minute: int = 0) -> datetime:
    return TODAY.replace(hour=hour, minute=minute)


class TestTheAnswer:
    def test_it_gives_the_whole_day(self) -> None:
        result = run_prayer_tool(SETTINGS, TODAY, "prayer.today", {})

        assert set(result) >= {"fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"}
        assert result["dhuhr"] == "11:49"

    def test_it_leads_with_the_next_one(self) -> None:
        """What a person actually wants when they ask. Without it the model
        reads six times aloud and the listener does the arithmetic."""
        result = run_prayer_tool(SETTINGS, at(12, 0), "prayer.today", {})

        assert result["next"]["prayer"] == "asr"
        assert result["next"]["name"] == "Аср"
        assert result["next"]["at"] == "15:18"
        assert result["next"]["in_minutes"] == pytest.approx(198, abs=2)

    def test_after_the_last_prayer_there_is_no_next_one(self) -> None:
        result = run_prayer_tool(SETTINGS, at(23, 0), "prayer.today", {})

        assert "next" not in result

    def test_sunrise_is_listed_but_never_offered_as_next(self) -> None:
        """It ends Fajr rather than beginning anything."""
        result = run_prayer_tool(SETTINGS, at(5, 0), "prayer.today", {})

        assert result["sunrise"] == "05:32"
        assert result["next"]["prayer"] == "dhuhr"

    def test_the_school_changes_the_answer(self) -> None:
        """An hour's difference, and not a detail to pick on someone's behalf."""
        hanafi = PrayerSettings(
            latitude=43.238, longitude=76.889, zone=ALMATY, asr=AsrMethod.HANAFI
        )

        standard_asr = run_prayer_tool(SETTINGS, TODAY, "prayer.today", {})["asr"]
        hanafi_asr = run_prayer_tool(hanafi, TODAY, "prayer.today", {})["asr"]

        assert standard_asr == "15:18"
        assert hanafi_asr == "16:11"

    def test_a_time_that_came_from_a_rule_says_so(self) -> None:
        """In a Norwegian June the sun never reaches the twilight angle, so Fajr
        and Isha are a scholarly convention rather than an observation. The time
        is still given - someone there still prays - but the model is told which
        ones it should not present as astronomy."""
        north = PrayerSettings(latitude=64.0, longitude=11.0, zone=ZoneInfo("Europe/Oslo"))

        result = run_prayer_tool(
            north, datetime(2026, 6, 21, 12, tzinfo=ZoneInfo("Europe/Oslo")), "prayer.today", {}
        )

        assert result["by_convention"] == ["fajr", "isha"]
        assert result["fajr"], "the time itself is still there"

    def test_an_ordinary_day_carries_no_such_warning(self) -> None:
        """At Almaty no rule is ever needed, so this key must stay absent."""
        assert "by_convention" not in run_prayer_tool(SETTINGS, TODAY, "prayer.today", {})

    def test_somewhere_no_rule_can_help_is_reported_rather_than_crashing(self) -> None:
        """Svalbard in December: the sun does not rise, and no convention
        produces a sunrise."""
        polar = PrayerSettings(
            latitude=78.22, longitude=15.65, zone=ZoneInfo("Arctic/Longyearbyen")
        )

        with pytest.raises(PrayerToolError, match="no prayer times"):
            run_prayer_tool(
                polar,
                datetime(2026, 12, 21, 12, tzinfo=ZoneInfo("Arctic/Longyearbyen")),
                "prayer.today",
                {},
            )

    def test_an_unknown_prayer_tool_is_refused_rather_than_crashing(self) -> None:
        with pytest.raises(PrayerToolError, match="not something I can tell you"):
            run_prayer_tool(SETTINGS, TODAY, "prayer.qibla", {})


class TestTheManifest:
    def test_it_takes_no_arguments(self) -> None:
        """ "Today" is the question people ask. A model free to pass a date would
        eventually pass the wrong one, with nothing in the answer to show it."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CATALOG.get("prayer.today").validate_args({"date": "2026-09-16"})

    def test_it_runs_on_the_backend(self) -> None:
        assert CATALOG.get("prayer.today").runs_on == "backend"


class TestTheReminder:
    def moment(self, now: datetime, **kwargs: object) -> Moment:
        kwargs.setdefault("present", True)
        return Moment(
            now=now,
            prayers=compute(now.date(), latitude=43.238, longitude=76.889, zone=ALMATY),
            **kwargs,  # type: ignore[arg-type]
        )

    def decide(self, now: datetime, schedule: Schedule | None = None) -> list:  # type: ignore[type-arg]
        return decide_all(self.moment(now), schedule or Schedule(), already_said=set())

    def test_it_arrives_before_the_prayer_not_at_it(self) -> None:
        """A reminder that arrives exactly at Maghrib is a reminder about
        something already happening."""
        planned = self.decide(at(17, 56))  # Maghrib is 18:03

        assert [item.notification.kind for item in planned] == [NotificationKind.PRAYER]
        assert "Магриб" in planned[0].notification.body
        assert "18:03" in planned[0].notification.body

    def test_nothing_is_said_at_an_ordinary_moment(self) -> None:
        assert self.decide(at(13, 0)) == []

    def test_nothing_is_said_after_the_prayer_has_passed(self) -> None:
        assert self.decide(at(18, 30)) == []

    def test_it_is_spoken(self) -> None:
        planned = self.decide(at(15, 12))  # Asr is 15:18

        assert planned[0].notification.speak is True

    def test_sunrise_earns_no_reminder(self) -> None:
        """Sunrise at 05:32 - a boundary, not a prayer."""
        assert self.decide(at(5, 25)) == []

    def test_nothing_is_said_to_an_empty_chair(self) -> None:
        planned = decide_all(
            Moment(
                now=at(17, 56),
                prayers=compute(TODAY.date(), latitude=43.238, longitude=76.889, zone=ALMATY),
                present=False,
            ),
            Schedule(),
            already_said=set(),
        )

        assert planned == []

    def test_setting_the_window_to_zero_switches_the_reminders_off(self) -> None:
        """The times stay answerable by asking; only the interrupting stops."""
        assert self.decide(at(17, 56), Schedule(prayer_reminder_minutes=0)) == []

    def test_a_wider_window_reminds_earlier(self) -> None:
        assert self.decide(at(17, 40)) == []
        assert self.decide(at(17, 40), Schedule(prayer_reminder_minutes=30)) != []

    def test_with_no_location_configured_it_says_nothing(self) -> None:
        """The normal state until the owner gives coordinates. Silence rather
        than prayer times for the wrong city."""
        planned = decide_all(
            Moment(now=at(17, 56), prayers=None, present=True), Schedule(), already_said=set()
        )

        assert planned == []

    def test_it_is_not_repeated_once_said(self) -> None:
        first = self.decide(at(17, 56))
        again = decide_all(self.moment(at(17, 57)), Schedule(), already_said={first[0].key})

        assert again == []

    def test_each_prayer_has_its_own_reminder(self) -> None:
        """Keyed by prayer as well as by date, so being reminded about Asr does
        not count as having been reminded about Maghrib."""
        asr = self.decide(at(15, 12))
        maghrib = self.decide(at(17, 56))

        assert asr[0].key.startswith("prayer:asr:")
        assert maghrib[0].key.startswith("prayer:maghrib:")


class TestASuppliedTimetableWins:
    """The calculation is this code's approximation of somebody's convention.

    A timetable published by the authority the owner actually follows *is* the
    convention. Where both exist there is no contest — and the answer says which
    one it came from, because "computed" and "from your mosque" are different
    claims and the owner should be able to tell them apart.
    """

    @staticmethod
    def timetable(day: str = "2026-09-15", fajr: str = "03:00") -> Any:
        from atlas_backend.prayer.schedule import load_timetable

        times = (fajr, "12:00", "16:00", "18:00", "20:00")
        return load_timetable(
            json.dumps(
                {
                    "locality": "Almaty",
                    "timezone": "Asia/Almaty",
                    "source": "Test fixture, not for worship",
                    "method": "Explicit test times",
                    "revision": "test-v1",
                    "days": [
                        {
                            "day": day,
                            "times": [
                                {"prayer": prayer.value, "at": f"{day}T{hour}:00+05:00"}
                                for prayer, hour in zip(OBLIGATORY, times, strict=True)
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

    def settings_with(self, **kwargs: Any) -> PrayerSettings:
        return PrayerSettings(latitude=43.238, longitude=76.889, zone=ALMATY, **kwargs)

    def test_the_timetable_is_used_for_a_day_it_covers(self) -> None:
        answer = run_prayer_tool(
            self.settings_with(timetable=self.timetable()), TODAY, "prayer.today", {}
        )

        assert answer["fajr"] == "03:00"
        assert answer["source"] == "timetable"

    def test_a_day_it_does_not_cover_falls_back_to_the_calculation(self) -> None:
        """A timetable that ran out in the spring must not take prayer times
        down with it in the autumn.

        The date is 2026 rather than something older on purpose: the validator
        checks the UTC offset against the zone for that day, and Kazakhstan was
        +06:00 until March 2024 — so a fixture dated 2020 with +05:00 is
        genuinely invalid, and it said so."""
        answer = run_prayer_tool(
            self.settings_with(timetable=self.timetable(day="2026-01-01", fajr="01:00")),
            TODAY,
            "prayer.today",
            {},
        )

        assert answer["fajr"] == "03:54"

    def test_without_one_the_answer_says_it_was_calculated(self) -> None:
        answer = run_prayer_tool(self.settings_with(), TODAY, "prayer.today", {})

        assert answer["source"] == "calculated"

    def test_sunrise_is_absent_rather_than_mixed_in(self) -> None:
        """A published timetable lists the five. Computing sunrise and slipping
        it into the same answer would give one reply two provenances."""
        answer = run_prayer_tool(
            self.settings_with(timetable=self.timetable()), TODAY, "prayer.today", {}
        )

        assert "sunrise" not in answer
        assert answer["dhuhr"] == "12:00"

    def test_the_next_prayer_comes_from_the_timetable_too(self) -> None:
        answer = run_prayer_tool(
            self.settings_with(timetable=self.timetable()), at(13, 0), "prayer.today", {}
        )

        assert answer["next"]["at"] == "16:00"
