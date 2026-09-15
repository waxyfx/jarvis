# Parallel development handoff — 2026-09-15

Base: `9fd13f6`, branch `codex/prayer-personality`, worktree
`C:/Users/serik/atlas-codex-prayer-personality`.

## Coordination

The supplied `C:/Users/serik/jarvis` directory is an older TypeScript prototype,
without Git metadata. The current JARVIS repository is `C:/Users/serik/atlas`.
Its README, milestone history and `atlas-*` protocol match the request.

At branch creation, the main worktree contained changes to backend `config.py`,
`policy/service.py`, shared `tools/catalog.py`, and new `web/` plus three web
test files. Web reader files were modified today. This is evidence that web
development is active, not a claim to know Claude's full task queue. These paths
are reserved for the integrator. No main worktree files are edited here.

Independent scope: offline prayer timetable handling, then deterministic reply
presentation. Both are new backend modules and tests. Runtime wiring is left
explicitly to the integrator; no competing edits to the catalogue, dispatcher,
configuration, orchestrator, API, voice engine or database migrations.

## Architecture reviewed

- M1: Ed25519 device pairing, challenge authentication, audit chain, outbound
  WebSocket transport; backend stores public device keys.
- M2: shared typed manifests, deterministic policy, signed command/result path,
  independent agent checks, path restrictions and locally released SAFE MODE.
- M3: Gemini proposes calls; schemas and policy decide. Tool results retain
  untrusted provenance. Limits bound the model loop; secrets stay on backend.
- M4: local wake word, VAD, recognition, speaker filtering and speech. The voice
  path reaches the same assistant endpoint. Voice recognition is not authority.
  Owner's physical spoken acceptance remains outstanding, per M4 report §12.
- Latest committed work: Sunny tracker backend tools, digest, writes and owner
  access. Tracker credentials must not be loaded for offline tests.
- Roadmap: personality is additive after M4. Scheduler uses deterministic gates
  before any model call; reports consume computed facts; iPhone is later work.

## Findings for the architect — no parallel rewrite

1. `PERSONALITY-ENGINE.md` describes `TurnResult` as frozen. The implementation
   is a mutable dataclass with lists of mutable SQLAlchemy `ToolCall` objects.
   A style provider should receive an immutable value snapshot, never those
   objects or the dispatcher. Do not freeze the orchestration type in parallel.
2. `TurnResult.executed` also contains unreachable/failed outcomes: `_perform`
   puts every non-pending, non-denied call there. A presentation layer must
   inspect status/result rather than infer success from membership.
3. Documentation has historical state: `security.md` opens by saying no model
   or microphone exists, while later sections and M3/M4 describe both. Use
   implementation and latest milestone checkpoints when deciding capability.
4. `available_tools()` at this base enables the full catalogue when a tracker
   exists. Web integration must filter each backend capability independently.
   This is in Claude's active dispatcher area; documented, not edited here.
5. Windows Python has no IANA timezone database in the existing virtualenv.
   Prayer scheduling needs `tzdata`; never substitute a fixed UTC offset for
   an IANA location, and never modify Claude's installed environment to test it.
6. `ToolCall.status` preserves dispatch lifecycle, not `ToolResult.status`.
   A non-OK wire result may carry data without optional failure/refusal fields;
   persisting it as `completed` loses the distinction from OK outside the audit.
   The personality adapter therefore protects **all** tool-bearing turns until
   explicit execution status is available. This branch does not change the
   active dispatcher, protocol, ORM or schema to resolve that larger issue.

## Validation baseline

Shared and backend suites at the base: **505 passed, 153 skipped**. Database
environment variables were cleared in the test subprocess; no `.env` copied.
Skips are integration cases requiring PostgreSQL. No live Gemini or tracker
calls were made. Test imports were checked to resolve inside this worktree.

## USER_ACTION_REQUIRED

- Real prayer schedule activation: owner-selected locality and a timetable
  source/method accepted by the owner. No city or religious convention inferred
  from the machine timezone, and no test times represented as real prayer times.
- M4 physical voice acceptance stays with the owner; no synthetic test can close it.
- iPhone device signing, Apple account setup and physical testing remain outside
  this independent scope.

Implementation and final verification notes follow as modules are completed.
