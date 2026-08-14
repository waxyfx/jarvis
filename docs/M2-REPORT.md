# M2 — acceptance report

> Status: **awaiting your approval**. M3 has not been started.
> Date: 2026-08-12. Builds on [M1-REPORT.md](M1-REPORT.md).

M2's criterion was the full signed path:

```
backend → policy → signed command → agent → agent policy → execution
        → signed result → audit
```

That works, end to end, against a real server and a real database. ATLAS can now
do things to this computer — within a permission model that is deterministic,
two-sided, and stoppable from the keyboard.

## 1. What exists now

| Area | Delivered |
|---|---|
| **Protocol** | Signed `agent.tool.execute` / `agent.tool.result` / `agent.tool.cancel`, one-way `agent.mode.enter_safe`, telemetry and activity batches |
| **Server identity** | Ed25519 signing key; devices pin the public half at pairing and refuse anything else |
| **Policy Engine** | Pure function, no I/O and no model. Risk from manifests, user overrides, device trust, SAFE MODE, fixed rule order |
| **Dispatch** | `tool_calls` and `permissions` tables, request/response over the hub, every outcome audited |
| **Path guard** | Real resolution before the boundary check; traversal, UNC, ADS, reserved names, reparse points, denylist |
| **SAFE MODE** | Agent-owned, persisted, enterable from anywhere, leavable only locally |
| **Tools** | Six typed executors. No shell, ever |
| **Monitoring** | Foreground process, idle time, system counters. Nothing else, by schema and by test |
| **Tray + kill switch** | Status icon, SAFE MODE toggle, monitoring pause, `Ctrl+Alt+Shift+A` global hotkey |
| **CLI** | `pair`, `status`, `run`, `safe-mode on/off/status`, `autostart install/uninstall/status` |
| **Autostart** | Per-user `Run` entry, no elevation, fully removable |

## 2. Test results

```
586 passed, 1 skipped in 137.66s
ruff: All checks passed!    ruff format: clean    mypy --strict: 68 source files, no issues
```

| Area | Tests | M1 → M2 |
|---|---|---|
| `atlas-shared` | 211 | 211 → 211 |
| `atlas-backend` | 176 | 133 → 176 |
| `atlas-agent-windows` | 160 | 41 → 160 |
| `e2e` | 40 | 8 → 40 |
| **Total** | **587** | 393 → 587 |

Code: 6 857 lines of source across 68 files; 4 458 lines of tests across 33.

The one skip is the off-Windows plaintext-key test, unreachable here because
DPAPI is always available.

## 3. Windows tools and risk levels

Full detail in [tools.md](tools.md).

| Tool | Base | Escalates to | Bound |
|---|---|---|---|
| `system.metrics` | LOW | — | ✅ |
| `app.list` | LOW | — | ✅ |
| `app.launch` | LOW | HIGH — executable outside known install roots | ✅ |
| `app.close` | MEDIUM | HIGH — with `force` | ✅ |
| `fs.search` | LOW | DENY — root outside allowed roots | ✅ |
| `fs.open` | LOW | DENY outside roots; HIGH for executables and scripts | ✅ |
| `fs.delete` | MEDIUM | HIGH if recursive or >20 targets; DENY outside roots | ❌ `not_implemented` |

`fs.delete` stays declared-only as agreed. Its manifest exists so the rules could
be written and tested ahead of the executor; asking for it returns
`not_implemented` rather than quietly doing nothing.

**No generic shell tool exists, and none will.** `app.launch` passes an argv list
with `shell=False`, so there is no command line for an injection to live in.

## 4. Security review

Full review in [security.md](security.md). What matters most:

**Commands are signed and the key is pinned.** A bearer token proves the backend
is talking; it does not prove *which* backend. The agent verifies every command
against the key it pinned at pairing. A stolen token driving the agent from
another server gets nothing.

**The agent does not trust the server's risk assessment.** It recomputes risk
from the same manifests and refuses on disagreement. A command labelled LOW that
this machine assesses as HIGH does not run at the lower bar — it does not run.

**Path resolution happens before the boundary check.** A junction inside an
allowed root that points elsewhere passes a naive check and fails this one. The
test creates a real junction and confirms the refusal.

**ATLAS cannot read its own credentials.** The identity file, `.env` files, SSH
and GPG keys, password manager stores and browser profiles are denied even
inside an allowed root, by a list no config file can shorten.

**SAFE MODE has no remote release.** The protocol has a message to enter it and
none to leave it. The controller refuses any non-local source. The tray and
hotkey write a local file, so the kill switch works with the network down.

**Monitoring cannot widen quietly.** No column exists for window titles,
keystrokes or clipboard contents, and a test greps the collector for the APIs
that would collect them.

**The agent needs no administrator rights.** Confirmed the hard way: every
`schtasks /SC ONLOGON` variant is refused without elevation on Windows 11, so
autostart uses a per-user registry entry instead. The limited token is a
capability boundary — it is why the agent cannot reach elevated windows or the
UAC desktop.

## 5. The failure modes you asked about

Each is a test in `e2e/test_m2_resilience.py` unless noted.

