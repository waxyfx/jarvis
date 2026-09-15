# Deterministic reply presentation

This is the first conservative implementation of `PERSONALITY-ENGINE.md`, in
`atlas_backend.personality`. It is available for integration; the existing API
and voice pipeline do not call it automatically on this branch.

## What it does

- `professional`: exact original reply.
- `jarvis` (default): occasional RU/EN address, with a cooldown.
- `personal`: occasional informal preface for longer single-line answers.
- `custom`: explicit address and cooldown settings.
- `StyleConfig` rejects unknown settings and invalid values. No setting changes
  permissions, risk, SAFE MODE, token budget or confirmation requirements.
- Existing address, multiline/code/JSON replies, empty and long replies pass
  through. Kazakh passes through verbatim until it has reviewed phrasing.
- The entire original reply is retained **verbatim**. There is no truncation,
  paraphrase, number conversion, translated factual content or invented result.
- History contains at most eight preface enum ids. No user text, identities,
  emotions or health inference. A stateless provider can serve multiple sessions;
  each caller supplies its own history.

Sarcasm, humour/profanity dials, free-form preferred names, translation, learned
preferences and model-assisted rewriting remain roadmap work. The first
provider deliberately provides only the style choices it can honour safely.

## Boundary

`snapshot_turn(result)` reads the completed turn and returns a frozen
`ReplySnapshot`. It passes no SQLAlchemy objects, mutable outcome lists,
dispatcher, session, tools or callbacks into the provider.

Every tool-bearing turn and every interrupted turn makes the snapshot protected.
A protected reply passes through exactly in **every** mode. At present the API
adapter adds style only to completed conversational turns without tool proposals.
The provider can also style a trusted immutable factual snapshot supplied by a
future caller with complete outcome information.

This is conservative for a concrete reason: the orchestrator's `executed` list
includes failed attempts, and `ToolCall.status=completed` is only a dispatch
lifecycle state. `ToolResult.status` is audited but is not preserved as a
distinct field on `ToolCall`. The wire schema permits a non-OK result with data
and no failure/refusal field. Checking those nullable fields cannot prove OK.
An integrator may extend this adapter once explicit execution status reaches it;
do not infer success from absence of an error or from the reply's wording.

This layer preserves the reply it is given. It does not certify that Gemini's
original prose is factually correct. If that prose already contradicts an
outcome, verification belongs upstream; presentation cannot repair it by guess.
Untrusted words are never interpreted as settings. Ordinary Python provider
code is still trusted application code, not a sandbox for arbitrary plugins.

## Integration for Claude

The existing `TurnResult` is mutable, despite the roadmap calling it frozen.
Do not change the orchestrator to accommodate presentation. Use the adapter:

```python
from atlas_backend.personality import (
    RuleBasedPersonality,
    StyleConfig,
    StyleHistory,
    snapshot_turn,
)

# After Assistant.handle has completed the policy/execution/audit path.
presentation = RuleBasedPersonality().present(
    snapshot_turn(result),
    config=StyleConfig(),  # replace with settings explicitly chosen by the owner
    history=StyleHistory(),  # replace with this session's recent presentation ids
)
# Use presentation.text for BOTH the stored assistant Message and response.reply.
# Keep every other result/response field unchanged.
# Retain presentation.history for this session only, after successful delivery.
```

The insertion point is in `api/assistant.py` after `assistant.handle()` and
before creating the assistant `Message`. Keep styled prose out of tool
planning/system instructions. If formatting fails, return the original reply;
never rerun the tool. Account for response retries when updating history. A
persisted preference needs an authenticated owner settings endpoint, not an
untrusted utterance containing “switch mode”.

The voice responder already reads the endpoint's reply; it needs no new policy
or confirmation path. Preserve structured `pending_confirmation`, `denied`,
`executed`, risk, reasons and status fields on the API response exactly.

Tests cover real Policy Engine verdicts (SAFE MODE, external-content override
suspension and explicit user deny) through snapshot/presentation, across every
mode. Separate tests cover failed attempts, unchanged facts, immutable snapshots,
bounded session histories and malicious text that resembles instructions.
