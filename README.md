# JARVIS

A personal AI assistant distributed across a Windows PC, an iPhone and a small
always-on backend.

**Current state: M4 implemented and awaiting acceptance — JARVIS listens.** Say
"Jarvis", then "открой Notepad", and it opens. Say "закрой Notepad" and it asks
you to confirm first.

The model proposes; the Policy Engine and the agent decide. Speaking to it
grants nothing that typing would not: the voice path produces text and hands it
to the same endpoint, and an action held for confirmation stays held — saying
"yes" to a microphone is not the confirmation step.

`ATLAS` remains the internal namespace throughout — package names, environment
variables, signing domains, the state directory, protocol identifiers. Renaming
those would mean re-pairing devices and migrating cryptographic material for a
cosmetic gain, so only the user-facing name changed.

## Documentation

| Document | What it covers |
|---|---|
| [PHASE-0-ARCHITECTURE.md](docs/PHASE-0-ARCHITECTURE.md) | Full system architecture, platform limits, risks |
| [PHASE-0.1-DECISIONS.md](docs/PHASE-0.1-DECISIONS.md) | Tracker integration, STT strategy, MVP-first roadmap |
| [VISION-POLICY.md](docs/VISION-POLICY.md) | When a screenshot may leave the machine, and SAFE MODE |
| [protocol.md](docs/protocol.md) | Wire protocol: envelopes, signatures, close codes |
| [tools.md](docs/tools.md) | Every Windows capability, its risk class and its guard |
| [ai.md](docs/ai.md) | How the model is wired in, and why it cannot decide anything |
| [security.md](docs/security.md) | Threat model and the controls that answer it |
| [runbook.md](docs/runbook.md) | Deploying, operating, the kill switch, recovering |
| [M1-REPORT.md](docs/M1-REPORT.md) | What M1 delivered, with test and review results |
| [M2-REPORT.md](docs/M2-REPORT.md) | What M2 delivered, including the failure-mode review |
| [M3-REPORT.md](docs/M3-REPORT.md) | What M3 delivered, including the adversarial matrix |
| [M4-PLAN.md](docs/M4-PLAN.md) | The voice engine as planned, and the choices behind it |
| [M4-REPORT.md](docs/M4-REPORT.md) | What M4 delivered, what it measured, and what is still wrong |
| [PERSONALITY-ENGINE.md](docs/PERSONALITY-ENGINE.md) | Adaptive communication style — roadmap, never policy |

## Layout

```
packages/
  atlas-shared/          wire protocol, cryptography, tool manifests
  atlas-backend/         FastAPI: identity, audit, realtime hub, persistence
  atlas-agent-windows/   the local agent: identity, transport, voice runtime
  atlas-voice/           wake word, VAD, recognition, speaker profile, speech
e2e/                     end-to-end tests against a real server
training/                wake-word training and the measurements behind it
infra/                   docker compose, Caddy, Dockerfile
scripts/                 bootstrap, model download, latency and noise measurement
docs/                    architecture, operations and milestone reports
```

`atlas-shared` is imported by both the backend and the agent, so their view of
the protocol and the risk model cannot drift apart. `atlas-voice` depends on
neither: it produces text and knows nothing about tools, permissions or the
network.

## Getting started (Windows)

One command sets up everything — uv, Python 3.12, a local PostgreSQL that runs
as a plain process (no administrator rights, no Windows service), the databases,
`.env` files, dependencies and migrations:

```bash
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_dev.ps1
```

Then run the checks:

```bash
uv run pytest
```

```bash
uv run ruff check . ; uv run mypy
```

Start the backend locally:

```bash
uv run atlas-backend --reload
```

## Pairing the agent

With the backend running, issue a code using the bootstrap token from `.env`:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/pair/start -H "X-Atlas-Bootstrap-Token: $ATLAS_BOOTSTRAP_TOKEN" -H "Content-Type: application/json" -d "{\"kind\":\"windows_agent\",\"name\":\"workstation\"}"
```

Then enrol and connect the agent:

```bash
uv run atlas-agent pair --code 4F2K-9X1M
```

```bash
uv run atlas-agent run
```

`uv run atlas-agent status` shows whether this machine is paired and whether the
backend is reachable.

The agent's private key is generated locally, protected with Windows DPAPI under
your user account, and never leaves the machine. The backend stores only the
public half — a copy of its database cannot be used to command your computer.

## Voice

Download the models once (about 3 GB — Silero, sherpa keyword spotting, a
speaker embedder, Piper voices; Whisper fetches itself on first use):

```bash
powershell -ExecutionPolicy Bypass -File scripts/fetch_voice_models.ps1
```

Register your voice. Twelve short phrases, a few of them deliberately quiet or
spoken from across the room, because a profile recorded in one position only
recognises you in that position:

```bash
enroll-voice.bat
```

Then start listening:

```bash
jarvis.bat
```

Voice runs inside the agent process rather than beside it: two processes would
be two connections for one device identity, and the backend displaces the older,
so whichever started first would quietly stop working.

**Everything about your voice stays on the machine.** The recordings are deleted
once the profile exists, the profile is a few hundred numbers under DPAPI and
cannot be played back, and what crosses the network is the transcript — the same
sentence you could have typed. Nothing about your voice reaches the model.

Speaker verification decides *whose speech is acted on*. It is not
authentication and is not treated as any: every action still goes through the
Policy Engine exactly as it does from the keyboard.

## Requirements

* Windows 10/11 for the agent
* Python 3.12 (not 3.14 — the ML stack has no complete support)
* PostgreSQL 17 (the bootstrap script provides a portable one for development)
* A VPS with a domain for anything beyond local development
* For voice: an NVIDIA GPU is strongly preferred. Recognition runs on CPU but
  large-v3 is slow there; listening itself costs about 3% of one core
