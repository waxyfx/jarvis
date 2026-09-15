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

## Starting JARVIS

On the machine that runs everything, one thing to double-click:

```bash
start-jarvis.bat
```

It brings up the database, applies any pending migrations, starts the backend,
waits for it to answer `/v1/health/ready`, and only then opens the microphone.
Each step is checked before the next begins, so a failure says which step.

It is idempotent. A database already running is left alone. A backend already
listening is **used rather than replaced** — two backends on one database would
both run the proactive scheduler, and every reminder would arrive twice.

Whatever it started, it stops: Ctrl+C or closing the window takes down the
backend and the database it brought up, and leaves alone anything that was
already running.

`jarvis.bat` starts only the agent. Use it when the backend runs somewhere else.

| Symptom | Where to look |
|---|---|
| "the database did not start" | `.pgdata\server.log` |
| "migrations failed" | `.logs\migrate.log` |
| "the backend stopped on startup" | `.logs\backend.err.log` — usually a missing value in `.env` |
| "the backend did not become ready" | `.logs\backend.log` |

## Autostart on Windows

The agent must run **in your interactive session**, not as a service: a Session 0
service cannot see your desktop, which later phases need for input and screen
capture.

On the machine that runs the whole stack, register the launcher rather than the
agent — an agent that starts at logon with no backend to connect to is a tray
icon that does nothing:

```bash
uv run atlas-agent autostart install --everything
```

Where the backend lives elsewhere, the agent alone is right:

```bash
uv run atlas-agent autostart install
```

```bash
uv run atlas-agent autostart status
```

```bash
uv run atlas-agent autostart uninstall
```

This writes one value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
No elevation is needed, and none is requested.

A scheduled task would have been the tidier mechanism, but every
`schtasks /SC ONLOGON` variant is refused with *Access is denied* for a
non-elevated caller on Windows 11 — logon triggers are administrative. Requiring
a UAC prompt to install the agent was the worse trade.

Running unelevated is a security property, not an oversight: it means ATLAS
cannot inject input into elevated windows or touch the UAC prompt at all.

## The kill switch

Three routes, all local, none of which touch the network:

* the tray menu — **SAFE MODE (kill switch)**;
* the global hotkey `Ctrl+Alt+Shift+A`;
* the command line:

```bash
uv run atlas-agent safe-mode on
```

```bash
uv run atlas-agent safe-mode status
```

In SAFE MODE the agent runs low-risk local reads and nothing else. Cloud vision
is refused unconditionally, and no confirmation can lift the restriction.

**There is no remote release.** The protocol has a message to enter SAFE MODE and
none to leave it, and the controller refuses any non-local caller. Releasing it
requires a person at this machine:

```bash
uv run atlas-agent safe-mode off
```

State is persisted, so a restart does not clear it. If the state file is ever
unreadable, the agent starts in SAFE MODE and says so.

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

## Connecting the tracker

JARVIS can read and update the owner's Life OS — Sunny, deployed on Vercel. The
integration is **off until configured**: without both settings the tracker tools
are not offered to the model, nothing is constructed, and the backend starts
exactly as it does now.

Two settings, on the **backend only**:

```
ATLAS_SUNNY_BASE_URL=https://<the Vercel deployment>
ATLAS_SUNNY_TOKEN=<the machine token>
```

**The token does not go anywhere else.** Not the Windows Agent, not the phone,
not git, not a test fixture, not a support message. It is a credential for a
remote service with write access to the owner's data, which puts it in the same
class as the Gemini key and under the same rule. The backend is the only process
that needs it because the backend is the only process that calls Sunny — the
tracker is a web service, not a Windows capability, and a question about today's
tasks should still be answerable when the laptop is off.

### Issuing a token

Sunny's side already exists: `src/server/auth.ts` accepts a bearer token when
`JARVIS_API_TOKEN` is set, compares it in constant time, requires at least 24
characters, binds it to one account through `JARVIS_USER_EMAIL`, and never logs
it. Nothing needs writing; the token needs generating and placing.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the same value in three places and nowhere else:

1. Sunny's local `.env`, if you run it locally.
2. Sunny's **Vercel** environment, or the deployment will refuse it.
3. The JARVIS backend's `.env` as `ATLAS_SUNNY_TOKEN`.

Also set `JARVIS_USER_EMAIL` in Sunny to the owner's account. Without it Sunny
falls back to whichever user its database returns first, which is harmless with
one account and is not a property worth relying on.

### Rotating it

Generate a new one and update the same three places. There is no revocation list
to maintain: Sunny rejects the previous value the moment its environment
changes, which is a point in favour of the design it already had. JARVIS will
report the tracker as unavailable in the gap — the tools disappear from what the
model is offered rather than failing mid-sentence.

