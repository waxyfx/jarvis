# Tool catalogue

Every action ATLAS can take on a Windows machine is one entry in this table.
There is **no generic shell tool** and there will not be one: the risk of
`powershell -c "…"` depends entirely on a string, and a string cannot be
classified in advance. Each capability is instead declared separately, with a
typed argument schema and its own risk rules.

Declarations live in `atlas_shared/tools/catalog.py`; executors live in
`atlas_agent/tools/`. Both are required — a manifest without an executor reports
`not_implemented`, and an executor cannot be reached without a manifest. That is
what keeps the model's view, the policy's view and the agent's view identical.

## Risk classes

| Class | Meaning | What happens |
|---|---|---|
| **LOW** | Reads state, or starts something the user asked for | Runs immediately |
| **MEDIUM** | Changes something, recoverably | Requires confirmation, unless a standing user rule pre-authorises it |
| **HIGH** | Destroys work, or runs unknown code | **Always** requires confirmation. No standing rule can pre-authorise it |
| **DENY** | Structurally forbidden | Never runs. There is no confirmation path |

Risk is a ratchet: rules may raise it, never lower it. The agent recomputes it
independently and refuses if its answer is higher than the server's.

## The catalogue as of M2

| Tool | Base | Escalates to | Executor | What it does |
|---|---|---|---|---|
| `system.metrics` | LOW | — | ✅ | CPU, memory, disks, uptime, GPU temperature |
| `app.list` | LOW | — | ✅ | Running processes; optionally registered applications |
| `app.launch` | LOW | **HIGH** if the executable path is outside the known install roots | ✅ | Starts an application |
| `app.close` | MEDIUM | **HIGH** with `force` | ✅ | Asks windows to close; `force` terminates |
| `fs.search` | LOW | **DENY** if the root is outside the allowed roots | ✅ | Finds files by name |
| `fs.open` | LOW | **DENY** outside the roots; **HIGH** for executables and scripts | ✅ | Opens a file with its default application |
| `fs.delete` | MEDIUM | **HIGH** if recursive or more than 20 targets; **DENY** outside the roots | ❌ | *Declared only.* Returns `not_implemented` |

`fs.delete` is deliberately unbound in M2. Its manifest exists so the policy
rules can be written and tested before anything can act on them; deletion will
arrive with its own review.

## Details

### `system.metrics`

No arguments. Reads counters only. GPU temperature comes from `nvidia-smi` when
present; **CPU temperature is not reported at all** — on Windows it needs a
kernel driver and administrator rights, and a confidently wrong number would be
worse than none.

### `app.list`

`include_store_apps: bool = false`. Returns the 200 largest processes by memory.
Processes the agent cannot inspect (elevated ones) are skipped rather than
guessed at.

### `app.launch`

`name: str`, `arguments: tuple[str, ...] = ()`, `executable_path: str | None`.

Resolution order: an alias table of names people actually say (`chrome`,
`vs code`, `terminal`…), then `PATH`, then the Windows *App Paths* registry —
the same place the Run dialog looks.

The process is started with an **argv list and `shell=False`**. There is no
command line for an injected `&&` or `;` to live in.

An explicit `executable_path` outside `Program Files`, `Program Files (x86)` or
`Windows` is treated as an unknown binary and escalates to HIGH.

### `app.close`

`name: str | None`, `pid: int | None`, `force: bool = false`.

Without `force`, `WM_CLOSE` is posted to the process's visible windows, so the
application can prompt about unsaved work. With `force`, the process is
terminated and unsaved work is lost — which is why it escalates to HIGH.

The agent never closes itself: that would look like a crash rather than a
decision, and would drop the connection mid-command.

### `fs.search`

`query: str`, `root: str`, `max_results: int = 50` (max 1000).

Walks with `followlinks=False`, prunes directories the path guard rejects, and
stops after 200 000 entries so a search rooted at a large tree cannot become an
unbounded scan. Results are marked `truncated` when either limit is hit.

### `fs.open`

`path: str`. Opens with the default handler. Executables and scripts
(`.exe .com .scr .bat .cmd .ps1 .psm1 .vbs .js .jar .msi .reg`) escalate to
HIGH, because opening them runs code.

## The path guard

Every filesystem argument passes through the guard on the agent — after the
server's own check, and with the final say. The server matches strings; only
this machine can resolve what a path really points at.

Refused, in the order the checks run:

1. relative paths — they depend on the working directory;
2. UNC and device paths (`\\server\share`, `\\?\`, `\\.\`);
3. NTFS alternate data streams (`notes.txt:hidden`);
4. reserved DOS device names (`CON`, `NUL`, `COM1`…);
5. `..` traversal;
6. **symlinks, junctions and other reparse points that resolve out of bounds** —
   resolution happens *before* the boundary check, which is the whole point;
7. denylisted patterns, even inside an allowed root.

### Always denied, everywhere

Not configurable — the point of a floor is that a config file cannot lower it:

* `agent_identity*.json`, `atlas_device_key*` — **ATLAS cannot read the
  credential that authorises ATLAS**;
* `.env` and `.env.*`;
* `.ssh`, `.gnupg`, `.aws`, `.azure`, `.config/gcloud`, `id_rsa*`, `id_ed25519*`;
* `*.kdbx`, `*.kdb`, 1Password and Bitwarden data;
* browser profile directories;
* `*.pem`, `*.pfx`, `*.p12`, `*.jks`.

Additional patterns can be added through `ATLAS_AGENT_DENIED_PATH_PATTERNS`.

### Allowed roots

Default: `Desktop`, `Downloads`, `Documents` under the user profile. Deliberately
narrow — the rest of the profile is out of bounds until deliberately added.

The backend keeps its own copy in `ATLAS_ALLOWED_FILE_ROOTS` for a cheap
pre-filter. **Empty means no file tool can run**, which is the correct behaviour
for an unconfigured deployment.

## Adding a tool

1. Write the argument model and the manifest in `catalog.py`, including the
   escalation rules. Rules are structured data, never expression strings — there
   is no `eval` anywhere in the risk path.
2. Write the executor in `atlas_agent/tools/` and register it.
3. Add tests for the risk rules *and* for the executor. The catalogue tests in
   `test_catalog.py` assert properties across every manifest, so a new tool is
   checked for free against the invariants (irreversible ⇒ at least MEDIUM,
   rules reference declared arguments, base risk is never DENY).

The manifest is what the language model will see from M3, generated from the
same object the Policy Engine reads. They cannot drift apart.
