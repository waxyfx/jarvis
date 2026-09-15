# Parallel development handoff — 2026-09-15

Initial base: `9fd13f6`; rebased cleanly onto Claude's completed Web Tools at
`a3d8a80`. Branch `codex/prayer-personality`, worktree
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

## Delivered

| Component | Commit | Integration notes |
|---|---|---|
| Architecture/ownership review | `6efdeff` | This document |
| Offline prayer timetable | `caefb33` | [PRAYER-SCHEDULE.md](PRAYER-SCHEDULE.md) |
| Immutable personality presentation | `0d032c5` | [PERSONALITY-IMPLEMENTATION.md](PERSONALITY-IMPLEMENTATION.md) |

Both modules are implemented, tested and importable. Neither is automatically
activated in the running assistant. Catalogue/API wiring belongs to the
integrator and is described in the component documents. There are no database
migrations, new external services or new credentials. The only added runtime
dependency is `tzdata`; existing locked package versions were retained.

While this branch was in progress, Claude committed Web Tools (`2476dce`) and
their live model acceptance harness (`a3d8a80`), then started backend `activity/`,
activity tests, catalogue and dispatcher edits. The rebase incorporated only
his completed commits, not his uncommitted activity work. No overlapping
component paths were edited here. Do not copy an entire catalogue or dispatcher
from this branch over Claude's newer files.

## Final verification after rebase

- Backend and shared suites: **708 passed, 163 skipped**, including Web Tools.
- New coverage: **41 prayer tests + 95 personality tests**.
- `ruff check .`: passed.
- `ruff format --check .`: 215 files clean, including Markdown Python examples.
- `mypy`: 110 source files, no issues.
- `uv lock --check`: passed; only `tzdata` added to the lockfile.
- `detect-secrets scan`: no findings in files changed by this branch.
- `git diff --check`: passed.

Skipped tests require PostgreSQL; this run intentionally did not use Claude's
databases, environment files or live model/HTTP credentials. This is not a
claim of database or real-device acceptance. Both new modules and their tests
run without a database, model, tracker, microphone or device.

### Reproducing the isolated run on this machine

Run in this worktree. The shared Python executable supplies existing libraries;
explicit source paths ensure it imports **this checkout**, not Claude's. The
small timezone package was installed under this worktree's ignored `.tools/`
directory using `uv pip install --target`; the shared virtualenv was unchanged.

```powershell
$env:PYTHONPATH = "$PWD/.tools/test-deps;$PWD/packages/atlas-backend/src;$PWD/packages/atlas-shared/src;$PWD/packages/atlas-agent-windows/src;$PWD/packages/atlas-voice/src"
$env:ATLAS_TEST_DATABASE_URL = ''
$env:ATLAS_E2E_DATABASE_URL = ''
& C:/Users/serik/atlas/.venv/Scripts/python.exe -m pytest packages/atlas-shared/tests packages/atlas-backend/tests -m 'not live'
& C:/Users/serik/atlas/.venv/Scripts/ruff.exe check .
& C:/Users/serik/atlas/.venv/Scripts/ruff.exe format --check .
& C:/Users/serik/atlas/.venv/Scripts/python.exe -m mypy
```

For a fresh environment, use the branch's lockfile and the project's normal
isolated environment setup. Do not point tests at production or Claude's active
test databases: those suites truncate their configured test tables.

## Integration order

Review `git diff a3d8a80...codex/prayer-personality` and the three feature/review
commits above, followed by the final handoff update. Apply them to the integrator's
chosen branch after checking current ownership again. The modules are independent;
personality does not depend on prayer or `tzdata`. If integrating prayer, take
its dependency and lockfile changes together. Keep all `atlas-*` identifiers.

No merge, push, deployment or activation was performed. The current worktree and
branch are retained for review.
