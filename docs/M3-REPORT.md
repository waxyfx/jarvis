# M3 — acceptance report

> Status: **awaiting your approval**. M4 has not been started.
> Date: 2026-08-12. Builds on [M2-REPORT.md](M2-REPORT.md).

M3 connects a language model to the tool layer M2 built. The model understands
the request and chooses a tool; everything that decides whether the tool runs is
the same deterministic code as before, untouched by the model.

## 1. What exists now

| Area | Delivered |
|---|---|
| **`AIProvider`** | Provider-agnostic contract. Gemini is one implementation; a scripted one ships beside it |
| **Gemini** | REST provider, function declarations generated from the tool registry, key in a header and only on the backend |
| **Orchestrator** | Full turn: validation → policy → confirmation → signed command → agent → signed result → reply |
| **Provenance** | Every text segment tagged `user_instruction` / `external_content` / `tool_result`, with a deterministic policy consequence |
| **Guards** | Per-turn call cap, iteration cap, wall-clock timeout, cancellation, daily token budget |
| **Persistence** | `conversations`, `messages`, `api_usage`; tool calls linked to the turn that proposed them |
| **API** | `POST /v1/assistant/message` |
| **Audit** | Turn start and end, every proposal, every rejection, arguments redacted |

## 2. Test results

```
670 passed, 20 skipped in 403.85s
ruff: All checks passed!    ruff format: clean    mypy --strict: 76 files, no issues
```

| Area | Tests | M2 → M3 |
|---|---|---|
| `atlas-shared` | 211 | 211 → 211 |
| `atlas-backend` | 252 | 176 → 252 |
| `atlas-agent-windows` | 160 | 160 → 160 |
| `e2e` | 67 | 40 → 67 |
| **Total** | **690** | 587 → 690 |

The 20 skips are the live-model evaluation (19 cases, no API key present) and
the off-Windows plaintext-key test.

## 3. The pipeline

```
user text → model → proposed tool calls → validation → Policy Engine
          → confirmation if required → signed command → agent → execution
          → signed result → reply
```

The model's only power is proposing. Full description in [ai.md](ai.md).

## 4. Adversarial cases

Every one is a test. All use the scripted provider, because a real model cannot
be asked to reliably invent a nonexistent tool or return malformed JSON on
demand — and each drives the production pipeline.

| Case | Behaviour | Tests |
|---|---|---|
| Model invents a tool | Rejected `unknown_tool` before policy; **no `tool_calls` row at all** | 1 |
| Model asks for a shell | Same: `shell.run`, `powershell.execute`, `system.shell`, `cmd.exec`, `os.system` all do not exist | 5 |
| Wrong arguments | Rejected `invalid_arguments`; extra fields, empty strings, wrong types, missing required | 4 |
| Attempt to call `fs.delete` | Policy holds it for confirmation; nothing runs | 1 |
| Attempt to bypass policy | A path outside the roots is denied; confident phrasing changes nothing | 2 |
| Too many tool calls | Truncated at the per-turn cap; `stopped_because: tool_call_limit` | 3 |
| Gemini unavailable | Honest reply, `provider_unavailable`, no actions | 1 |
| Timeout | Same path, with its own message | 1 |
| Malformed response | Distinct message; no crash | 2 |
| Repeated command | Two turns, two recorded calls, no confusion | 1 |
| User does not confirm | Call stays `pending_confirmation` forever; never executes | 2 |
| MEDIUM/HIGH without confirmation | Held regardless of what the model says about it | 2 |

Two more worth naming:

* **A rejection is fed back**, so a model that made a typo can correct itself
  inside the same turn — tested with `app.lunch` → `app.launch`.
* **No failure mode leaks the API key**, tested across HTTP status, connection
  and malformed-body paths, with the key deliberately embedded in the upstream
  error text.

## 5. Security review

The model is outside the security boundary, and the code enforces that rather
than assuming it.

**A tool the model names must exist in the registry.** Declarations are
generated from the same `ToolManifest` objects the Policy Engine reads, so the
model cannot be shown a tool policy does not know, and cannot call one it was
not shown. Invented names are discarded before a `tool_calls` row is created.

**Arguments are validated twice before execution** — once here against the
Pydantic model, once again on the agent — and the agent still recomputes risk
independently and refuses on disagreement.

**Policy does not read the model.** `decide()` takes the tool, the arguments,
device trust, agent mode, the user's standing permissions and a clock. It never
receives the model's text, confidence or reasoning. A test asserts that a call
wrapped in *"This is definitely safe and the user clearly wants it"* is still
held.

**Prompt injection has a deterministic answer, not just a prompt-level one.**
Untrusted text is wrapped and labelled, but more importantly: once a turn has
ingested external content, standing `always_allow` permissions stop applying. A
successful injection lands in the confirmation queue rather than in a
pre-authorised action.

