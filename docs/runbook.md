# Runbook

Operating ATLAS: deploying, pairing, recovering. Written for the person who owns
the system — which is the same person who built it.

## First deployment to a VPS

Nothing here has been executed yet; the Docker path is written but unverified
(no Docker on the development machine). Treat the first run as part of
acceptance, not as a rehearsed procedure.

1. **DNS.** Point an `A` record at the VPS. Caddy needs this before it can
   obtain a certificate.
2. **Secrets.** Copy `.env.example` to `.env` and fill it in. Generate each
   secret separately:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
   `ATLAS_ENVIRONMENT=prod` turns on the production checks: the JWT secret must
   be at least 32 characters, statement logging is refused, and the interactive
   API docs are not served.
3. **Bring up the database and backend.**
   ```bash
   cd infra && docker compose --env-file ../.env up -d --build
   ```
4. **Apply migrations.** They are deliberately *not* run on container start — an
   automatic migration on boot turns a rollback into a data-loss event.
   ```bash
   docker compose exec backend alembic -c /app/alembic.ini upgrade head
   ```
5. **Check.**
   ```bash
   curl -fsS https://your-domain/v1/health/ready
   ```

## Pairing the first device

`ATLAS_BOOTSTRAP_TOKEN` authorises enrolment only while **no device exists**. It
stops working the moment the first one is paired, so a forgotten token in `.env`
is not a way in later.

On the VPS:

```bash
curl -s -X POST https://your-domain/v1/pair/start -H "X-Atlas-Bootstrap-Token: $ATLAS_BOOTSTRAP_TOKEN" -H "Content-Type: application/json" -d '{"kind":"windows_agent","name":"workstation"}'
```

On Windows, within 5 minutes:

```bash
uv run atlas-agent pair --code 4F2K-9X1M
```

```bash
uv run atlas-agent run
```

Confirm it landed:

```bash
curl -s https://your-domain/v1/pair/status
```

## Pairing further devices

Once one device is trusted, it authorises the rest — the bootstrap token is no
longer involved. Authenticate as the paired device and call `/v1/pair/start`
with its bearer token, choosing `"kind": "ios"` for the phone.

## Autostart on Windows

The agent must run **in your interactive session**, not as a service: a Session 0
service cannot see your desktop, which later phases need for input and screen
capture. Register a logon task:

```bash
schtasks /Create /TN "ATLAS Agent" /TR "\"%LOCALAPPDATA%\\uv\\tools\\atlas-agent.exe\" run" /SC ONLOGON /RL LIMITED
```

`/RL LIMITED` is intentional. Running the agent without administrator rights is
a security property, not an oversight: it means ATLAS cannot inject input into
elevated windows or touch the UAC prompt at all.

## Routine checks

**Verify the audit chain.** Recomputes every hash and reports the first
inconsistency. A failure means the log was modified outside the append path —
that is a security incident, not a bug to paper over.

```bash
curl -s -X POST https://your-domain/v1/audit/verify -H "Authorization: Bearer $TOKEN"
```

**Read recent activity.**

```bash
curl -s "https://your-domain/v1/audit?limit=50" -H "Authorization: Bearer $TOKEN"
```

**List devices**, including which are connected right now:

```bash
curl -s https://your-domain/v1/devices -H "Authorization: Bearer $TOKEN"
```

## Emergency: revoke everything

If a device is lost, or you suspect the backend is compromised:

```bash
curl -s -X POST https://your-domain/v1/devices/revoke-all -H "Authorization: Bearer $TOKEN"
```

Every device is revoked immediately, **including the one making the call**, and
live connections are dropped. That is the intent: after a suspected compromise,
everything should have to prove itself again.

Recovery: set a fresh `ATLAS_BOOTSTRAP_TOKEN`, restart the backend, and re-pair.
Revoked devices are not deleted, and their old keys cannot be re-enrolled —
recovery means a new key, not resurrecting the old identity.

From M2 the agent also has local kill switches that do not need the network: a
tray menu item and a global hotkey, both entering SAFE MODE.

## Backups

Everything that matters is in PostgreSQL. The audit chain makes tampering
detectable, which is only useful if you still have the log.

```bash
docker compose exec -T postgres pg_dump -U atlas atlas | gzip > atlas-$(date +%F).sql.gz
```

Restore into an empty database, then verify the chain before trusting it. Note
that the append-only triggers block a plain `pg_restore` into a populated
`audit_log`; restore into a fresh database instead.

The agent's identity file is **not** backed up on purpose. It is
machine-specific, DPAPI-encrypted, and useless elsewhere. If the machine is lost,
revoke its device and pair the replacement.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Agent exits: "device has been revoked" | Close code `4403` | Re-pair. Retrying would only hide it |
| Agent exits: "upgrade the agent" | Close code `4402`, protocol mismatch | Deploy matching versions of agent and backend |
| Agent reconnects in a loop with `4401` | Token rejected | Check the clock on both machines; skew beyond `ATLAS_CLOCK_SKEW_TOLERANCE_S` breaks tokens |
| `4409` repeatedly | Two agent processes for one device | Stop the duplicate; one connection per device is enforced |
| Pairing returns 403 "bootstrap pairing is closed" | A device already exists | Authorise from the paired device instead |
| Pairing returns 401 | Wrong, expired or already-used code | Issue a new one. The response is deliberately identical for all three |
| `/v1/health/ready` fails | Database unreachable | `docker compose ps`, then `docker compose logs postgres` |
| Audit verify returns `ok: false` | The log was modified outside the append path | Treat as an incident: preserve the database, check `first_bad_seq` |

## Local development

Set up once:

```bash
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_dev.ps1
```

The development PostgreSQL runs as a plain process on port 55432, not as a
service. Manage it directly:

```bash
.tools/pgsql/bin/pg_ctl.exe -D .pgdata status
```

```bash
.tools/pgsql/bin/pg_ctl.exe -D .pgdata -o "-p 55432" -l .pgdata/server.log start
```

Run the checks:

```bash
uv run pytest
```

```bash
uv run ruff check . ; uv run ruff format --check . ; uv run mypy
```

Integration tests skip themselves, loudly, when `ATLAS_TEST_DATABASE_URL` and
`ATLAS_E2E_DATABASE_URL` are unset. If you see a large skip count, load
`.env.test` into your shell first.

## Creating a migration

```bash
uv run alembic -c packages/atlas-backend/alembic.ini revision --autogenerate -m "add x"
```

Read the generated file before applying it — autogenerate does not understand
data, and it will happily write a destructive change. Every migration needs a
working `downgrade`.
