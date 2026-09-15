"""Prayer times, checked against things that are true regardless of method.

The awkwardness of testing this is that there is no single right answer to
check against. Fajr and Isha depend on which authority the timetable follows,
and the spread between them is twenty minutes or more. Copying one printed
timetable into the test would pin the code to that timetable rather than to
anything true.

So the assertions here are of two kinds. The first is astronomy, which does have
one right answer and is checkable from first principles — at the equator on an
equinox the sun rises six hours before local solar noon, and the shadow of an
upright stick equals its own length exactly three hours after noon. The second is
structural: the order of the day, the relationships between the schools, and
what happens where the sun never sets.

Whether the *angles* match the owner's own mosque is not something a test can
settle. That is `USER_ACCEPTANCE_PENDING`, and the module says so.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from atlas_backend.prayer.times import METHODS, AsrMethod, Prayer, compute

UTC = ZoneInfo("UTC")
ALMATY = ZoneInfo("Asia/Almaty")

#: 43.238 N, 76.889 E. The owner's city, and the default in the settings.
ALMATY_AT = {"latitude": 43.238, "longitude": 76.889, "zone": ALMATY}

EQUINOX = date(2026, 3, 20)
MIDSUMMER = date(2026, 6, 21)
MIDWINTER = date(2026, 12, 21)


def minutes_between(first: datetime, second: datetime) -> float:
    return abs((second - first).total_seconds()) / 60


class TestAstronomy:
    """The half that has a right answer."""

    def test_at_the_equator_on_an_equinox_the_day_is_twelve_hours(self) -> None:
        times = compute(EQUINOX, latitude=0.0, longitude=0.0, zone=UTC)

        assert times.sunrise is not None
        assert times.maghrib is not None
        assert 11.9 * 60 <= minutes_between(times.sunrise, times.maghrib) <= 12.2 * 60

    def test_noon_sits_between_sunrise_and_sunset(self) -> None:
        times = compute(EQUINOX, latitude=0.0, longitude=0.0, zone=UTC)

        assert times.sunrise is not None and times.maghrib is not None
        midpoint = times.sunrise + (times.maghrib - times.sunrise) / 2
        assert minutes_between(midpoint, times.dhuhr) <= 2

    def test_the_equation_of_time_is_applied(self) -> None:
        """Solar noon on the Greenwich meridian is not 12:00. In late March the
        sun is about seven minutes slow, so noon falls after twelve — and a
        version that ignored the correction would put it at 12:01."""
        times = compute(EQUINOX, latitude=0.0, longitude=0.0, zone=UTC)

        assert (
            timedelta(hours=12, minutes=5)
            < (times.dhuhr - datetime.combine(EQUINOX, datetime.min.time(), tzinfo=UTC))
            < timedelta(hours=12, minutes=10)
        )

    def test_the_asr_shadow_rule_holds_where_it_can_be_checked_by_hand(self) -> None:
        """At the equator on an equinox the sun is overhead at noon, so a
        shadow equals its object's length exactly when the sun is 45 degrees up
        — three hours later. No method or convention involved."""
        times = compute(EQUINOX, latitude=0.0, longitude=0.0, zone=UTC)

        assert times.asr is not None
        assert minutes_between(times.dhuhr, times.asr) == pytest.approx(180, abs=3)

    def test_longitude_moves_noon(self) -> None:
        """Fifteen degrees east is one hour earlier, on the same clock."""
        west = compute(EQUINOX, latitude=0.0, longitude=0.0, zone=UTC)
        east = compute(EQUINOX, latitude=0.0, longitude=15.0, zone=UTC)

        assert minutes_between(east.dhuhr, west.dhuhr) == pytest.approx(60, abs=2)

    def test_summer_days_are_longer_than_winter_days_in_the_north(self) -> None:
        summer = compute(MIDSUMMER, **ALMATY_AT)
        winter = compute(MIDWINTER, **ALMATY_AT)

        assert summer.sunrise is not None and summer.maghrib is not None
        assert winter.sunrise is not None and winter.maghrib is not None
        assert (
            minutes_between(summer.sunrise, summer.maghrib)
            > minutes_between(winter.sunrise, winter.maghrib) + 200
        )


class TestTheOrderOfTheDay:
    @pytest.mark.parametrize("on", [EQUINOX, MIDSUMMER, MIDWINTER, date(2026, 9, 15)])
    def test_the_prayers_come_in_order(self, on: date) -> None:
        """The failure this catches is not hypothetical: a sign error in the Asr
        calculation put it at 20:33, two and a half hours after sunset, and
        every individual number still looked like a time."""
        times = compute(on, **ALMATY_AT)

        moments = [moment for _, moment in times.items()]
        assert moments == sorted(moments), times.as_result()

    @pytest.mark.parametrize("on", [EQUINOX, MIDSUMMER, MIDWINTER])
    def test_asr_falls_between_noon_and_sunset(self, on: date) -> None:
        times = compute(on, **ALMATY_AT)

        assert times.asr is not None and times.maghrib is not None
        assert times.dhuhr < times.asr < times.maghrib

    def test_maghrib_is_sunset(self) -> None:
        """Not an approximation of it: the same calculation, mirrored about
        noon. A drift between the two would mean one of them is wrong."""
        times = compute(date(2026, 9, 15), **ALMATY_AT)

        assert times.sunrise is not None and times.maghrib is not None
        before = minutes_between(times.sunrise, times.dhuhr)
        after = minutes_between(times.dhuhr, times.maghrib)
        assert before == pytest.approx(after, abs=2)


class TestTheSchools:
    def test_hanafi_asr_is_about_an_hour_later(self) -> None:
        """Twice the shadow length. If this came out *earlier*, the shadow
        factor is being applied backwards — which is exactly what a sign error
        in the angle looks like."""
        standard = compute(date(2026, 9, 15), **ALMATY_AT, asr=AsrMethod.STANDARD)
        hanafi = compute(date(2026, 9, 15), **ALMATY_AT, asr=AsrMethod.HANAFI)

        assert standard.asr is not None and hanafi.asr is not None
        assert 30 <= minutes_between(standard.asr, hanafi.asr) <= 90
        assert hanafi.asr > standard.asr

    def test_a_wider_fajr_angle_means_an_earlier_fajr(self) -> None:
        mwl = compute(date(2026, 9, 15), **ALMATY_AT, method="mwl")  # 18 degrees
        isna = compute(date(2026, 9, 15), **ALMATY_AT, method="isna")  # 15 degrees

        assert mwl.fajr is not None and isna.fajr is not None
        assert mwl.fajr < isna.fajr

    def test_the_makkah_method_puts_isha_a_fixed_time_after_maghrib(self) -> None:
        """Umm al-Qura uses an interval rather than an angle, which is a
        different code path and would otherwise go unexercised."""
        times = compute(date(2026, 9, 15), **ALMATY_AT, method="makkah")

        assert times.isha is not None and times.maghrib is not None
        assert minutes_between(times.maghrib, times.isha) == pytest.approx(90, abs=1)

    @pytest.mark.parametrize("name", sorted(METHODS))
    def test_every_named_method_produces_a_usable_day(self, name: str) -> None:
        times = compute(date(2026, 9, 15), **ALMATY_AT, method=name)

        assert times.fajr is not None
        assert times.isha is not None
        assert [moment for _, moment in times.items()] == sorted(
            moment for _, moment in times.items()
        )


class TestWhereThereIsNoAnswer:
    def test_far_north_in_summer_returns_nothing_rather_than_inventing_one(self) -> None:
        """At 64 degrees in June the sun never gets 18 degrees below the
        horizon. Every published time for that night is a convention, and
        `None` is the only claim that is actually true."""
        times = compute(MIDSUMMER, latitude=64.0, longitude=11.0, zone=ZoneInfo("Europe/Oslo"))

        assert times.fajr is None
        assert times.isha is None
        assert times.asr is not None, "Asr still happens; only twilight is undefined"
        assert times.sunrise is not None

    def test_what_is_missing_does_not_appear_in_the_result(self) -> None:
        times = compute(MIDSUMMER, latitude=64.0, longitude=11.0, zone=ZoneInfo("Europe/Oslo"))

        assert "fajr" not in times.as_result()
        assert "dhuhr" in times.as_result()

    def test_an_impossible_place_is_refused_by_name(self) -> None:
        with pytest.raises(ValueError, match="latitude"):
            compute(EQUINOX, latitude=95.0, longitude=0.0, zone=UTC)
        with pytest.raises(ValueError, match="longitude"):
            compute(EQUINOX, latitude=0.0, longitude=200.0, zone=UTC)


class TestWhatIsHandedOn:
    def test_times_are_in_the_owners_own_clock(self) -> None:
        """Kazakhstan moved to UTC+5 in March 2024. A timetable printed before
        then is an hour later than this, and that difference looks exactly like
        a bug until you know."""
        times = compute(date(2026, 9, 15), **ALMATY_AT)

        assert times.dhuhr.utcoffset() == timedelta(hours=5)

    def test_the_next_prayer_is_the_next_one(self) -> None:
        times = compute(date(2026, 9, 15), **ALMATY_AT)
        after_dhuhr = times.dhuhr + timedelta(minutes=1)

        upcoming = times.next_after(after_dhuhr)

        assert upcoming is not None
        assert upcoming[0] is Prayer.ASR

    def test_after_the_last_prayer_there_is_no_next_one_today(self) -> None:
        times = compute(date(2026, 9, 15), **ALMATY_AT)
        assert times.isha is not None

        assert times.next_after(times.isha + timedelta(minutes=1)) is None

    def test_sunrise_is_listed_but_is_not_a_prayer(self) -> None:
        """It ends Fajr rather than beginning anything, so it belongs in the
        timetable and not in the reminders."""
        times = compute(date(2026, 9, 15), **ALMATY_AT)

        assert Prayer.SUNRISE in [prayer for prayer, _ in times.items()]
        assert Prayer.SUNRISE not in [prayer for prayer, _ in times.prayers()]

    def test_seconds_are_dropped_rather_than_shown(self) -> None:
        """Nobody reads prayer times to the second, and 05:59:47 displayed as
        05:59 while a reminder treats it as 06:00 is a discrepancy with no
        upside."""
        times = compute(date(2026, 9, 15), **ALMATY_AT)

        assert all(moment.second == 0 for _, moment in times.items())
