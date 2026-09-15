"""Asking "во сколько намаз?", and being reminded before one.

The arithmetic is tested next door. This is about what the model is handed and
what gets said out loud: that the answer leads with the next prayer rather than
reciting six times, and that a reminder arrives before a prayer rather than at
it.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from atlas_backend.notify.rules import Moment, Schedule, decide_all
from atlas_backend.prayer.times import AsrMethod, compute
from atlas_backend.prayer.tools import PrayerSettings, PrayerToolError, run_prayer_tool
from atlas_shared.enums import NotificationKind
from atlas_shared.tools.catalog import CATALOG

ALMATY = ZoneInfo("Asia/Almaty")
SETTINGS = PrayerSettings(latitude=43.238, longitude=76.889, zone=ALMATY)

#: 2026-09-15 in Almaty. Dhuhr 11:48, Asr 15:19, Maghrib 18:04, Isha 19:35.
TODAY = datetime(2026, 9, 15, 12, 0, tzinfo=ALMATY)


def at(hour: int, minute: int = 0) -> datetime:
    return TODAY.replace(hour=hour, minute=minute)


class TestTheAnswer:
    def test_it_gives_the_whole_day(self) -> None:
        result = run_prayer_tool(SETTINGS, TODAY, "prayer.today", {})

        assert set(result) >= {"fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"}
        assert result["dhuhr"] == "11:48"

    def test_it_leads_with_the_next_one(self) -> None:
        """What a person actually wants when they ask. Without it the model
        reads six times aloud and the listener does the arithmetic."""
        result = run_prayer_tool(SETTINGS, at(12, 0), "prayer.today", {})

        assert result["next"]["prayer"] == "asr"
        assert result["next"]["name"] == "Аср"
        assert result["next"]["at"] == "15:19"
        assert result["next"]["in_minutes"] == pytest.approx(199, abs=2)

    def test_after_the_last_prayer_there_is_no_next_one(self) -> None:
        result = run_prayer_tool(SETTINGS, at(23, 0), "prayer.today", {})

        assert "next" not in result

    def test_sunrise_is_listed_but_never_offered_as_next(self) -> None:
        """It ends Fajr rather than beginning anything."""
        result = run_prayer_tool(SETTINGS, at(5, 0), "prayer.today", {})

        assert result["sunrise"] == "05:31"
        assert result["next"]["prayer"] == "dhuhr"

    def test_the_school_changes_the_answer(self) -> None:
        """An hour's difference, and not a detail to pick on someone's behalf."""
        hanafi = PrayerSettings(
            latitude=43.238, longitude=76.889, zone=ALMATY, asr=AsrMethod.HANAFI
        )

        standard_asr = run_prayer_tool(SETTINGS, TODAY, "prayer.today", {})["asr"]
        hanafi_asr = run_prayer_tool(hanafi, TODAY, "prayer.today", {})["asr"]

        assert standard_asr == "15:19"
        assert hanafi_asr == "16:12"

    def test_a_night_with_no_twilight_is_named_rather_than_hidden(self) -> None:
        """A missing Fajr in a Norwegian June is a fact about the sun. Leaving
        it silently out of the result would let the model imply the day simply
        has no dawn prayer."""
        north = PrayerSettings(latitude=64.0, longitude=11.0, zone=ZoneInfo("Europe/Oslo"))

        result = run_prayer_tool(
            north, datetime(2026, 6, 21, 12, tzinfo=ZoneInfo("Europe/Oslo")), "prayer.today", {}
        )

        assert result["not_defined_today"] == ["fajr", "isha"]

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
        planned = self.decide(at(17, 56))  # Maghrib is 18:04

        assert [item.notification.kind for item in planned] == [NotificationKind.PRAYER]
        assert "Магриб" in planned[0].notification.body
        assert "18:04" in planned[0].notification.body

    def test_nothing_is_said_at_an_ordinary_moment(self) -> None:
        assert self.decide(at(13, 0)) == []

    def test_nothing_is_said_after_the_prayer_has_passed(self) -> None:
        assert self.decide(at(18, 30)) == []

    def test_it_is_spoken(self) -> None:
        planned = self.decide(at(15, 12))  # Asr is 15:19

        assert planned[0].notification.speak is True

    def test_sunrise_earns_no_reminder(self) -> None:
        """Sunrise at 05:31 - a boundary, not a prayer."""
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
