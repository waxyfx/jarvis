# Tracker integration — change plan

What exists, what changes, and what deliberately does not. The analysis was done
first, as required; `C:\Users\serik\sunny` has not been modified and its working
tree is clean.

---

## 1. The tracker already exists, and it already expects JARVIS

**Sunny — Life OS**: Next.js 15 (App Router), TypeScript, Prisma, forty models
covering tasks, goals, projects, habits, health, books, courses, vocabulary,
workouts and notes. Source at `C:\Users\serik\sunny`, deployed on Vercel
(project `sunny`).

Two findings shape everything below.

**Sunny has a machine-access path written for JARVIS by name.**
`src/server/auth.ts` contains `userFromMachineToken()`:

- enabled only when `JARVIS_API_TOKEN` is set and at least 24 characters —
  absent, nothing changes;
- constant-time comparison, so the token cannot be recovered from response times;
- bound to one account through `JARVIS_USER_EMAIL` rather than falling back to
  whichever user the database returns first;
- never logged.

That is the seam. Building a second one would mean a second authentication path
to get wrong.

**Sunny separates proposing from applying.** `/api/ai/apply` re-reads actions
from the database by message id rather than trusting the request body, and
`markApplied` flips a flag atomically so a repeated request cannot create two
sets of tasks. Its own comment says why. That is the same instinct as JARVIS's
Policy Engine, arrived at independently, and it is the principle this
integration follows.

---

## 2. Decisions

### 2.1 The backend calls Sunny, not the agent

| | Backend | Windows Agent |
|---|---|---|
| Where the token lives | Backend secrets, beside the Gemini key | On the laptop |
| Works with the laptop off | **Yes** | No |
| Matches the standing rule | Yes | No |

The standing rule decides it: a credential for a remote service with write
access to the owner's data does not go on a laptop. Beyond that, the tracker is
not a Windows capability — every existing agent tool acts on *this machine*, and
routing HTTP through a WebSocket to a laptop adds a hop, a failure mode and a
device that must be awake. And the requirement is explicit: JARVIS must reach
the tracker when the Windows machine is off.

### 2.2 `SUNNY_BASE_URL` is configuration

Sunny is on Vercel, so production points at the deployment. The URL is a setting
rather than a constant so that a local instance can be used in development
without the production backend ever being able to reach `localhost`.

```
ATLAS_SUNNY_BASE_URL   https://<the Vercel deployment>
ATLAS_SUNNY_TOKEN      the machine token (secret)
ATLAS_SUNNY_TIMEOUT_S  bounded, like every other outbound call
```

Absent `ATLAS_SUNNY_TOKEN`, the tracker is simply not available and the tools do
not appear in the catalogue. Off by default, like Sunny's own side.

### 2.3 The token

One already exists in Sunny's local `.env` and satisfies the length rule. What
remains is operational rather than code:

- it must be present in the **Vercel** environment, or the deployment will not
  accept it;
- it must be present in the **JARVIS backend** environment;
- it goes nowhere else. Not the Windows Agent, not the iPhone, not git, not
  logs, not this document, not a test fixture.

Issuing or rotating one is a `secrets.token_urlsafe(32)` and two environment
updates; `docs/runbook.md` gets the procedure, without the value. If the token
must be rotated, Sunny rejects the old one the moment its environment changes —
there is no revocation list to maintain, which is a point in favour of the
design Sunny already chose.

---

## 3. The first set of actions

Ten, as agreed. Mapped onto Sunny's **existing** contract rather than duplicating
it — where Sunny already does the thing, JARVIS calls what is there.

| JARVIS tool | Risk | Sunny endpoint | Note |
|---|---|---|---|
| `tracker.today` | LOW | `GET /api/tasks?scope=today` | `scope` already exists |
| `tracker.upcoming` | LOW | `GET /api/tasks?scope=upcoming` | |
| `tracker.schedule` | LOW | `GET /api/tasks?scope=today` | Those with a time, ordered |
| `tracker.goals` | LOW | `GET /api/goals` | |
| `tracker.habits` | LOW | `GET /api/habits` | Returns today's state and streaks |
| `tracker.add_task` | LOW | `POST /api/tasks` | Additive |
| `tracker.add_goal` | LOW | `POST /api/goals` | Additive |
| `tracker.complete_task` | **MEDIUM** | `POST /api/tasks/{id}/toggle` | Changes what the owner tracks by |
| `tracker.reschedule_task` | **MEDIUM** | `PATCH /api/tasks/{id}` | `taskUpdateSchema` accepts it |
| `tracker.set_priority` | **MEDIUM** | `PATCH /api/tasks/{id}` | Same schema |

**Nothing new is needed in Sunny for any of these.** `taskUpdateSchema` is
`taskCreateSchema.partial()`, so priority and deadline are already writable, and
`taskQuerySchema` already understands `today | week | overdue | upcoming |
unscheduled`. The earlier draft of this plan assumed new actions would be
required; reading the validators showed otherwise.

