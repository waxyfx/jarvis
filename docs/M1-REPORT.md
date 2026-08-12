# M1 — acceptance report

> Status: **awaiting your approval**. M2 has not been started.
> Date: 2026-08-12

M1's definition of done was: *the Windows agent connects to the backend over a
real socket, registers itself, and the audit log records it.* That is met, and
asserted by an end-to-end test that runs the production code paths on both sides
against a real uvicorn server.

## 1. What exists now

| Component | Delivered |
|---|---|
| **Monorepo** | uv workspace, Python 3.12, ruff + mypy strict + pytest, git initialised |
| **`atlas-shared`** | Wire protocol (envelope, registry, canonical JSON), Ed25519 signing, ULIDs, tool manifests with the risk-escalation engine |
| **`atlas-backend`** | FastAPI app, PostgreSQL schema + migration, device pairing, challenge/response auth, hash-chained audit log, WebSocket hub, rate limiting |
| **`atlas-agent-windows`** | DPAPI-protected identity store, enrolment client, outbound WS transport with reconnection policy, `pair` / `status` / `run` CLI |
| **Infrastructure** | docker compose (Postgres + backend + Caddy), Dockerfile, Caddyfile, `.env.example` |
| **Scripts** | `bootstrap_dev.ps1` (full local environment, no admin rights), `measure_vps_latency.ps1` |
| **Docs** | protocol, security model, runbook, vision policy, this report |

**What ATLAS still cannot do**, deliberately: execute anything on Windows, call
Gemini, open a microphone, capture the screen. Those are M2–M4. The tool
manifests and risk rules are declared and tested, but no executor is bound to
them — an execution request would fail with `tool_not_implemented` rather than
silently doing nothing.

## 2. Test results

```
392 passed, 1 skipped in 81.18s
```

| Area | Tests | Covers |
|---|---|---|
| `atlas-shared` | 211 | ULIDs, canonical JSON, Ed25519, envelope signing and tampering, message registry, risk escalation, tool catalogue |
| `atlas-backend` | 133 | Pairing, authentication, revocation, audit chain and database triggers, WebSocket handshake and protocol violations, rate limiting, config validation |
| `atlas-agent-windows` | 41 | Identity storage and DPAPI, file-integrity failures, URL derivation, reconnection policy |
| `e2e` | 8 | Real agent against real uvicorn: pairing, connection, heartbeat, revocation, restart, chain integrity |

The one skip is `test_plaintext_storage_is_refused_by_default_off_windows`,
which is not reachable on Windows because DPAPI is always available there.

Integration tests need PostgreSQL and **skip loudly** without it rather than
being quietly replaced by a weaker check — the behaviours they test (advisory
locks, append-only triggers, `FOR UPDATE`) do not exist in a substitute.

Slowest tests are the deliberate timing ones: heartbeat survival (6.8 s),
server ping (2.8 s), hello timeout (2.5 s).

**Static checks**

```
ruff check .        All checks passed!
ruff format --check All files formatted
mypy (strict)       Success: no issues found in 50 source files
```

Code size: 4 082 lines of source across 50 files, 2 547 lines of tests across 24.

## 3. Security review

Full review in [security.md](security.md). The properties that matter most:

* **A dump of the backend database cannot command your computer.** Devices are
  Ed25519 keypairs; the server holds only public keys.
* **The agent's key never leaves the machine**, encrypted with user-scoped DPAPI.
  Off Windows the store refuses to write an unprotected key unless explicitly
  told to.
* **A pairing code interceptor cannot substitute their own key** — the proof
  signs the code together with the key being enrolled.
* **Bootstrap enrolment closes itself** once any device exists, so a forgotten
  token in `.env` is not a way in later.
* **Revocation is immediate**, for future requests and for live sockets.
* **The audit log is append-only in PostgreSQL itself** (triggers reject UPDATE,
  DELETE and TRUNCATE) and hash-chained, with failures recorded in their own
  transaction so a rejected pairing survives the rollback that caused it.
* **Authentication failures are indistinguishable** — unknown, expired and spent
  codes all return the same 401.

