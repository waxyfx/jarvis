# M5 — The internet, the day, and speaking first: report

What was built, what it does, and what is still wrong with it.

Three features, delivered in the order they were asked for. Where a number
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

## 5. Bugs found and fixed

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

---

## 6. What is still wrong with it

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

**Nothing here has been heard out loud by the owner yet.** The delivery path is
tested end-to-end with the speakers stood in for. `USER_ACCEPTANCE_PENDING`.

---

## 7. Tests

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

Plus five live Gemini cases, which cost quota and are run deliberately.

Full regression green across every package, ruff and mypy clean.
