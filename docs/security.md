# Security model — as implemented in M1 and M2

This is a review of what exists today, not of the design target. Controls that
land in later phases are listed as gaps, not as features.

**M1** delivered device identity, enrolment, authentication, the audit trail and
the realtime transport. **M2** added the part that can actually touch the
machine: signed commands, a deterministic Policy Engine, a path guard, SAFE
MODE, six typed tools, and metadata-only activity monitoring.

Still absent, and therefore still outside this review: **no AI model, no
microphone, no screen capture, no remote input.** Those arrive in M3, M4 and M6
with their own controls.

## Trust boundaries

```
[ Windows agent ]  --outbound WSS-->  [ backend on a VPS ]  <--WSS--  [ iPhone ]
   private key                            public keys                 private key
   in DPAPI                               only                        in Secure Enclave (M5)
```

The agent never listens on a port. Nothing reaches the Windows machine from the
internet; it dials out and keeps the connection open.

## Identity

A device is an **Ed25519 keypair**. The private half is generated on the device
and never transmitted. The backend stores only the public half.

The consequence worth stating plainly: **a full dump of the backend database
does not let an attacker command your computer.** They would learn which devices
exist, but could not forge a message from one.

* **Agent key at rest** — encrypted with Windows DPAPI, user-scoped. Copying the
  file to another machine or another Windows account yields nothing.
* **No plaintext fallback on Windows.** Off Windows, where DPAPI does not exist,
  the store refuses to write the key unless the operator explicitly opts in. A
  development convenience must not quietly become a production weakness.
* **File integrity** — the stored public key is checked against the private key
  on every load. A mismatch (edited file, wrong decryption) fails closed.
* **Atomic writes** — write to a temporary file, then replace. A crash mid-write
  cannot leave a half-written key.

## Enrolment

A pairing code authorises exactly one enrolment, and only the enrolment the
initiator described.

| Property | How |
|---|---|
| Codes are short-lived | TTL, default 5 minutes |
| Codes are single-use | Marked consumed inside the same transaction, under `FOR UPDATE` |
| Codes are not recoverable from the database | Only `sha256(code)` is stored |
| A leaked code cannot enrol a *different* kind of device | Kind and name are fixed by the initiator at `pair/start` |
| A code interceptor cannot substitute their own key | The proof signs the code **together with** the public key being enrolled |
| Brute force is impractical | 32⁸ ≈ 1.1 × 10¹² codes, plus a per-address rate limit |
| One key cannot back two devices | Unique constraint on the public key |
| Enrolment closes itself | The bootstrap token stops working the moment any device exists |

That last point matters in practice: a forgotten `ATLAS_BOOTSTRAP_TOKEN` in
`.env` stops being a way in after the first pairing.

Codes are read aloud and typed, so the alphabet excludes `I`, `L`, `O` and `U` —
no character pair a human can confuse. Input is normalised, so `4f2k-9x1m` and
`4F2K 9X1M` are the same code.

## Authentication

Challenge/response, then a short-lived bearer token.

1. The server issues a random 32-byte nonce bound to one device.
2. The device signs `"atlas.auth.challenge.v1" ␟ device_id ␟ nonce`.
3. The server verifies, marks the challenge spent, and issues a 15-minute JWT.

The nonce is **server-generated**, so a device never chooses what it signs, and
**single-use**, so a captured signature is worthless once spent. It is marked
spent only after the signature verifies, so a failed attempt cannot burn a
legitimate device's challenge.

**There are deliberately no refresh tokens.** The device's Ed25519 key is already
a long-lived credential in hardware-backed storage; a refresh token would add a
second, weaker secret to steal without adding any capability. Re-authenticating
costs one round trip every 15 minutes.

**Revocation bites immediately.** Every authenticated request re-reads the device
and refuses if it is revoked, rather than trusting the token until it expires. A
live WebSocket is closed with `4403` at the moment of revocation.

## The audit trail

Append-only and hash-chained:

```
hash(n) = SHA-256( hash(n-1) || canonical_json(entry n) )
```

`chain_index` is inside the hash, so deleting a whole row — which would otherwise
leave a shorter but self-consistent chain — is also detectable.

Three things make this worth trusting:

* **The database refuses to rewrite it.** Triggers reject `UPDATE`, `DELETE` and
  `TRUNCATE`. Row-level triggers do not see `TRUNCATE`, so it has its own guard.
