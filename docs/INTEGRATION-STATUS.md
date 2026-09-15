# Coordination

Two agents. **Claude** moves forward from the current state; **Astra** works
backwards from the end of the roadmap. This file is where they meet.

| | |
|---|---|
| Astra's branch | `codex/prayer-personality`, worktree `C:/Users/serik/atlas-codex-prayer-personality` |
| Last reviewed Astra commit | `32bae94` — merged into main |
| Claude's NEXT | Kazakh, or web protection — reminders, memory, overview and the iOS source are done |
| Astra's NEXT | unknown; her worktree has an uncommitted `prayer/calculator.py` that is now superseded |

**Superseded:** Astra's `calculator.py` argued for adhanpy over hand-rolled
trigonometry. The argument was accepted and adhanpy now sits behind the existing
`prayer.times.compute()`, so the tool, the reminder rule and fifty-one tests
carried over unchanged. Her standalone adapter is not needed; her high-latitude
handling was taken.

---


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
| Prayer **computation** | `prayer/times.py`, `prayer/tools.py` | adhanpy behind the same `compute()`; wired |
| Ad-hoc reminders | `reminders/` | Wired; table `reminders`, migration 0004 |
| Memory | `memory/` | Wired; table `memories`, migration 0005, injected into the prompt |
| Phone overview | `api/overview.py` | `GET /v1/overview` |
| iOS app | `apps/ios/` | Source only, never compiled |
| Deployment | `deploy/`, `infra/` | Scripts written; image never built |
| Launcher | `start-jarvis.bat`, `scripts/start_jarvis.ps1` | Database, backend, agent, in order |
| Dispatcher, catalogue, config, orchestrator, `main.py` | | Actively edited; coordinate before touching |

## Done since: the prayer engine

adhanpy shipped, behind the existing `compute()`. Five cities and four
solstice/equinox dates showed the two implementations agreeing to within three
minutes everywhere, so the swap cost nothing and dropped two hundred lines that
had already produced two bugs. Recorded in
`docs/measurements/prayer-engine-comparison.json`.

## Still open, and not started by either of us

- Reading a timetable file into `schedule.Timetable` — nothing loads one yet,
  and where both exist the timetable should win over the computation.
- Kazakh: `Language.KK` exists and passes through the personality layer
  untouched by design; the voice stack has no Kazakh model.
- Memory and personalisation, presence and posture, web protection.
- Remote control: blocked on nothing but a security review, which has to come
  first — a phone that can move the mouse is a different threat model.
- Push to the phone: `server.notify` already exists and is signed; APNs needs a
  paid Apple account.

## Needs the owner, not us

- Coordinates for the prayer times, and one day checked against their mosque.
- The spoken acceptance run: twelve scenarios, `live-e2e.bat --remaining`.
