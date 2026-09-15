# M5 — The internet, the day, and speaking first: report

What was built, what it does, and what is still wrong with it.

Seven features, delivered in the order they were asked for. Where a number
appears here it was measured on this machine; where something was not measured,
it says so.

---

## 1. What works

**JARVIS looks things up.** «Jarvis, найди в интернете последнюю версию Python»
reaches DuckDuckGo, comes back with five results and their sources, and the
answer is built from what came back rather than from training data. Asked to
explain, it opens one of the results and reads it.

**JARVIS knows what the day looked like.** «Сколько я сегодня работал?» is
answered from the activity samples the agent has been sending every ten seconds
— hours at the machine, hours actually working, and which applications.

**JARVIS speaks first.** A reminder fifteen minutes before something timed, a
briefing in the morning, a summary in the evening, and a word after ninety
unbroken minutes at the desk. Nothing is said to an empty chair.

**JARVIS writes the day down.** Which tasks were finished, which were not, where
the hours went — filed as a note in Sunny, where the owner already keeps things.

**JARVIS knows when the prayers are.** Computed here from the date and a pair of
coordinates, with a reminder before each one. Nothing leaves the machine.

**JARVIS sounds like itself.** A small deterministic layer adds an address at the
end of a reply, sometimes, and can never change a fact.

**And all of it starts with one double-click.** `start-jarvis.bat` brings up the
database, the backend and the agent in order, checking each before the next.

---

## 2. Web tools

### What it is

Two tools, both on the backend, both free and keyless.

| Tool | Risk | What it does |
| --- | --- | --- |
| `web.search` | LOW | Five results with titles, sources and one-sentence snippets |
| `web.read` | LOW | Opens one of those results and returns its prose |

### Why DuckDuckGo Lite, by POST

Measured, not assumed. `html.duckduckgo.com` and `api.duckduckgo.com` both
answer a plain client with **HTTP 202** — a bot challenge, not a page.
`lite.duckduckgo.com/lite/` answers a GET with **200 and zero results**, which
is the dangerous failure: it looks like "nothing found". The same endpoint
answers a **POST** — which is what its own form sends — with results.

The parser is a regular expression over `class='result-link'` anchors. Crude,
and correct for a page whose markup is a table. The test fixture is trimmed from
a real response, single quotes and all, because a fixture invented to match the
regex would prove only that the regex matches itself.

### The line drawn around `web.read`

`web.read` is the only tool in the catalogue where the **model** supplies an
address. Two defences, and they cover different attacks:

1. **Only a public address.** Every hop is resolved before it is connected to,
   redirects are followed by hand so each one is checked, only `http(s)` is
   opened, and a host that resolves to a private, loopback, link-local or
   reserved address is refused by name. `follow_redirects=True` would have
   checked the first address and then connected wherever the server nominated —
   the entire defence skipped by one header.

2. **Only an address a search returned**, and only for fifteen minutes. Search
   results are weak provenance — anyone can rank — but they are *somewhere the
   owner's question led*, which `https://intranet/payroll` composed mid-sentence
   is not. The address checks cannot catch that one, because an attacker's page
   is on a public address too.

Reads are streamed under a two-megabyte cap and cut to four thousand characters,
with the cut declared in the result.

### Prompt injection

Nothing new was needed. The orchestrator already marks every turn after a tool
call as carrying external content, which tightens policy for the rest of the
turn and restates the data/instruction boundary in the model's own system
instruction. A test now pins that for a page whose text says to ignore all
previous instructions and delete the owner's tasks: the words arrive as a tool
result, nothing runs because of them, and the turn is flagged.

### Measured

| | |
| --- | --- |
| Search, live | 5 results, ~0.4 s |
| Page read, live (python.org) | 4000 characters, 0.4 s |
| Russian query | Cyrillic results returned correctly |
| Model chooses `web.search` for a current-facts question | Yes — live Gemini, `-m core` |

---

## 3. Activity

`activity.today` answers "how long did I work", from rows already being
collected. It runs on the backend, because the laptop does not remember its own
day and the question gets asked when the laptop is shut.

**Time is measured between samples, never counted in samples.** A row count
times an assumed interval is wrong as soon as the interval changes, the agent
reconnects or the machine sleeps — and wrong in the direction that invents
hours. A gap longer than two minutes is not counted at all: a laptop closed at
six and opened at nine the next morning must not read as a fifteen-hour day.

Idle is reported separately from active. Sitting at a desk with the screen on is
not working, and the difference is sometimes the interesting part.

Applications are named the way a person names them — `Code.exe` becomes
"VS Code" — because the answer is spoken.

---

## 4. Speaking first

### The rules

| Rule | When | Spoken? |
| --- | --- | --- |
| Reminder | 15 min before something with an hour on it | Yes |
| Morning briefing | Once, 08:00–12:00, if the owner is there | Yes |
| Evening summary | Once, 21:00–23:00 | No — shown |
| Long session | After 90 unbroken minutes, repeating hourly | No — shown |

