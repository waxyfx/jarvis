# Security model — as implemented in M1

This is a review of what exists today, not of the design target. Controls that
land in later phases are listed as gaps, not as features.

Scope of M1: device identity, enrolment, authentication, the audit trail, and
the realtime transport. **No tool execution, no AI model, no microphone, no
screen access exists yet** — so the largest attack surface of the finished
system is not present, and neither are its defences.

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

## Known gaps

Honest list of what is *not* protected yet.

| Gap | Status |
|---|---|
| Commands to the agent are not yet signed end-to-end | The envelope signature scheme exists and is tested; nothing sends signed commands because no commands exist yet. Wired up in M2 |
| Rate limiting is per-process, in memory | Correct for one backend process. Must move to the database if a second process is ever added |
| No key rotation procedure for device keys | Recovery today is re-pairing with a new key. A rotation flow is M12 |
| No intrusion detection or alerting on audit anomalies | `POST /v1/audit/verify` exists and must be run manually; automated checking is M12 |
| JWTs are not revocable individually before expiry | Device-level revocation is immediate, which covers the realistic case. Per-token revocation is unnecessary complexity at one user |
| The Docker deployment is unverified | Written, never built — Docker is not installed on the development machine. First build is part of VPS acceptance |
| No security review by a third party | Self-review only |

## Not in scope for M1

Everything the Vision Policy and permission model cover — SAFE MODE enforcement,
risk-based confirmation, path guards at execution time, screenshot redaction —
is *declared* in `atlas-shared` and *documented* in
[VISION-POLICY.md](VISION-POLICY.md), but nothing enforces it yet because
nothing executes yet. The manifests and risk machinery exist so that M2 and M3
bind to a contract that was designed before the code that uses it.
