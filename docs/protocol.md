# ATLAS wire protocol, version 1

Every message between any two ATLAS components is an **envelope**. One format,
one parser, one place where validation happens — so a component can never act on
something it did not fully understand.

Defined in `atlas_shared.protocol`, imported by both sides.

## Envelope

```jsonc
{
  "v": 1,                                  // protocol version
  "id": "01K2M4YB8QZC5R7T9V0XA3D6EF",      // ULID, unique per message
  "corr_id": "01K2M4YB1234567890ABCDEFGH", // correlates a response to its request
  "ts": "2026-08-12T10:31:02.123Z",        // UTC, milliseconds, always 'Z'
  "kind": "cmd",                           // cmd | res | evt | err
  "type": "agent.hello",                   // registered message type
  "payload": { },                          // schema determined by `type`
  "sig": "base64url"                       // Ed25519 signature (optional)
}
```

Rules the parser enforces, in this order:

1. **Version first.** A `v` that is missing or null is `malformed`; a `v` that is
   present but different is `unsupported_version`. On a version mismatch nothing
   else in the frame is trustworthy, so no other field is examined.
2. **Envelope shape.** Unknown fields are rejected. `id` and `corr_id` must be
   canonical uppercase ULIDs. `ts` must carry a timezone.
3. **Known type.** An unregistered `type` is `unsupported_type`. A type cannot be
   invented by a peer.
4. **Permitted kind.** Each type declares which kinds it may appear as; anything
   else is `invalid_kind`.
5. **Payload schema.** Validated against the model bound to the type. Unknown
   payload fields are rejected too.

## Timestamps and canonical form

`ts` is always UTC, millisecond precision, `Z` suffix — one formatting, because
signatures cover the string. Payloads are hashed through a canonical JSON
encoding (sorted keys, no insignificant whitespace, literal UTF-8, no NaN, no
non-string object keys), so a peer may re-serialise a payload with different key
order and the signature still verifies.

## Signatures

```
signing input = "atlas.envelope.v1" ␟ v ␟ id ␟ corr_id ␟ ts ␟ kind ␟ type ␟ sha256(canonical(payload))
```

`␟` is ASCII 0x1F. None of the joined fields can contain it, so the pre-image is
unambiguous — two envelopes whose fields happen to concatenate identically still
produce different inputs. The domain prefix means an envelope signature can
never be replayed as a signature over anything else ATLAS signs.

The payload enters as a hash rather than inline: the pre-image stays small and
its framing cannot be confused by payload content.

## Message types in version 1

M1 defines the connection lifecycle. Later phases register their own types the
same way; an unregistered type is refused, so a newer peer cannot smuggle
behaviour past an older one.

| Type | Kind | Direction | Purpose |
|---|---|---|---|
| `agent.hello` | cmd | agent → server | Introduce a Windows agent |
| `client.hello` | cmd | client → server | Introduce an iOS or web client |
| `server.hello_ack` | res | server → peer | Session parameters; correlates to the hello |
| `conn.ping` | cmd | either | Liveness probe |
| `conn.pong` | res | either | Answer to a ping |
| `agent.mode.changed` | evt | agent → server | SAFE MODE transition |
| `server.error` | err | server → peer | Protocol or application error |

A device may only send the hello matching the kind it was enrolled as: a Windows
agent cannot present itself as a phone.

## Connection lifecycle

```
authenticate (HTTP)  →  connect (WSS)  →  hello / hello_ack  →  serve
```

Authentication happens **before** the WebSocket is accepted. A peer without a
valid token is refused during the handshake and never gets an open socket.

After accepting, the server expects a hello within `hello_timeout_s`. Once
acknowledged, the connection is served until the peer leaves, stops answering
heartbeats, or violates the protocol.

**Heartbeats.** The server pings after `heartbeat_interval_s` of silence and
drops the connection after `heartbeat_interval_s × heartbeat_grace_periods`
without any traffic. Agents answer `conn.ping` with `conn.pong`.

**One connection per device.** Registering a second connection closes the first
with `4409`. Without that rule there is no single answer to "where do I send this
command?", and a stale socket could swallow instructions meant for the live one.

## Replay protection

Two independent checks, per connection:

* **Freshness** — a timestamp further than `clock_skew_tolerance_s` from server
  time, in either direction, is refused. This bounds how long a captured frame
  stays useful.
* **Uniqueness** — a message id seen before on this connection is refused.

The id cache is bounded; the freshness window is what makes that safe, since an
id old enough to be evicted is already too old to pass the clock check.

## Close codes

Clients branch on these, so they are part of the contract.

| Code | Meaning | What the peer should do |
|---|---|---|
| `1001` | Server going away | Reconnect after a short delay |
| `4400` | Malformed frame | Fix the client; retrying will not help |
| `4401` | Unauthorized | Obtain a new token, then reconnect |
| `4402` | Unsupported protocol version | **Stop.** Upgrade the software |
| `4403` | Device revoked | **Stop.** Re-pair to restore access |
| `4408` | Handshake or heartbeat timeout | Reconnect |
| `4409` | Replaced by a newer connection | Wait longer before reconnecting |
| `4410` | Replay detected | Reconnect with a fresh session |

The agent treats `4402` and `4403` as fatal and exits with an explanation.
Retrying either forever would only hide the problem.

## Error codes

Carried in `server.error` payloads and in REST bodies.

`malformed`, `unsupported_version`, `unsupported_type`, `invalid_kind`,
`unauthorized`, `forbidden`, `signature_invalid`, `replay_detected`, `timeout`,
`rate_limited`, `safe_mode`, `tool_not_implemented`, `internal`.

Details are echoed to the caller only for the codes where they help fix a
request. Authentication failures deliberately carry no detail: telling a caller
*why* a credential was rejected helps them find one that works.

## Recoverable versus fatal

A frame the server cannot parse closes the connection. A frame it parses but
does not handle produces a `server.error` and the connection continues — that
way a newer client stays usable against an older server instead of failing
outright.