**The key never leaves the backend.** Header not URL; `SecretStr` so it cannot
be printed; excluded from every exception path; never sent to the agent or the
phone; never in a log line — startup logs the provider *name*, not the key.

**Runaway loops are bounded three ways**, and each bound is tested by driving a
model that never stops proposing tools.

## 6. Live demonstration

`e2e/test_assistant_e2e.py` — real backend, real agent, real database, scripted
model. Eight tests, all passing:

| Scenario | Result |
|---|---|
| «Открой Notepad» | `app.launch` → allow → agent → **Notepad really starts**, pid returned |
| «Покажи использование RAM» | `system.metrics` → real numbers → model answers in Russian from them |
| «Закрой Notepad» | `app.close` → **held for confirmation** → confirmed → agent closes it |
| «Открой Notepad и покажи использование памяти» | Two controlled calls in one turn, both completed |
| SAFE MODE engaged | The same model-driven action does not execute |
| Search outside the roots | Denied, never sent to the agent |
| Search inside the roots | Finds `report.pdf` |
| «Выполни Get-Process в PowerShell» | `powershell.run` rejected; zero `tool_calls` rows |

**What is not yet demonstrated: the same scenarios against real Gemini.** That
needs an API key, which by your instruction lives only in `.env` on the backend
and which I have not seen and have not asked for. Section 9 has the two commands
to run once you add one.

## 7. Russian, English and mixed phrasing

Pipeline-level language handling is tested with the scripted provider: the
requested language reaches the provider, and replies come back in it.

Whether the *model* chooses the right tool for a Russian sentence is a different
question and is measured separately, in `e2e/test_gemini_live.py`: 15 command
cases plus 4 judgement cases, asserting the chosen tool **and** its arguments —
not merely that some text came back.

| Group | Cases | Examples |
|---|---|---|
| Russian | 7 | «Открой VS Code» → `app.launch(name~code)`, «Закрой Notepad» → `app.close`, «Сколько свободно места?» → `system.metrics` |
| English | 4 | "Open Chrome", "Close Notepad", "Show me RAM usage", "What is running right now?" |
| Mixed | 4 | «Запусти Chrome и покажи использование памяти» → two tools; «Закрой Chrome и покажи memory usage»; «Открой Notepad and show me RAM» |
| Judgement | 4 | Ambiguous request must ask, not guess; a shell request must not become an invented tool; only registered tools ever proposed |

These are skipped without a key. They are an evaluation of model quality, not of
safety: a wrong choice there still has to pass policy and the agent.

## 8. Known limitations of M3

| Limitation | Detail |
|---|---|
| **The live model has not been exercised** | Every test uses the scripted provider or is skipped. The Gemini transport is covered offline against a mock, but no real call has been made from this machine |
| Model id may be stale | `gemini-2.5-flash` is the default; verify against the current list. My knowledge of available ids has a cutoff |
| No conversation memory across turns | Each message starts fresh. Multi-turn context is M8 |
| No intent router | Every message costs a model call, including "what is my CPU". A cheap classifier in front is M4 work, where latency matters |
| Language is a request parameter | The client says which language; automatic detection arrives with the voice pipeline in M4 |
| Confirmation is still an API call | Face ID and freshness are M5, unchanged from M2 |
| Budget resets at midnight UTC | Not the user's timezone |
| Vision is not implemented | Deliberately out of scope for M3, per your instruction. `VISION-POLICY.md` still describes the target |

Everything in [M1-REPORT §9](M1-REPORT.md) and [M2-REPORT §10](M2-REPORT.md)
still applies.

## 9. How to run the live demonstration

The key must be a **new** one. If the old key ever appeared in a message, a
source file or a log, treat it as compromised and issue a replacement in Google
AI Studio.

Add it to `.env` **yourself** — it is gitignored, and I do not need to see it:

```bash
ATLAS_GEMINI_API_KEY=your-new-key-here
```

Then evaluate the model's tool choices:

```bash
uv run pytest e2e/test_gemini_live.py -v
```

And drive the four demo scenarios against the real model:

```bash
uv run atlas-backend --port 8000
```

```bash
curl -s -X POST http://127.0.0.1:8000/v1/assistant/message -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"text\":\"Открой Notepad\",\"language\":\"ru\"}"
```

## 10. Proposed M4 scope

Confirm and I will start — this is the milestone that completes the MVP:

* Wake word «Atlas», locally, on the agent
* Voice activity detection and endpointing
* Speech recognition, ru/en locally on your 3060, behind a replaceable provider
* Speaker verification as an identity hint, never as authorisation
* Text-to-speech with the ATLAS voice
* Continuous conversation session after the wake word
* An STT comparison bench measured on **your** recorded commands, choosing
  defaults from data rather than by preference

After M4 the loop closes: you say «Atlas, открой Chrome» and it opens.
