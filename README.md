# ATLAS

A personal AI assistant distributed across a Windows PC, an iPhone and a small
always-on backend.

**Current state: M3 complete — ATLAS understands natural language and acts on
it, under a deterministic permission model.** Say "открой Notepad" and it opens;
say "закрой Notepad" and it asks you to confirm first.

The model proposes; the Policy Engine and the agent decide. No microphone yet —
that is M4, which closes the MVP. See
[docs/PHASE-0.1-DECISIONS.md](docs/PHASE-0.1-DECISIONS.md) for the roadmap.

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

## Layout

```
packages/
  atlas-shared/          wire protocol, cryptography, tool manifests
  atlas-backend/         FastAPI: identity, audit, realtime hub, persistence
  atlas-agent-windows/   the local agent: identity, outbound transport
e2e/                     end-to-end tests against a real server
infra/                   docker compose, Caddy, Dockerfile
scripts/                 development bootstrap, VPS latency check
docs/                    architecture and operations
```

`atlas-shared` is imported by both the backend and the agent, so their view of
the protocol and the risk model cannot drift apart.

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

## Requirements

* Windows 10/11 for the agent
* Python 3.12 (not 3.14 — the ML stack in later phases has no complete support)
* PostgreSQL 17 (the bootstrap script provides a portable one for development)
* A VPS with a domain for anything beyond local development