* **Appends cannot fork the chain.** A PostgreSQL advisory lock serialises the
  read-then-write of the chain head. Verified under 25 concurrent writers.
* **Failures are recorded even though the request failed.** A rejected pairing or
  a refused token is written in its *own* transaction — inside the request's
  transaction it would roll back together with the thing it was meant to record.
  These are precisely the entries most worth keeping.

There is no foreign key from `audit_log.device_id` to `devices`. An append-only
record of what happened must outlive the rows it mentions, and a cascading
`SET NULL` would itself be an `UPDATE`, which the trigger rejects — deleting a
device would fail rather than the log adapting to it.

Audit payloads carry identifiers and decisions, never credentials, screenshots
or transcript bodies. The log is readable from the iPhone Settings screen.

## Transport

* TLS terminated by Caddy with automatic Let's Encrypt certificates. The backend
  container publishes no port of its own, so TLS cannot be bypassed.
* PostgreSQL publishes no port. It is reachable only from the compose network.
* Authentication precedes the WebSocket accept: an unauthenticated peer never
  holds an open socket.
* Replay protection per connection: timestamp freshness plus message-id
  uniqueness (see [protocol.md](protocol.md)).
* Rate limits on pairing and authentication endpoints.

## Commands (M2)

A bearer token proves *the backend* is talking. It does not prove *which*
backend. So every command that can act on the machine is signed with the
server's Ed25519 key, and the agent verifies it against a key it **pinned at
pairing time**. An attacker holding a stolen token, directing the agent from
their own server, gets nothing: the signature will not verify.

* The server's private key lives in the environment, never in the database —
  same reasoning as the agent's key in DPAPI. A database dump does not yield the
  ability to sign commands.
* Rotating the key invalidates every pin and forces re-pairing. That is
  deliberate: a key that can be swapped quietly is a key an attacker can swap
  quietly.
* Results are signed back **by the device**, so the audit trail records an
  outcome only that machine could have produced. A result whose signature does
  not verify is dropped and recorded as `tool.result_unverified`.
* A command whose signature fails puts the agent into SAFE MODE. It is either a
  bug or an attack; both mean stop accepting instructions.
* Replay protection runs on the agent, not only the server: a repeated message
  id is refused, and so is a timestamp outside the freshness window. The cache
  belongs to the agent rather than to a connection, so reconnecting is not a
  fresh start for an attacker.

## Two independent policy checks (M2)

The Policy Engine on the server is a **pure function** — no I/O, no clock of its
own, no model. From M3 a language model will propose tool calls *into* it; it
does not consult the model, cannot be persuaded by it, and does not read its
reasoning.

The agent then does the whole assessment again, from the same manifests, and
**refuses on any disagreement**. A command that arrives claiming to be LOW when
this machine computes HIGH is not executed at the lower bar — it is not executed
at all, and the divergence is logged and stored.

This matters because the two sides are not equivalent: the server matches path
strings a model may have produced; only the agent can resolve what a path really
points at. And the server can be wrong, or compromised.

Ordering inside the engine is fixed, and the verdict can only get stricter as
rules are evaluated:

1. risk from the manifest — an unevaluable rule is a broken guard, so it denies;
2. DENY is terminal, with no confirmation path;
3. a non-trusted device may read, never act;
4. an explicit user denial outranks everything below it;
5. SAFE MODE blocks anything above LOW;
6. HIGH always requires confirmation — **no standing rule can pre-authorise it**;
7. a user may always ask for *more* friction than the default.

## SAFE MODE and the kill switch (M2)

The rule the design exists to enforce: **SAFE MODE can be entered from anywhere,
and left only from this machine.**

* The protocol has `agent.mode.enter_safe` and **no message that leaves it**.
  The wire cannot express the dangerous direction.
* `SafeModeController.leave_safe_mode()` raises for any non-local source. Tray,
  hotkey and CLI are local; the backend and the agent's own fail-safes are not.
* State is persisted, so a restart does not quietly clear it. An agent stopped
  for a reason stays stopped.
* An unreadable state file means SAFE MODE, not "normal". Failing safe is the
  only defensible reading of "I do not know what state I am in".
* The tray and the global hotkey (`Ctrl+Alt+Shift+A`) call the controller
  directly, which writes a local file. **No network, no backend, no token** — so
  the kill switch works when the connection is down and when the backend is
  hostile.