**Deletion is absent.** Sunny has `delete_task`; JARVIS will not expose it. This
project has measured recognition turning «Открой» into «Закрой» — a misheard
delete is not recoverable by apologising. Same reasoning as `fs.delete`, which is
still `not_implemented`.

### 3.1 Naming a task out loud

`complete_task`, `reschedule_task` and `set_priority` need a task id, and a
person says a title. Sunny solves this internally with `pickByTitle`; JARVIS
resolves the same way — read the candidates, match, and **name the match in the
confirmation**.

This is exactly where a misheard word does damage, which is why all three are
MEDIUM: the Policy Engine holds them and the owner hears *which* task before
anything happens. "Mark the gym session complete?" is a different question from
"Mark something complete?".

---

## 4. propose → validate → apply

Sunny's principle, implemented on JARVIS's side of the wire.

1. **Propose.** Gemini emits a `tracker.*` tool call with structured arguments.
   It never produces a URL, a method, a header or a body.
2. **Validate.** The tool manifest's schema checks the arguments, exactly as for
   every other tool, and the Policy Engine assigns the risk above.
3. **Apply.** `SunnyTracker` maps the validated call to one specific request.
   The mapping lives in code. There is no path from model output to an arbitrary
   HTTP call, because no such function exists to reach.

**JARVIS does not reuse `/api/ai/apply`, and that is a deliberate departure
worth stating.** That endpoint applies what *Sunny's* model proposed, keyed by a
message in Sunny's own conversation store; JARVIS has its own model, its own
conversation and its own confirmation step. Borrowing it would mean two systems
each believing they own the gate, and would require JARVIS to write rows into
Sunny's AI tables to say anything at all. The principle is kept; the endpoint is
not the principle.

---

## 5. Reading a tracker out loud

The requirement: *"Сегодня у вас 11 задач, сэр. Три высокого приоритета.
Ближайшая — тренировка в 15:00. Хотите услышать остальные?"*

**The compaction is done in code, not by asking the model nicely.** A prompt
saying "be brief" is a preference; a summariser that returns four facts is a
guarantee. Given eleven tasks, the tracker provider returns:

```
count: 11
by_priority: {urgent: 0, high: 3, medium: 6, low: 2}
next: {title: "Тренировка", at: "15:00"}
overdue: 2
```

The model is given *that*, not the eleven tasks. It cannot read out a list it was
never shown, which is the only reliable way to stop it.

**A correction, from reading the orchestrator rather than assuming.** An earlier
draft of this plan said the full list could travel in the tool result for the
interface to display. It cannot. `_describe_outcome` feeds the *entire* result
back to the model as `OK: <tool> returned <result>`, so anything in there is
something the model has read and may recite. There is one field and it has one
audience.

So the digest *is* the result, and "хотите услышать остальные?" is answered the
way a conversation answers it — by asking again. `tracker.today(offset=3)`
returns the next few. The model never holds eleven titles at any point in the
turn, which is a stronger guarantee than trimming what it says after the fact,
and it costs nothing that a voice interface was going to use anyway.

The full list already has a home: Sunny's own web interface, which is where a
person looks when they want to read rather than listen. Building a second one
here is not in this stage.

Thresholds worth arguing about, and therefore configuration: how many items are
named individually before switching to a summary (three seems right), and how
many a single follow-up returns.

---

## 6. What changes where

**In JARVIS** — all of it additive:

- `packages/atlas-backend/src/atlas_backend/tracker/` — a `TrackerProvider`
  protocol and a `SunnyTracker` implementation, so the orchestrator talks to a
  protocol and "Sunny" stays in one file.
- Ten entries in `atlas_shared.tools.catalog` with the risk levels above.
- A voice summariser, with its own tests, because it is where the requirement in
  §5 actually lives.
- Settings and a runbook entry.

**In Sunny** — ideally nothing, and nothing at all in the first stage. Every
endpoint needed already exists and the machine token already reaches them. Two
things may be wanted later, both additive and neither required:

1. A read endpoint shaped for a spoken answer, *if* the summariser turns out to
   need several round trips. It should not be added before that is measured.
2. Rate limiting on the machine-token path. A shared secret with no limit can be
   attacked offline in a way constant-time comparison does not prevent.

**Not touched:** Sunny's own assistant, its conversations, its Gemini and
Anthropic keys. Two assistants sharing one conversation store is a design nobody
asked for.

---

## 7. Order of work

1. Read-only, against the Vercel deployment: `tracker.today` first.
2. Measure what a spoken answer actually sounds like, and fix the summariser
   against that rather than against a guess.
3. The three additive writes: `add_task`, `add_goal`.
4. The MEDIUM writes, with the confirmation naming the task.

---

## 8. Still needed from the owner

1. **The Vercel URL.** The project is `sunny`; the deployment hostname has not
   been read from anywhere and should not be guessed.
2. **`JARVIS_API_TOKEN` in the Vercel environment.** It exists locally. Whether
   the deployment has it is not visible from here, and the integration cannot
   work until it does.