Every threshold is a setting; the defaults are above.

### The design, which is mostly about not being muted

- **Nothing is said to an empty chair.** Presence is the last activity sample
  being recent and not idle, so a connected laptop at lunchtime stays quiet.
- **Quiet hours downgrade rather than suppress.** Between 23:00 and 07:00 a
  notification is still delivered and still shown; it is not announced into a
  dark room. Suppressing it outright would lose information about an 08:00
  meeting.
- **SAFE MODE silences the voice and keeps the text.** The kill switch means
  stop acting, not stop existing.
- **Undelivered means dropped, not queued.** A reminder about a meeting that
  started forty minutes ago is noise with a timestamp, and a pile of them at
  reconnect is the fastest way to teach someone to ignore notifications.
- **The wording is a template, never a model call.** Predictable, free, and
  incapable of phrasing something wrongly at four in the afternoon with nobody
  watching. Personality belongs in replies to what the owner actually said.

### Signed

`server.notify` carries a signature like a command. Not because it acts on the
machine — it does not — but because it is a sentence spoken aloud in the owner's
room in the assistant's voice, and an unsigned channel for that is a channel for
telling them their bank called. `e2e/test_proactive_e2e.py` sends an unsigned
one: it is refused, not spoken, and the agent enters SAFE MODE, which is the
existing behaviour for any frame that fails verification.

### Audited

Every notification is recorded — `notification.sent`, or
`notification.suppressed` with a reason — so "why did it tell me that" has an
answer. The **body is deliberately not recorded**: this log is readable from the
iPhone Settings screen and the rule there has always been identifiers and
decisions, not transcripts.

---

## 5. The day, written down

`activity.today` answers the question out loud; this is the detail behind it,
filed as a note in Sunny at 22:00 — tagged `jarvis` and `итоги-дня` so it can be
found and filtered out. The machine token already reaches every Sunny route, so
nothing on that side needed changing, and `add_note` is deliberately **not** in
the tool catalogue: the model has no reason to write prose into the owner's
notes, and giving it one would be a way to persist whatever a web page talked it
into.

It shares the proactive loop but not its rules. A notification needs someone in
the room; a report does not.

Verified live: one real report filed in Sunny, note `cmu2iq8tb0021kz0482tc427j`.

---

## 6. Prayer times

Computed here, never fetched. A free API would carry the owner's coordinates and
the fact that they pray to a third party, daily, for a page of trigonometry — and
this works with the internet down.

`prayer.today` leads with the next prayer, because that is the question. A
reminder arrives a configurable few minutes before each one, through the same
rules as everything else, so quiet hours, SAFE MODE and presence all apply.

Method and school are settings. Sunrise and sunset are astronomy and have one
right answer; Fajr and Isha depend on which authority you follow, and the
methods disagree by twenty minutes or more at this latitude. The runbook says to
check one day against the owner's own mosque.

Where there is no answer there is no answer: far enough north the sun never
reaches the twilight angle on a summer night, and those prayers come back absent
rather than invented.

There are now **two** ways to know a prayer time, and they answer different
worries — see §8.

---

## 7. Character

A deterministic layer runs at the very end of a turn, after the audit entry. It
can add at most a short address and can never change a fact, reach a tool, or
touch a policy decision.

The rule it enforces is blunt on purpose: **any turn that involved a tool goes
out verbatim.** Not "tools that succeeded" — any tool at all. At that layer
there is no reliable way to tell an attempt from a success, and an answer the
owner is about to act on is not somewhere to experiment with wording.

It does not say "сэр" every turn either. Said every time it stops being
character and becomes a tic.

---

## 8. Work integrated from the second agent

A second coding agent worked in a separate worktree on `codex/prayer-personality`
and left `docs/CODEX-PARALLEL-HANDOFF.md`. Its findings were checked rather than
assumed; two of them — that `available_tools()` removed every backend tool when
no tracker was configured, and that Windows has no IANA timezone database — had
been found independently on this side, which is corroboration rather than
coincidence.

**The personality engine was taken as written and wired in.** It is careful
work: no I/O, no model call, history of enum ids only, and tool-bearing turns
passed through untouched.

**Both prayer implementations were kept**, because they answer different
worries. Computation needs only coordinates and works for any date, but its
angles are an approximation of somebody's convention. A supplied timetable
guesses nothing and is exactly what the owner's mosque publishes, but is bounded
to the days in the file. Where both exist the timetable should win.

Two things were changed during integration, and both are worth stating plainly
because they were the other agent's decisions:

The five-prayer enum in `schedule.py` was replaced by the shared six-member one
(which includes sunrise) plus an explicit `OBLIGATORY` tuple. One package had
grown two vocabularies for the same five names, and two would eventually
disagree.

