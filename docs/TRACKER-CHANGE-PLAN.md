# Tracker integration — change plan

What exists, what would change, and what should not. **No code has been written
and nothing in `C:\Users\serik\sunny` has been modified.** This is the analysis
that was required before any of that, and it is here to be argued with.

---

## 1. The tracker already exists, and it already expects JARVIS

The tracker is **Sunny — Life OS**: Next.js 15 (App Router), TypeScript, Prisma,
at `C:\Users\serik\sunny` and deployed on Vercel. Forty models covering tasks,
goals, projects, habits, health, books, courses, vocabulary, workouts, notes and
notifications.

Two things found in it change the shape of this work substantially.

**Sunny has a machine-access path built for JARVIS by name.** `src/server/auth.ts`
already contains `userFromMachineToken()`:

- enabled only when `JARVIS_API_TOKEN` is set, and only if it is at least 24
  characters — absent, nothing changes;
- constant-time comparison, so the token cannot be guessed from response times;
- bound to a specific account through `JARVIS_USER_EMAIL` rather than falling
  back to whichever user the database returns first;
- the token value is never logged.

That is the seam. There is no reason to design another one, and good reason not
to: a second authentication path is a second thing to get wrong.

**Sunny has a typed action whitelist and a proposal-then-apply flow.**
`src/server/ai/tools.ts` defines `ACTION_SCHEMAS` — twelve actions, each with a
zod schema:

```
create_task   create_goal    create_project  create_note
create_habit  create_vocab_words             complete_task
reschedule_task  delete_task  update_goal_progress
log_water     log_habit
```

The flow matters more than the list. Sunny's own assistant *proposes* actions;
they are stored against the message, and `/api/ai/apply` re-reads them from the
database by message id rather than taking them from the request body. The
comment says why: otherwise that endpoint would execute anything sent to it.
`markApplied` flips a flag atomically before executing, so a double-click cannot
create two sets of tasks.

That is the same instinct as JARVIS's Policy Engine, arrived at independently.

---

## 2. The one decision that matters: which side calls Sunny

| | Backend calls Sunny | Windows Agent calls Sunny |
|---|---|---|
| Where the token lives | Backend, beside the Gemini key | On the laptop |
| Works when the laptop is off | Yes | No |
| Matches the existing rule | Yes | No |
| Tracker reachable from | One place | Every paired device |

**Recommendation: the backend.** Three reasons, in order of weight.

**The standing rule already decides it.** "Gemini API key должен находиться
только на backend/VPS и никогда не передаваться Windows Agent." A Sunny token is
the same kind of thing: a credential for a remote service that grants write
access to the owner's data. Nothing about it belongs on a laptop that also runs
a wake word.

**The tracker is not a Windows capability.** Every existing agent tool does
something to *this machine* — launch a program, read a file, report memory. The
agent's whole justification is that it is the only thing that can touch the
local machine. Sunny is an HTTP service; routing a call to it through a
WebSocket to a laptop and back adds a hop, a failure mode and a device that has
to be awake, and buys nothing.

**It keeps the tracker working when the laptop is not.** Tracker questions —
"what is due today" — are exactly the ones worth answering from a phone later.

---

## 3. What would change in JARVIS

### 3.1 A tracker provider, behind a protocol

New: `packages/atlas-backend/src/atlas_backend/tracker/` with a `TrackerProvider`
protocol and a `SunnyTracker` implementation. Same shape as `AIProvider`: the
orchestrator talks to the protocol, and the fact that the tracker is Sunny stays
in one file.

This is not ceremony. It is what makes the tracker testable without a running
Next.js app, and it is the thing that stops "the tracker" and "Sunny" becoming
the same word in forty places.

### 3.2 Tools in the catalogue, with risk levels

Tracker actions enter `atlas_shared.tools.catalog` like any other tool, because
the Policy Engine is not something to route around:

| Tool | Risk | Why |
|---|---|---|
| `tracker.today` | LOW | Read-only |
| `tracker.search` | LOW | Read-only |
| `tracker.create_task` | LOW | Additive, and trivially undone |
| `tracker.complete_task` | **MEDIUM** | Changes state the owner tracks by |
| `tracker.reschedule_task` | **MEDIUM** | Same |
| `tracker.log_water` / `log_habit` | LOW | Additive |
| `tracker.update_goal_progress` | **MEDIUM** | Overwrites a number |
| `tracker.delete_task` | **not implemented** | See §5 |

The risk column is the substance of this plan. A misheard word becoming a
completed task is a small harm; a misheard word becoming a deleted one is not,
and the existing rule about `fs.delete` applies here for the same reason.

### 3.3 Nothing new in the agent

No agent changes. Tracker tools are dispatched by the backend, not sent down the
WebSocket, and the agent's tool catalogue does not grow.

---

## 4. What would change in Sunny

**Ideally nothing.** The endpoints JARVIS needs already exist — `/api/tasks`,
`/api/dashboard`, `/api/goals`, `/api/habits`, `/api/health/*`, `/api/notes` —
and the machine token already reaches them.

Two things might be wanted, and both are additive:

1. **A read endpoint shaped for a spoken answer.** "What is due today" currently
   means several calls and joining them client-side. `/api/dashboard` may
   already be close enough; this needs checking against the real response before
   anything is proposed.
2. **Rate limiting on the machine-token path.** A shared secret with no limit is
   a shared secret that can be brute-forced offline in a way the constant-time
   comparison does not prevent.

Neither is required for a first version, and neither should be written before
the read endpoint is tried as it stands.

---

## 5. What this plan deliberately does not do

**`tracker.delete_task` is not implemented.** Sunny has `delete_task` and JARVIS
will not expose it, for the same reason `fs.delete` is still `not_implemented`.
Speech recognition mishears — measured, in this project, turning «Открой» into
«Закрой». A misheard delete is not recoverable by apologising.

**JARVIS does not reuse Sunny's `/api/ai/apply`.** That endpoint exists to apply
what *Sunny's* model proposed, keyed by a message in Sunny's own conversation.
JARVIS has its own model, its own conversation and its own confirmation step;
borrowing Sunny's would mean two systems each believing they own the gate.
JARVIS calls the ordinary REST endpoints and applies its own Policy Engine.

**No write path is enabled before the read path is used in anger.** Asking the
tracker what is due today is most of the value and none of the risk.

**Sunny's own assistant is left alone.** It has a Gemini key and an Anthropic key
of its own, and its conversations are its own. Two assistants sharing one
conversation store is a design nobody asked for.

---

## 6. Open questions for the owner

1. **Where is Sunny reachable from the backend?** The Vercel deployment, or the
   local instance? A VPS backend cannot reach `localhost:3000` on the laptop.
2. **Is `JARVIS_API_TOKEN` already set anywhere,** or does it need issuing? It
   must be generated fresh, stored only on the backend, and never committed —
   the existing rules about the Gemini key apply unchanged.
3. **Which of the twelve actions are actually wanted by voice?** The list above
   is what Sunny supports, not what is worth saying out loud. "Add a task" and
   "what is due today" may be the whole of it.

---

## 7. Suggested order

1. Read-only first: `tracker.today` and `tracker.search`, against the real
   deployment, with the token on the backend.
2. Measure what a spoken tracker answer actually sounds like. A list of eleven
   tasks read aloud is unusable, and that changes the shape of the read API more
   than any amount of planning will.
3. `tracker.create_task` — additive, LOW, the most obviously useful write.
4. The MEDIUM writes, once confirmation by voice has been settled, which it has
   not been: saying "yes" to a microphone is still not the confirmation step.

Nothing here is started. This document is the deliverable.
