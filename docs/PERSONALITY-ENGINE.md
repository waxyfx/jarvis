# Personality & Adaptive Communication Engine

> Status: **roadmap**. Nothing implemented. Scheduled after the M4 critical
> path, because it is additive and the voice pipeline is not.
> Date: 2026-08-16.

JARVIS should read as a particular assistant rather than a chat model with a
name. This module owns *how* something is said. It owns nothing about *what may
be done*.

## The one rule everything else follows from

**Personality is not authority.**

```
user → Gemini → tool planning → Policy Engine → execution → result
                                                              ↓
                                                    Personality layer → user
```

The layer runs **after** the Policy Engine has decided and the tool has run. It
receives an outcome and produces wording. It cannot reach backwards.

Concretely, it may change phrasing, humour, sarcasm, register, length and
address. It may not change a decision, a risk level, a confirmation
requirement, SAFE MODE, or whether a tool ran.

> «Джарвис, удали всё к чертям.»
> «Эффектно, сэр, но нет. Это требует подтверждения.»

The refusal in that reply is not the personality being witty about danger. The
Policy Engine already refused; the layer is only reporting it in character. A
user's emotional register is never evidence of permission, and the layer has no
channel through which it could be.

This is enforced structurally rather than by discipline. The layer's input is a
`TurnResult` — the same frozen record the API already returns — plus a style
configuration. It has no access to the dispatcher, the catalogue, or the policy
evaluator, and it runs in a place where the decision has already been persisted
to the audit log. There is nothing for it to override.

## Facts outrank character

The layer must never invent technical content to make a line land.

> **No:** «Это точно драйвер NVIDIA.» when nothing established that.
> **Yes:** «Я бы с удовольствием обвинил NVIDIA, сэр, но доказательств пока
> нет. Проверяю журналы.»

Humour must not be used to paper over uncertainty. A reaction may precede an
answer; it may not replace one. Every styled reply carries the substantive
content it was given — if the outcome says "unknown", the reply says unknown,
however charmingly.

The implementation follows from that: the layer is given the *facts* as
structured fields and may only rewrap them. It never receives a free hand to
compose technical claims.

## Modes

| Mode | Character |
|---|---|
| `PROFESSIONAL` | No profanity, minimal humour, short and plain |
| `JARVIS` *(default)* | Calm, intelligent, understated, lightly British, dry humour, mild sarcasm, "сэр" where it lands naturally, profanity essentially absent |
| `PERSONAL` | Looser, matches the user's register, slang, sarcasm, jokes, profanity where the context genuinely carries it, may pick up the user's emotional beat |
| `CUSTOM` | Explicit dials |

`CUSTOM` exposes: sarcasm, humour, profanity, formality, verbosity, preferred
address, language preference. Stored as ordinary settings so a Windows or phone
UI can present them later without the engine changing.

## Variety, not catchphrases

A fixed line after every similar request stops being character and becomes a
tic. «Я сам в ахуе, сэр» is good once and grating on the fourth outing.

The layer picks among registers rather than templates:

* plain answer
* short emotional beat, then the answer
* sarcasm
* dry humour
* straight and serious

with the same situation legitimately drawing different reactions:

> «Это действительно странно, сэр. Сейчас посмотрю.»
> «Великолепно. Ещё одна загадка Windows, сэр.»
> «Похоже, компьютер решил проявить характер. Сейчас разберёмся.»

A short recent-history window suppresses repetition: the same opener, the same
joke shape, and "сэр" in consecutive sentences are all discouraged by
construction. Profanity is never used for its own sake — only where the register
of the moment actually carries it.

## Adaptive, within limits

Length should track the request. «Джарвис, Chrome.» deserves «Открываю, сэр.»,
not a paragraph. «Объясни, почему этот код работает именно так» deserves the
paragraph.

Adaptation is over *observable style* — command length, language, register —
and it produces *presentation preferences*. It does not infer mental or medical
state. `user_was_angry_at_21:43` is not a thing this module writes down; that
would be a different feature, needing its own justification and its own consent.

What may persist is the boring, useful kind:

```
preferred_personality = personal
humor = medium
sarcasm = medium
profanity = allowed
preferred_address = sir
```

Individual utterances — swearing included — are not stored as long-term memory.

## Languages

Russian and English at minimum, Kazakh later. Jokes are not translated
literally; each language gets phrasing that is natural in it.

> RU: «Ну это уже интересно, сэр. Сейчас разберёмся.»
> EN: "Well, that's interesting, sir. Let me take a look."

Code-switching is fine when the user does it, matching the behaviour the M3
acceptance already tests for.

## With the voice

The written style has to suit the voice that speaks it. The voice stays calm,
confident, understated, male, lightly British — sarcasm should read as dry, not
as a performance. Text that only works when acted will sound wrong.

The voice remains original. No cloning of any real person's voice.

## Where it will sit

`packages/atlas-backend/src/atlas_backend/personality/` — after the
orchestrator, before the response is serialised. A `PersonalityProvider`
protocol, so the styling can be rule-based first and model-assisted later
without touching callers.

Two properties are worth testing hard, and both are the sort that pass
vacuously if written carelessly:

* a refusal stays a refusal in every mode, under every provocation — asserted
  on the `TurnResult`, not on the wording;
* the styled reply still contains the facts it was given, so a test must check
  that the substance survived, not merely that a reply came back.

## Order of work

Not now. The voice pipeline is the critical path and this is additive. It lands
once M4's critical path is complete, and it should not require changes to
anything M1–M4 already does.