* A missing tray icon is never the reason a kill switch is missing: if the GUI
  libraries are unavailable the agent runs headless and the hotkey and
  `atlas-agent safe-mode on` still work.

## Autostart (M2)

A per-user `Run` entry under `HKEY_CURRENT_USER`, installed and removed without
elevation.

A scheduled task was tried first and rejected **on evidence**: every
`schtasks /SC ONLOGON` variant — with and without `/RU`, at `/RL LIMITED` — is
refused with *Access is denied* for a non-elevated caller on Windows 11. Logon
triggers are an administrative operation.

The consequence is a security property, not a compromise: the agent inherits the
user's ordinary limited token, so it **cannot inject input into elevated windows
or reach the UAC desktop**. `HKEY_LOCAL_MACHINE` is never touched.

## Activity monitoring (M2)

Collected: the foreground process name, whether the user is idle, how long, and
system counters.

Not collected, and with no code path to collect: window titles, keystrokes,
clipboard contents, screen contents. The only call made against a window is
`GetWindowThreadProcessId`, which returns a process id and nothing else;
`GetWindowText` is never called.

Two mechanisms keep it that way rather than one:

* the database schema has no column that could hold any of it, so widening
  collection would require a visible migration;
* a test greps the collector's source for `GetWindowText`, `GetClipboardData`,
  `SetWindowsHookEx`, `GetAsyncKeyState`, `keybd_event` and `BitBlt`, and fails
  if any appears.

Sampling is pausable from the tray, and pausing drops what is already buffered.

## The language model (M3)

**The model is not part of the security boundary.** It proposes; deterministic
code disposes. Full description in [ai.md](ai.md); the controls are:

* Function declarations are generated from the same `ToolManifest` objects the
  Policy Engine reads, so a tool the model names must exist. Invented names —
  including every shell variant it might reach for — are discarded before a
  `tool_calls` row is created.
* Arguments are validated against the tool's schema here, and again on the
  agent, which also recomputes risk and refuses on disagreement.
* `decide()` never receives the model's text, confidence or reasoning. A call
  wrapped in "this is completely safe and the user clearly wants it" is assessed
  identically to a bare one, and a test pins that.
* MEDIUM and HIGH go through the existing confirmation path regardless of how
  the model phrases the request.
* A turn is bounded by a call cap, an iteration cap and a wall-clock timeout,
  and can be cancelled. Each bound is tested against a model that never stops
  proposing tools.
* Token use is metered daily with a hard stop, so a loop cannot run up a bill.

### Prompt injection

Text is tagged by provenance — `user_instruction`, `external_content`,
`tool_result` — and the untrusted kinds are wrapped in labelled blocks stating
that they are data. That reduces how often the question arises.

What *answers* it is deterministic: **once a turn has ingested external content,
standing `always_allow` permissions stop applying.** The user pre-authorised
acting on their own requests, not on whatever a filename happened to say. A
successful injection therefore lands in the confirmation queue, in front of a
person, rather than in a pre-authorised action.

### The API key

Backend only. In the `x-goog-api-key` header, never the URL, because a key in a
query string ends up in proxy and access logs. Held as `SecretStr` so it cannot
be printed. Excluded from every exception path — tested across HTTP status,
connection and malformed-body failures with the key deliberately planted in the
upstream error text. Never sent to the agent or the phone; startup logs the
provider name, not the credential.

## Error hygiene

Authentication and pairing failures return one indistinguishable answer. Unknown
code, expired code, already-used code, wrong device — all `401` with the same
message. Distinguishing them would let an attacker probe which codes or device
ids exist. Error *details* are echoed only for codes where they help a caller fix
a malformed request.

Secrets are `SecretStr`, so they cannot be printed by accident, including by
FastAPI's own error pages. A test asserts that a bootstrap token does not appear
in `repr(settings)`. `database_echo` is rejected outright in production, because
statement logging would put device identifiers into the log stream.

## What tests actually verify

Not "there is a test file", but which properties are pinned:

