# What is on `main`, and what is taken

For the second agent, and for anyone picking this up mid-flight. Written by the
integrator after merging `codex/prayer-personality`.

## Merged, with thanks

**`personality/`** landed as written and is now wired in: it runs at the end of
every assistant turn, after the audit entry, configured by
`ATLAS_PERSONALITY_ENABLED`, `_MODE` and `_ADDRESS`. A provider that raises
costs a decoration, not an answer. Both test settings factories disable it so
the suites keep asserting what the assistant *did* rather than how it was
worded — one e2e test compares an exact reply and would otherwise have failed
for the wrong reason.

**`prayer/schedule.py`** landed as written, except for the enum. See below.

Two of your decisions were changed, and both were yours rather than mine, so
here is the reasoning rather than a fait accompli:

**The five-prayer enum is gone.** `schedule.py` now imports the shared `Prayer`
from `prayer/times.py` — six members, because a computed timetable reports
sunrise — and validates against an explicit `OBLIGATORY` five-tuple. One package
had grown two vocabularies for the same five names, and two would eventually
disagree about something.

**The address moved to the end of the reply.** "Сэр, Свободно 42 ГБ." is wrong
Russian — a capital letter after a comma — and there is no safe way to lowercase
the next word, which may be "VS Code" or "Chrome". Every proactive notification
in this system already ends "..., сэр". Your protection rules, cooldown and
history are untouched; only the shape of the address changed, and your tests
were updated in step.

## Areas now taken

Work landed on `main` since the base you branched from. Rebase before touching
any of it:

| Area | Where | State |
|---|---|---|
| Web search and page reading | `web/` | Wired, catalogued, live-tested |
| Activity summaries | `activity/` | Wired; `activity.today` in the catalogue |
| Proactive notifications | `notify/` | Wired; `server.notify` is a new **signed** message |
| Daily report into Sunny | `reports/` | Wired to the proactive loop; one real report filed |
| Prayer **computation** | `prayer/times.py`, `prayer/tools.py` | Wired; `prayer.today` in the catalogue, reminders in `notify/rules.py` |
| Launcher | `start-jarvis.bat`, `scripts/start_jarvis.ps1` | Database, backend, agent, in order |
| Dispatcher, catalogue, config, orchestrator, `main.py` | | Actively edited; coordinate before touching |

## About `calculator.py`

Your worktree has an uncommitted `prayer/calculator.py` using **adhanpy**. That
is very likely the better engineering choice, and this is not a request to drop
it.

`prayer/times.py` on `main` is about two hundred lines of hand-rolled
trigonometry. Two real bugs were found in it, both by tests: an Asr angle with
the wrong sign, which put Asr two and a half hours after sunset; and an
unreduced solar longitude, which computed every time for a date twenty-five days
away while every *time of day* still looked correct. A maintained library has
had those found years ago.

So if adhanpy holds up, the clean move is **not** a third implementation. Keep
`compute()`'s signature and replace what is behind it:

```python
def compute(
    on: date, *, latitude: float, longitude: float, zone: tzinfo,
    method: Method | str = "mwl", asr: AsrMethod = AsrMethod.STANDARD,
) -> PrayerTimes: ...
```

`prayer.today`, the reminder rule in `notify/rules.py`, and 51 tests already pin
the behaviour — including the twelve-hour equinox, the three-hour equinox Asr,
the Hanafi gap, the absent Fajr at 64°N, and that the returned datetimes fall on
the requested date. A swap that keeps those green is a verifiable improvement
rather than a rewrite, and the diff would be one file.

One thing to preserve if you do: **`None` for a prayer the sun never reaches**.
Far enough north there are summer nights where every published time is a
convention, and absent is the only claim that is true.

## Still open, and not started by either of us

- Reading a timetable file into `schedule.Timetable` — nothing loads one yet,
  and where both exist the timetable should win over the computation.
- Kazakh: `Language.KK` exists and passes through the personality layer
  untouched by design; the voice stack has no Kazakh model.
- Memory and personalisation, presence and posture, web protection.
- iPhone and remote control: blocked on hardware, not on code.

## Needs the owner, not us

- Coordinates for the prayer times, and one day checked against their mosque.
- The spoken acceptance run: twelve scenarios, `live-e2e.bat --remaining`.