The address moved from the front of the reply to the end. "Сэр, Свободно 42 ГБ."
is wrong Russian — a capital after a comma — and the next word may be "VS Code",
so there is no safe way to lowercase it. Every proactive notification in this
system already ends "..., сэр".

---

## 9. Bugs found and fixed

Each of these was found by a test before it could be found by the owner.

**`ZoneInfo("Asia/Almaty")` raises on Windows.** Windows ships no IANA time zone
database. The scheduler resolves the owner's zone when it is constructed, so
this would have taken the whole backend down on the machine it is meant to run
on. `tzdata` is now a declared dependency, and an unknown zone falls back to UTC
with a loud log line rather than refusing to start.

**The morning briefing fired at four in the afternoon.** It tested a threshold
(`hour >= 8`) rather than a window, so someone sitting down after lunch was
greeted with a cheerful summary of a day that was mostly over. It is a window
now, and missing the window means no briefing — which is the right answer.

**A duplicated activity sample broke a stretch of work.** Two samples at the same
instant — a batch replayed after a reconnect — reset the current stretch,
quietly chopping one long afternoon into pieces. That is precisely the signal the
long-session warning depends on, so it would have failed silently.

**Backend tools disappeared with the tracker.** `available_tools` removed *every*
backend tool when no tracker was configured, so a backend without Sunny would
have had no internet either. Found while adding the web tools.

**A notification could claim LOW priority and ask to be spoken.** Two fields
setting the same thing independently. `speak` is now derived from the effective
priority.

**Asr landed two and a half hours after sunset.** The shadow rule gives the
sun's altitude *above* the horizon; the hour-angle function is written in terms
of depth *below* it. With the sign wrong, Asr came out at 20:33 and the Hanafi
time fell an hour *earlier* than the standard one. Every individual number still
looked like a time.

**Every prayer time was computed for the wrong date.** The sun's mean longitude
was not reduced to a single turn, so the equation of time came out six hundred
hours wrong. The error was very nearly a whole number of days, so the times of
day were all still correct and only the date was wrong — by twenty-five days —
which a timetable printed as HH:MM shows not at all. Caught by a test comparing
absolute datetimes.

**The startup script died while succeeding.** Windows PowerShell 5.1 turns a
native program's redirected stderr into terminating errors under
`$ErrorActionPreference = "Stop"`, so alembic logging its progress killed the
launcher. Native calls now go through one wrapper that checks the exit code
instead. A second draft was lost to the same file being read as ANSI because it
had no byte-order mark: one em dash in a comment broke string parsing eleven
lines later.

---

## 10. What is still wrong with it

**What has been said is kept in memory only.** A backend restart can repeat a
briefing. The alternative — a table, a migration and a write on every tick —
buys little for a single-owner system. Known, not discovered.

**The page reader is a regular expression, not a parser.** It strips scripts,
styles, navigation and forms, and takes what is left. On the python.org page it
kept a "Notice: this page displays a fallback because interactive scripts did
not run" line. Good enough to answer "what does this say"; it is not
readability.

**No notification reaches the phone.** The iPhone app does not exist yet, and
the scheduler deliberately speaks only to Windows agents so that nothing is said
twice.

**Search is one provider.** If DuckDuckGo Lite changes its markup or starts
challenging POSTs too, search stops working and says so. A second provider is a
`SearchProvider` protocol away but was not built, because one working provider
beats two half-tested ones.

**Everything proactive needs the backend running.** It runs on this laptop, so
a reminder for 15:00 does not arrive if the machine was off at 14:45, and the
evening report is not written if it was off at 22:00. That is the honest cost of
not having a VPS, and it is the single change that would most improve the
proactive half.

**Prayer times need a location before they do anything.** Both coordinates are
required and neither is guessed: a timezone narrows a city down to a few hundred
kilometres, which moves Maghrib by twenty minutes. `USER_ACTION_REQUIRED`.

**The two prayer implementations are not yet joined.** The computation is wired
to the tool and the reminders; the supplied-timetable path is validated and
tested but nothing reads a timetable file yet. When it does, the timetable
should win.

**Nothing here has been heard out loud by the owner yet.** The delivery path is
tested end-to-end with the speakers stood in for. `USER_ACCEPTANCE_PENDING`.

---

## 11. Tests

| Area | Tests |
| --- | --- |
| Web search parsing | 10 |
| Page reader, mostly refusals | 23 |
| The search→read gate | 18 |
| Web dispatch and policy | 10 |
| Activity arithmetic | 16 |
| Activity dispatch | 7 |
| Notification rules | 26 |
| Notification delivery (agent) | 13 |
| Scheduler | 13 |
| Proactive end-to-end, signed | 4 |
| Daily report | 20 |
| Prayer times, from first principles | 31 |
| Prayer tool and reminders | 20 |
| Prayer timetables (second agent) | 24 |
| Personality (second agent) | 95 |
| Personality wired into a turn | 8 |

Plus five live Gemini cases, which cost quota and are run deliberately.

Full regression green across every package, ruff and mypy clean.