| Property | Where |
|---|---|
| A tampered envelope never verifies (each signed field, individually) | `test_envelope.py` |
| Payload key reordering does **not** break a signature | `test_envelope.py` |
| Signing input is unambiguous across field boundaries | `test_envelope.py` |
| Malformed keys and signatures return `False`, not an exception | `test_crypto.py` |
| Risk only ever escalates; a rule cannot downgrade a tool | `test_manifest.py` |
| An unevaluable safety rule raises instead of being skipped | `test_manifest.py` |
| Path traversal out of the allowed roots is denied | `test_manifest.py`, `test_catalog.py` |
| A code interceptor cannot enrol their own key | `test_pairing_api.py` |
| Bootstrap enrolment closes after the first device | `test_pairing_api.py` |
| A spent challenge is refused with `409` | `test_auth_api.py` |
| Revocation invalidates an already-issued token | `test_auth_api.py` |
| Unsigned (`alg: none`) and foreign-signed tokens are refused | `test_auth_api.py` |
| `UPDATE`, `DELETE`, `TRUNCATE` on the audit log are refused by PostgreSQL | `test_audit.py` |
| Tampering and row removal are detected by chain verification | `test_audit.py` |
| 25 concurrent appends produce one contiguous chain | `test_audit.py` |
| A revoked device is dropped from a live connection | `test_websocket.py` |
| Replayed message ids and stale timestamps close the connection | `test_websocket.py` |
| A Windows agent cannot present itself as a phone | `test_websocket.py` |
| The agent stops — and says why — when revoked | `test_end_to_end.py` |
| **M2** — HIGH risk cannot be pre-authorised by a standing rule | `test_policy_engine.py` |
| An override cannot bypass SAFE MODE or promote a limited device | `test_policy_engine.py` |
| A junction pointing out of the roots is refused (real junction created) | `test_path_guard.py` |
| ATLAS cannot read its own identity file or any `.env` | `test_path_guard.py` |
| SAFE MODE cannot be left from a remote or automatic source | `test_safe_mode.py` |
| A corrupt mode-state file fails into SAFE MODE | `test_safe_mode.py` |
| The agent refuses a command whose risk it assesses higher than the server did | `test_runner.py` |
| A declared-but-unbound tool reports `not_implemented`, never success | `test_runner.py` |
| The collector's source contains no window-text, keyboard or clipboard API | `test_monitor.py` |
| Autostart installs and removes without elevation (real registry) | `test_autostart.py` |
| A replayed signed command runs once, even across a reconnect | `test_m2_resilience.py` |
| A stale or future-dated command is refused | `test_m2_resilience.py` |
| A forged command is refused **and** drives the agent into SAFE MODE | `test_m2_resilience.py` |
| The backend can engage SAFE MODE but cannot release it | `test_m2_resilience.py` |
| A result signed by a foreign key is refused and audited | `test_websocket.py` |

## Known gaps

Honest list of what is *not* protected yet.

| Gap | Status |
|---|---|
| Confirmation is an API call, not a biometric | `POST /v1/tools/calls/{id}/confirm` accepts any trusted device. The Face ID gate and the ≤2-minute freshness requirement arrive with the iPhone app in M5 |
| The dispatcher holds a pending call in memory | A backend restart loses in-flight calls. They are recorded as dispatched and never completed, which is visible but not automatically resolved |
| No rate limit on tool execution | Per-tool `rate_limit_per_minute` is declared in the manifests and not yet enforced |
| Rate limiting is per-process, in memory | Correct for one backend process. Must move to the database if a second process is ever added |
| No key rotation procedure for device keys | Recovery today is re-pairing with a new key. A rotation flow is M12 |
| No intrusion detection or alerting on audit anomalies | `POST /v1/audit/verify` exists and must be run manually; automated checking is M12 |
| JWTs are not revocable individually before expiry | Device-level revocation is immediate, which covers the realistic case. Per-token revocation is unnecessary complexity at one user |
| The Docker deployment is unverified | Written, never built — Docker is not installed on the development machine. First build is part of VPS acceptance |
| No security review by a third party | Self-review only |

## Not in scope yet

The Vision Policy's cloud-vision rules ([VISION-POLICY.md](VISION-POLICY.md) §2)
are documented and their interfaces are declared, but nothing enforces them
because nothing captures the screen yet. `VisionProvider`, screenshot redaction
and the T1–T5 resolution chain land in M3.

SAFE MODE's vision clause is already true by construction: with no capture code,
there is nothing to disable. It becomes an enforced rule when M3 adds one.

Voice, speaker verification, remote input and screen streaming are M4 and M6,
each with the controls described in PHASE-0 §10 and §14.