Rotate if the value was ever pasted into a chat, a log, an issue or a shell
history that others can read. There is no way to tell whether a shared secret
has been copied, so the question is not "was it used" but "could it have been".

### The tracker must require a login

Sunny defaults to `REQUIRE_AUTH=false`, which is a deliberate choice recorded in
its own code: a personal app with no sign-in. On a laptop that is reasonable. On
a public deployment it means anyone who knows the hostname reads and writes the
owner's data, which is what was found here — an unauthenticated `GET
/api/tasks?scope=today` returned real tasks.

Production now sets `REQUIRE_AUTH=true`. Three environment variables make that
safe rather than merely closed, and the order matters:

1. `JARVIS_API_TOKEN` — without it in the deployment, turning on auth locks the
   assistant out. The machine token is checked *before* the auth requirement,
   which is why the assistant keeps working.
2. `JARVIS_USER_EMAIL` — the account the machine token acts as. Without it Sunny
   falls back to whichever user its database returns first.
3. `REQUIRE_AUTH=true` — last, once the other two are in place.

Environment changes need a deployment to take effect. Redeploy the existing
build rather than pushing from a working copy: `vercel redeploy <deployment>`
re-runs the same code with the new environment, where `vercel --prod` would
deploy whatever is on disk, including work in progress.

### Checking it works

With both settings present, ask the assistant something the tracker answers —
"what do I have today". The tool call is `tracker.today` and it should complete.
A tracker failure is reported in the reply rather than raised, so a broken
integration sounds like "the tracker could not answer" rather than taking the
turn down.

| Symptom | Cause | Fix |
|---|---|---|
| The model never uses a tracker tool | Not configured | Both settings must be present; either one alone leaves the tracker off |
| "the tracker refused the token" | Sunny got a token it does not recognise | The Vercel environment and the backend disagree. Rotate to the same value in both |
| "the tracker did not answer in time" | Sunny cold-starting, or unreachable | Vercel's first request after idle is slow; if it persists, check the deployment |
| "the tracker answered with something that is not JSON" | A Vercel error page | Usually a deployment that is failing to build |
| Tracker tools offered but every call is refused | A MEDIUM action awaiting confirmation | Completing, moving and re-prioritising are held by the Policy Engine by design |

## When JARVIS speaks first

Reminders, the morning briefing, the evening summary and the long-session nudge
are on by default and need nothing configured. Everything about them is a
setting, and the defaults are the ones described in
[M5-REPORT.md](M5-REPORT.md).

| Setting | Default | What it does |
|---|---|---|
| `ATLAS_PROACTIVE_ENABLED` | `true` | The whole feature. `false` and nothing is ever said unprompted |
| `ATLAS_PROACTIVE_INTERVAL_S` | `60` | How often the rules get a chance to fire |
| `ATLAS_OWNER_TIMEZONE` | `Asia/Almaty` | **Load-bearing.** Everything is decided in local time |
| `ATLAS_BRIEFING_HOUR` / `_UNTIL_HOUR` | `8` / `12` | The window the morning briefing may land in |
| `ATLAS_EVENING_SUMMARY_HOUR` / `_UNTIL_HOUR` | `21` / `23` | The same, for the evening |
| `ATLAS_LONG_SESSION_MINUTES` | `90` | Unbroken time at the desk before it says something |
| `ATLAS_QUIET_FROM_HOUR` / `ATLAS_QUIET_UNTIL_HOUR` | `23` / `7` | Outside these hours, shown but not spoken |

Things that are working correctly and can look like faults:

| What you see | Why |
|---|---|
| No morning briefing today | You were not at the machine between 08:00 and 12:00. A briefing at four in the afternoon is not a briefing |
| A reminder appeared but was not spoken | Quiet hours, or SAFE MODE. Both silence the voice and keep the text |
| Nothing at all while the laptop is on | Presence is "the last activity sample is recent and not idle". Nothing is said to an empty chair |
| A briefing repeated after a restart | Known. What has been said is kept in memory only |
| Reminders stop after a backend restart | They should not — check the log for `scheduler_started` |

The one setting that can actually break it is the timezone. An unknown name
falls back to UTC and logs `unknown_timezone`; if the briefing arrives in the
middle of the night, that log line is the first thing to look for.

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
| Agent enters SAFE MODE by itself, log says `agent_command_signature_invalid` | Something sent a frame claiming to be the backend | Treat as an incident. The server signing key and the agent's pin disagree, or someone is on the wire |
| "найди в интернете" answers from memory | Web tools switched off, or the model chose not to search | Check `ATLAS_WEB_TOOLS_ENABLED`; the tools are removed from the model's list when off |
| Every search returns nothing found | DuckDuckGo Lite changed, or is challenging the request | `web.search` distinguishes "no results" from a refusal — a refusal reports the status code |

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