Known gaps are listed in [security.md](security.md#known-gaps). The two worth
repeating: command signing exists and is tested but nothing signs commands yet
(no commands exist until M2), and the Docker deployment is written but has never
been built, because Docker is not installed here.

## 4. Defects found and fixed during M1

Recorded because each one would have been painful later.

1. **Audit entries for failures were being rolled back.** A rejected pairing was
   written inside the request transaction, which then rolled back — discarding
   exactly the record most worth keeping. Fixed with `append_detached`, which
   writes in its own transaction.
2. **A foreign key would have deadlocked against the immutability trigger.**
   `audit_log.device_id` referenced `devices` with `ON DELETE SET NULL`; deleting
   a device would attempt an `UPDATE` on the audit log, which the trigger
   rejects. The column is now a plain UUID: an append-only record must outlive
   the rows it mentions.
3. **A connection-pool leak in the request dependency.** `async for` over a
   nested async generator left the inner context manager to the garbage
   collector. Replaced with a direct `async with`, which also removed a stream of
   SQLAlchemy warnings.
4. **A crash in the protocol error path.** `ValidationError.errors()` was called
   with the wrong keyword, so a malformed frame raised `TypeError` instead of a
   protocol error. Caught by the tests that deliberately send bad frames.
5. **A risk rule crashed on an absent optional argument.** `PATH_OUTSIDE_ROOTS`
   raised when the path was `None`, which would have turned a routine
   `app.launch` into an error.

## 5. Environment findings

* **Python 3.14 is not usable for this project.** The ML stack needed from M4
  (torch, ctranslate2, onnxruntime) has no complete 3.14 support. The project is
  pinned to 3.12, installed alongside your 3.14 — nothing was removed.
* **No administrator rights on this machine.** PostgreSQL therefore runs as a
  portable process on port 55432 rather than as a Windows service. This is a
  development arrangement only; the VPS uses the container.
* **Your RTX 3060 (6 GB) has NVENC**, which makes screen streaming in M6 cheap
  in CPU terms. No AV1 encoder (Ampere), which does not matter — H.264 suffices.
* `ffmpeg` and Docker are still not installed. Neither is needed before M6.

## 6. Things I want to flag

* **The Docker deployment is unverified.** I wrote it; I could not build it. The
  first `docker compose up` on the VPS is part of acceptance, not a rehearsed
  step.
* **`TestClient` cannot verify connection teardown.** It cancels the ASGI task
  when the websocket context exits, so the handler's `finally` never completes.
  This is a property of the test client, not the server — the close path is
  asserted in `e2e/test_end_to_end.py` against a real uvicorn instead. The
  in-process test says so in its docstring rather than quietly asserting less.
* **Nothing is committed to git.** The repository is initialised; I left the
  first commit to you so you can review the tree first.
* **The Gemini API key you sent should be rotated.** It is in the conversation
  history. Nothing needs it until M3, and when it does it will be read from
  `.env` rather than passed through chat.

## 7. How to check this yourself

```bash
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_dev.ps1
```

```bash
uv run pytest
```

```bash
uv run ruff check . ; uv run mypy
```

Then see it work end to end — start the backend:

```bash
uv run atlas-backend --reload
```

Issue a pairing code with the bootstrap token from `.env`:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/pair/start -H "X-Atlas-Bootstrap-Token: PASTE_FROM_ENV" -H "Content-Type: application/json" -d "{\"kind\":\"windows_agent\",\"name\":\"workstation\"}"
```

Pair and run the agent:

```bash
uv run atlas-agent pair --code PASTE-CODE
```

```bash
uv run atlas-agent run
```

The agent should print `agent_connected`. Confirm it registered:

```bash
uv run atlas-agent status
```

## 8. What M2 needs from you

Nothing blocking — M2 (agent tools, tray, SAFE MODE, activity monitoring) can
start immediately. Useful when convenient:

| When | Question |
|---|---|
| Before M2 lands | Which folders should be the allowed file roots? My default: Desktop, Downloads, Documents, and this repo |
| Before M4 | Budget for TTS and Gemini per month — decides Azure vs ElevenLabs |
| Before M4 | Confirm timezone `Asia/Almaty` and your quiet hours |
| Before M4 | Voice transcript retention. My proposal: 30 days, audio never stored |
| Before M5 | Apple Developer Program, iPhone model, iOS version, Xcode version |
| Before M7 | Access to the tracker site repository — stack, database, hosting, existing API |

## 9. Known limitations of M1

The authoritative list. Everything here is a real constraint on what M1 delivers,
not a to-do that might already be half done.

### Unverified

| Limitation | Detail |
|---|---|
| **The Docker deployment has never been built or run** | `infra/backend.Dockerfile`, `docker-compose.yml` and `Caddyfile` are written from the documented behaviour of those tools. Docker is not installed on the development machine, so none of it has been executed. **It is not confirmed until it runs on the VPS.** Expect to iterate on the first build |
| TLS termination, certificate issuance, proxy headers | Configured in Caddy, never exercised. Only plain HTTP on loopback has been tested |
| Behaviour under real network conditions | Reconnection is tested against a local server. Packet loss, captive portals, sleep/resume and NAT rebinding are not |

### By design, for now

| Limitation | Detail |
|---|---|
| **Rate limiting is in-memory, per process** | `SlidingWindowLimiter` holds counters in the backend process. Correct for one backend serving one user; it does **not** survive a restart, and two backend processes would each get their own budget. Must move to the database or a shared store before any horizontal scaling |
| Commands are not yet signed in practice | The envelope signature scheme is implemented and tested, but nothing signs a command because no commands exist before M2 |
| No per-token revocation | Revocation is per device and takes effect immediately. Individual JWTs stay valid until they expire (15 minutes) |
| No key rotation flow | Recovery from a suspected key compromise is re-pairing with a new key |
| Audit verification is manual | `POST /v1/audit/verify` exists; nothing runs it on a schedule or alerts on failure. Automation is M12 |
| Challenge cleanup is manual | `ChallengeService.purge_expired` exists but is not scheduled — expired rows accumulate until M9 brings a scheduler |
| Single-user assumptions | `user_id` exists throughout so multi-user does not require rewriting, but nothing enforces per-user isolation beyond the owner check |

### Environment

| Limitation | Detail |
|---|---|
| Development PostgreSQL is a portable process | Port 55432, no Windows service, no automatic start after reboot. Restart it with `pg_ctl` (see the runbook). Production uses the container |
| Python is pinned to 3.12 | 3.14 is installed on this machine but unusable for the project: the ML stack needed from M4 has no complete support for it |
| The agent is tested on one machine only | Windows 11 Pro 26200, one user account, one CPU architecture |

### Test-harness limitations

| Limitation | Detail |
|---|---|
| `TestClient` cannot verify connection teardown | It cancels the ASGI task when the websocket context exits, so a handler's `finally` never completes. The close path is asserted in `e2e/` against real uvicorn instead. The in-process test documents this rather than quietly asserting less |
| Integration tests need a live PostgreSQL | They skip loudly without `ATLAS_TEST_DATABASE_URL` / `ATLAS_E2E_DATABASE_URL` rather than degrading to a weaker check |

## 10. M1 checkpoint

Performed before the first commit.

| Step | Result |
|---|---|
| `.gitignore` coverage | Extended: env files, private keys in every common form, agent identity files, database dumps, captured tokens, `.pgdata`, `.tools`, `.venv`, logs |
| Ignore rules verified | `git check-ignore` confirms `.env`, `.env.test` and `.pgdata/server.log` are excluded |
| Staged tree reviewed | 100 files; the only match against sensitive-path patterns is `.env.example`, which holds placeholders only |
| Secret scan (`detect-secrets`) | 8 findings, all triaged as false positives: placeholder DSNs in `.env.example` and docstrings, an example ULID in `protocol.md`, the Crockford alphabet constant in `ids.py` and `auth.py`, and test-only constants in the test suites |
| **Live-secret cross-check** | The actual values from `.env` and `.env.test` — JWT secret, bootstrap token, database passwords — were compared against the full content of every staged file. **No live secret appears anywhere in the commit.** Values were compared, never printed |
| Baseline recorded | `.secrets.baseline` stores hashes, not values, so a future scan flags anything new |
| Commit and tag | First commit created and tagged `m1` |

Re-run the scan before any future commit:

```bash
uv tool run detect-secrets scan --baseline .secrets.baseline
```

## 11. Proposed M2 scope

Confirm and I will start:

* WebSocket command dispatch on the agent, with signed `agent.tool.execute`
* Tray icon: status, kill switch, monitoring pause, recent actions
* SAFE MODE enforcement per [VISION-POLICY.md](VISION-POLICY.md) §3
* Path guard with real symlink resolution on the agent
* Executors for the declared tools: `system.metrics`, `app.list`, `app.launch`,
  `app.close`, `fs.search`, `fs.open`
* Activity monitoring with title sanitisation and the denylist
* Autostart at logon, without administrator rights