| Scenario | Behaviour | Verified |
|---|---|---|
| Connection to the backend lost | Reconnects with backoff; kill switch keeps working offline | ✅ 3 tests |
| Agent restarted | Identity, pinned server key and SAFE MODE all survive | ✅ |
| SAFE MODE state file corrupted | Fails into SAFE MODE; MEDIUM commands refused | ✅ |
| Same signed command delivered twice | Runs once; the repeat is `refused/replayed` — including across a reconnect | ✅ 2 tests |
| Expired command | Refused as `expired`; future-dated too; fresh still accepted | ✅ 3 tests |
| Unknown tool | Agent refuses `unknown_tool`; the API rejects it with 400 before dispatch | ✅ 2 tests |
| Command signature wrong | Refused `signature_invalid`, **and the agent enters SAFE MODE**; tampering invalidates too | ✅ 4 tests |
| Result signature wrong | Server drops it and records `tool.result_unverified` | ✅ 4 tests in `test_websocket.py` |
| Path guard bypass via junction | Refused `path_outside_roots`, end to end through a real command | ✅ |
| Backend tries to leave SAFE MODE | Impossible: no such message exists, and the controller raises | ✅ 2 tests |

Two of these were **real gaps your list found**, not confirmations:

1. **The agent had no replay protection.** A captured signed command would have
   executed a second time. Fixed by moving `ReplayGuard` into `atlas-shared` so
   both sides enforce identical rules, and giving the agent its own instance —
   owned by the transport, not the connection, so reconnecting is not a reset.
2. **The agent had no freshness check.** A command captured hours earlier would
   still have been accepted. Now bounded by `ATLAS_AGENT_COMMAND_FRESHNESS_S`
   (default 120 s).

## 6. Tray, kill switch and SAFE MODE

The tray icon shows state at a glance — teal when active, amber with a bar
through it in SAFE MODE, so it reads without relying on colour. Its menu offers
the SAFE MODE toggle, a monitoring pause, and quit.

`Ctrl+Alt+Shift+A` toggles SAFE MODE from anywhere. Three modifiers so it cannot
be hit by accident. If another application already owns the combination,
registration fails and the agent logs that the hotkey is unavailable rather than
pretending the kill switch is armed.

All three routes — tray, hotkey, `atlas-agent safe-mode on` — call the same
controller, which writes a local file. **No network is involved at any point.**

```bash
uv run atlas-agent safe-mode on
```

```bash
uv run atlas-agent safe-mode status
```

There is no `--remote` flag, no API endpoint and no protocol message that turns
it off.

## 7. Activity monitoring

Collected every 10 s: foreground process name, idle flag, idle seconds. Every
60 s: CPU, memory, disks, uptime.

Not collected: window titles, window contents, keystrokes, clipboard, screen.
Enforced twice over — the schema has nowhere to put them, and a test fails if
the forbidden APIs appear in the collector's source.

Pausable from the tray; pausing drops what is buffered rather than sending it
later.

## 8. Autostart

```bash
uv run atlas-agent autostart install
```

```bash
uv run atlas-agent autostart uninstall
```

Writes one value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
No elevation, no UAC prompt, no `HKEY_LOCAL_MACHINE`. Removal deletes the value
outright — a test asserts it is gone, not blanked.

## 9. The signed path, demonstrated

`e2e/test_signed_command_path.py`, 12 tests against real uvicorn and a real
database:

* a LOW tool runs; the agent's independent risk assessment matches; the trail
  shows `tool.dispatched` then `tool.executed`;
* a MEDIUM tool is held as `pending_confirmation`, runs only after `confirm`,
  and the trail shows `tool.confirmed`;
* a path outside the roots is `denied` and **never sent to the agent**;
* in SAFE MODE, LOW still runs and MEDIUM does not;
* the audit chain stays contiguous under load.

## 10. Known limitations of M2

| Limitation | Detail |
|---|---|
| Confirmation is an API call, not a biometric | Any trusted device can confirm. Face ID and the ≤2-minute freshness rule arrive with M5 |
| Pending calls live in memory | A backend restart loses in-flight calls; they stay recorded as dispatched |
| Per-tool rate limits are declared, not enforced | `rate_limit_per_minute` is in the manifests and unused |
| `fs.delete` is unbound | By agreement. Deletion gets its own policy review |
| The tray is Windows-only and optional | Without it, the hotkey and CLI still work |
| Docker deployment remains unverified | Unchanged from M1: written, never built |
| Monitoring has no retention job | Samples accumulate; the rollup and pruning job is M10 |

Everything from [M1-REPORT.md §9](M1-REPORT.md) still applies.

## 11. How to check this yourself

```bash
uv run pytest
```

```bash
uv run ruff check . ; uv run mypy
```

Then see it work. Start the backend, pair the agent, and ask for something:

```bash
uv run atlas-agent run
```

```bash
curl -s -X POST http://127.0.0.1:8000/v1/tools/system.metrics/execute -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{}"
```

Then press `Ctrl+Alt+Shift+A` and try `app.close` — it will come back refused,
with `refusal: safe_mode`, and no amount of confirming will change that until
you press the hotkey again.

## 12. Proposed M3 scope

Confirm and I will start:

* `LLMProvider` abstraction with `GeminiProvider` behind it
* Function declarations generated from the same manifests the policy reads
* Intent router: a cheap classifier before the model, so "what's my CPU?" does
  not cost a Pro-model call
* Natural-language → tool call → **existing** Policy Engine → execution
* Responses in Russian and English, language detected from the request
* API budget limiter with a hard daily stop
* Vision fallback per [VISION-POLICY.md](VISION-POLICY.md): UIA first, screenshot
  only when UIA cannot resolve the element, with redaction and the denylist

The security work is done first and the model plugs into it — which is the whole
reason M2 came before M3.
