"""The arithmetic behind "сколько я сегодня работал?".

Every number here is one the assistant will say out loud, so every way of being
wrong is a way of lying to the owner about their own day. The failures worth
guarding against are all in one direction — inventing time — and they come from
the same mistake: treating a row as a fixed slice of the clock instead of
measuring between rows.

A laptop closed at six and opened at nine the next morning produces two
consecutive samples fifteen hours apart. Multiply by ten seconds and it is
twenty seconds; count the gap naively and it is a fifteen-hour working day.
Neither is true, and the second is the one that gets repeated back to the owner
as fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from atlas_backend.activity.summary import Sample, friendly_name, summarise

START = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def run(
    minutes: float,
    *,
    app: str = "Code.exe",
    idle: bool = False,
    at: datetime = START,
    every_s: int = 10,
) -> list[Sample]:
    """Samples covering ``minutes``, the way the agent would report them."""
    count = int(minutes * 60 / every_s) + 1
    return [
        Sample(ts=at + timedelta(seconds=index * every_s), process_name=app, is_idle=idle)
        for index in range(count)
    ]


def after(samples: list[Sample], seconds: float = 10) -> datetime:
    return samples[-1].ts + timedelta(seconds=seconds)


class TestCountingTime:
    def test_no_samples_is_no_time_rather_than_an_error(self) -> None:
        digest = summarise([], now=START)

        assert digest.active_s == 0
        assert digest.as_result()["active_minutes"] == 0

    def test_an_hour_of_work_reads_as_an_hour(self) -> None:
        samples = run(60)

        digest = summarise(samples, now=after(samples))

        assert digest.active_minutes == 60

    def test_idle_time_is_not_work(self) -> None:
        """Sitting at a desk with the screen on is not the same as working, and
        "how long did I work" means the second one."""
        samples = run(30) + run(30, idle=True, at=START + timedelta(minutes=30))

        digest = summarise(samples, now=after(samples))

        assert digest.active_minutes == 30
        assert digest.as_result()["idle_minutes"] == 30

    def test_a_closed_laptop_is_not_a_working_night(self) -> None:
        """The failure this whole module is shaped around. Two samples fifteen
        hours apart mean nothing was observed, not that something happened."""
        evening = run(10, at=START)
        morning = run(10, at=START + timedelta(hours=15))

        digest = summarise(evening + morning, now=after(morning))

        assert digest.active_minutes == 20, "the gap must not be counted"

    def test_time_is_measured_not_assumed(self) -> None:
        """A row count times an assumed interval breaks the moment the interval
        changes. Samples every 30 seconds cover the same half hour as samples
        every 10 seconds."""
        sparse = run(30, every_s=30)
        dense = run(30, every_s=10)

        assert summarise(sparse, now=after(sparse, 30)).active_minutes == 30
        assert summarise(dense, now=after(dense)).active_minutes == 30

    def test_samples_arriving_out_of_order_are_still_counted_correctly(self) -> None:
        """They arrive in batches over a websocket, and a reconnect can deliver
        a late batch after a newer one."""
        samples = run(20)

        assert summarise(list(reversed(samples)), now=after(samples)).active_minutes == 20

    def test_a_duplicated_sample_is_nothing_rather_than_a_break(self) -> None:
        """A replayed batch delivers the same instant twice. No time passed, so
        no time is counted — and, the part that was wrong first time round,
        nothing about the stretch has changed either. Treating a duplicate as a
        break chopped long afternoons into pieces and suppressed exactly the
        warning that depends on noticing one."""
        samples = run(60)
        doubled = sorted(samples + samples[30:40], key=lambda sample: sample.ts)

        digest = summarise(doubled, now=after(samples))

        assert digest.active_minutes == 60
        assert digest.longest_stretch_s / 60 >= 59


class TestStretchesOfWork:
    def test_a_short_pause_does_not_end_a_stretch(self) -> None:
        """Ninety seconds away is reading something, not a break. Splitting on
        it would report four sessions for one afternoon."""
        first = run(40)
        pause = run(1.5, idle=True, at=START + timedelta(minutes=40))
        second = run(40, at=START + timedelta(minutes=41, seconds=30))

        digest = summarise(first + pause + second, now=after(second))

        assert digest.longest_stretch_s / 60 > 75

    def test_a_real_break_ends_the_stretch(self) -> None:
        first = run(40)
        lunch = run(30, idle=True, at=START + timedelta(minutes=40))
        second = run(20, at=START + timedelta(minutes=70))

        digest = summarise(first + lunch + second, now=after(second))

        assert 39 <= digest.longest_stretch_s / 60 <= 41, "the two stretches must not merge"

    def test_the_stretch_in_progress_is_reported_separately(self) -> None:
        """What a "you have been at this for two hours" warning is about: not
        the longest stretch of the day, the one happening now."""
        morning = run(120)
        lunch = run(30, idle=True, at=START + timedelta(minutes=120))
        afternoon = run(45, at=START + timedelta(minutes=150))

        digest = summarise(morning + lunch + afternoon, now=after(afternoon))

        assert 44 <= digest.current_stretch_s / 60 <= 46
        assert digest.longest_stretch_s / 60 >= 119

    def test_a_silent_agent_ends_the_stretch_rather_than_extending_it(self) -> None:
        """If the samples stopped an hour ago, nothing is known about the hour.
        Reporting a stretch still in progress would be a guess."""
        samples = run(60)

        digest = summarise(samples, now=after(samples, seconds=3600))

        assert digest.current_stretch_s == 0
        assert digest.active_minutes == 60, "what was observed still counts"


class TestWhatWasBeingDone:
    def test_applications_are_ranked_by_time(self) -> None:
        editor = run(50)
        browser = run(20, app="chrome.exe", at=START + timedelta(minutes=50))

        digest = summarise(editor + browser, now=after(browser))

        assert [name for name, _ in digest.by_app] == ["VS Code", "Chrome"]

    def test_applications_are_named_the_way_a_person_names_them(self) -> None:
        """`Code.exe` read aloud as "code dot e x e" is how an assistant starts
        sounding like a log file."""
        assert friendly_name("Code.exe") == "VS Code"
        assert friendly_name("chrome.exe") == "Chrome"
        assert friendly_name("WINWORD.EXE") == "WINWORD"

    def test_a_barely_used_application_is_not_worth_saying(self) -> None:
        """Alt-tabbing through something for twenty seconds is not part of an
        answer to what the day was spent on."""
        editor = run(50)
        glance = run(0.3, app="explorer.exe", at=START + timedelta(minutes=50))

        result = summarise(editor + glance, now=after(glance)).as_result()

        assert [app["name"] for app in result["apps"]] == ["VS Code"]

    def test_only_a_few_applications_are_named(self) -> None:
        samples: list[Sample] = []
        for index in range(6):
            samples += run(10, app=f"app{index}.exe", at=START + timedelta(minutes=index * 10))

        result = summarise(samples, now=after(samples)).as_result()

        assert len(result["apps"]) == 3


class TestWhatTheModelIsGiven:
    def test_it_is_minutes_not_seconds(self) -> None:
        """Seconds invite "one hundred and forty-seven minutes" where a person
        would say "about two and a half hours"."""
        samples = run(147)

        result = summarise(samples, now=after(samples)).as_result()

        assert result["active_minutes"] == 147
        assert not any(key.endswith("_s") for key in result)
