# apecx-mcp — MCP Integration Guide

The `apecx-mcp` server exposes the apecx workflow platform to any
[Model Context Protocol](https://modelcontextprotocol.io) client —
Claude Desktop, the `mcp` CLI, custom MCP clients, etc. Scientists
ask questions or describe workflows in natural language; the server
composes, surfaces a diff for review, executes, and reports.

## TL;DR — Claude Desktop in 3 steps

After a one-time `pip install` (see "Install" below), the entire
config is **one block in `claude_desktop_config.json`**. The MCP
server **autostarts the Control Plane backend** if it isn't already
running, so you do not need a separate terminal, docker compose, or
manual `apecx-cp serve`.

```jsonc
{
  "mcpServers": {
    "apecx": {
      "command": "/ABSOLUTE/PATH/TO/apecx-mcp-integration/.venv/bin/apecx-mcp",
      "args": [],
      "env": {
        "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
        "APECX_LLM_MODEL": "mistral-nemo:latest",
        "APECX_LLM_API_KEY": "unused"
      }
    }
  }
}
```

Restart Claude Desktop. The 11 apecx tools appear in the tool
picker. The first launch takes ~5–15 s while the backend boots and
runs SQLite migrations; subsequent launches are <1 s.

## Two pitfalls that cause silent failure in Claude Desktop

If the tools don't appear, the cause is almost always one of these.
Claude Desktop **does not surface a user-visible error** for either —
it just shows the empty tool list — so check both before debugging
anything else.

| Pitfall | Symptom | Fix |
|---|---|---|
| Server block placed OUTSIDE `mcpServers` | Tools never appear; no log | Indent the `apecx` block as a child of `mcpServers`, not a sibling |
| `command` points at the venv directory, not the binary | Process exits instantly with exec error; no user-visible log | Use the **absolute** path to `.venv/bin/apecx-mcp` (not `.venv`) |

The Claude Desktop logs that capture the actual stderr live at:

| OS | Log file |
|---|---|
| macOS | `~/Library/Logs/Claude/mcp-server-apecx.log` |
| Windows | `%LOCALAPPDATA%\Claude\Logs\mcp-server-apecx.log` |

Tail that file when in doubt:

```bash
tail -f ~/Library/Logs/Claude/mcp-server-apecx.log
```

If the log is **empty**, the binary couldn't even be exec'd — almost
always pitfall #2 (wrong `command` path).

## Install (one-time)

The honest current state: there is **not yet a `pip install
apecx-mcp` from PyPI**. The server depends on two sibling repos
(`nanobrain`, `apecx-harvesters`) that are also not on PyPI today.
For now the install is "clone + one editable install", which we'll
phase out as the sibling repos publish wheels.

### Standard install (clone + venv)

```bash
# 1. Clone the workspace + siblings.
mkdir ~/apecx-cowork && cd ~/apecx-cowork
git clone <apecx-mcp-integration> apecx-mcp-integration
git clone <apecx-harvesters>      apecx-harvesters
git clone <nanobrain>             nanobrain

# 2. Create a venv inside apecx-mcp-integration and editable-install
#    everything in one command.
cd apecx-mcp-integration
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ../nanobrain ../apecx-harvesters .

# 3. Verify the binary is on the venv's bin dir.
ls .venv/bin/apecx-mcp     # should print the path
```

That's everything. From here, the only ongoing requirement is the
Claude Desktop config block (above). The MCP server bundles
`apecx-cp` (the backend), spawns it on-demand, and runs against
SQLite — no Docker, no separate process to babysit.

### After install, what actually runs?

When Claude Desktop spawns `apecx-mcp`:

1. The MCP server reads `APECX_CONTROL_PLANE_URL`
   (default `http://localhost:8000`).
2. It probes `/healthz`. If the backend is already running (e.g.,
   you started one manually for development), continue.
3. If not, AND `APECX_MCP_AUTOSTART_BACKEND=1` (the default), spawn
   `apecx-cp serve` as a child process. Stderr lands in
   `$TMPDIR/apecx-cp-autostart.log`. Poll `/healthz` for up to 60 s.
4. On clean exit (stdin closes, Ctrl-C, Claude Desktop kills the
   server), an `atexit` handler SIGTERMs the child with a 5 s grace,
   then SIGKILLs if it lingers. No orphan processes.

If the autostart fails (binary not found, port conflict, migration
error), the MCP server logs the autostart-log tail to its own stderr
(which Claude Desktop captures) and exits with code `2`.

## What this server is (and is not)

**Is**: a thin MCP-stdio adapter over the apecx Control Plane HTTP
API. Each tool is a one-call wrapper that marshals MCP input into
the right JSON envelope and returns the parsed response.

**Is not**:

- An LLM. Tools call the Control Plane, which calls a local LLM
  (Ollama / vLLM / hosted OpenAI) via the `APECX_LLM_*` env vars.
- A scheduler. HPC submission (`/hpc/submit`) is deliberately not
  exposed — the user runs `qsub` themselves on the bundle this
  server hands them.
- A persistence layer. State (runs, approvals, artifacts, provenance
  events) lives in SQLite (default) or Postgres (opt-in).

```
  ┌────────────────┐   stdio JSON-RPC   ┌────────────────┐   HTTP   ┌──────────┐
  │ Claude Desktop │───────────────────▶│   apecx-mcp    │─────────▶│ apecx-cp │
  │  (MCP client)  │◀───────────────────│  (this server) │◀─────────│ (backend)│
  └────────────────┘                    └─────┬──────────┘          └──────────┘
                                              │ spawns + supervises       │
                                              │ on first start            │
                                              └───────────────────────────┘
                                                                          │
                                                              SQLite (default)
                                                              + local LLM
```

## Configuration — environment variables

The server reads these from the Claude Desktop config's `env` block.
Most have sensible defaults; you really only need the LLM block.

### Backend autostart + connection

| Variable | Default | What it does |
|---|---|---|
| `APECX_CONTROL_PLANE_URL` | `http://localhost:8000` | Where the MCP server forwards each tool call |
| `APECX_MCP_AUTOSTART_BACKEND` | `1` (on) | Spawn `apecx-cp serve` if URL unreachable. Set `0` to require a manually-started backend. |
| `APECX_MCP_SKIP_HEALTHCHECK` | unset | Set `1` to skip the startup `/healthz` probe entirely. Developer-only escape hatch — do not ship in user configs. |
| `APECX_CP_POSTGRES_URL` | unset → SQLite | Optional Postgres connection string. When set, the autostarted backend uses it instead of SQLite. |

### Local LLM (used by the composer + RAG synthesis)

| Variable | Default | What it does |
|---|---|---|
| `APECX_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `APECX_LLM_MODEL` | `mistral-small:latest` | Model name the endpoint serves |
| `APECX_LLM_API_KEY` | empty | `langchain-openai` requires a non-empty value even when the endpoint ignores it; set to `"unused"` for Ollama |
| `APECX_LLM_TEMPERATURE` | `0.0` | Composer determinism; leave at 0 for reproducibility |
| `APECX_LLM_MAX_TOKENS` | `4096` | Per-call budget |

### Workflow-step (only matters if you wire those steps in)

| Variable | Used by | Notes |
|---|---|---|
| `APECX_DB_DATA_DIR` | VIOLIN steps | Path to VIOLIN CSVs |
| `APECX_BVBRC_CACHE_DIR` | BV-BRC step | Path to BV-BRC TSV snapshots |

## Local LLM setup (Ollama)

Default profile. Install Ollama, pull a model, and the env vars
above will Just Work:

```bash
# 1. Install (https://ollama.ai/) and pull a model.
ollama pull mistral-nemo:latest

# 2. Confirm reachability.
curl -s http://localhost:11434/api/tags | jq '.models[].name'
```

Other endpoints work — vLLM, llama.cpp's OpenAI mode, hosted OpenAI
proper. Anything that speaks the OpenAI v1 chat completions API.
Set `APECX_LLM_BASE_URL` accordingly. For hosted OpenAI, set a real
API key.

## Optional: Postgres backend

SQLite is fine for a single user. For multi-process / shared state:

```bash
docker compose up -d postgres
# then add to the env block:
"APECX_CP_POSTGRES_URL": "postgresql+psycopg://apecx:apecx@localhost:5433/apecx_cp"
```

The autostart path inherits this env var, so when the MCP server
spawns the backend, the backend uses Postgres.

## Tool reference

The server exposes 11 tools across three lifecycles. Each entry
shows the signature, JSON return shape, and an example
natural-language prompt as a scientist would phrase it.

### Workflow lifecycle

#### `start_workflow`

```python
start_workflow(
    description: str,                     # required, min length 1
    user_id: str,                         # required
    preferred_executor: str = "local",    # one of: "local", "hpc"
) -> dict
```

Returns:

```jsonc
{
  "run": {
    "id": "9c1f...uuid",
    "status": "paused" | "running",
    "user_id": "alex",
    "preferred_executor": "local",
    "created_at": "2026-04-27T14:00:00Z"
  },
  "generated_workflow_artifact_id": "1a2b...uuid"
}
```

`status: paused` means the approval policy classified at least one
step as requiring human review. Use `list_pending_approvals` next.

**Prompt**: *"Compose a workflow that finds EEEV vaccines."*

#### `show_diff`

```python
show_diff(run_id: str) -> dict
```

Returns the differential-review payload — the YAML the composer
produced, any novel Python it generated, and per-step categorization
(`composed_standard` / `composed_parameterized` / `composed_wrapped`
/ `novel`).

**Prompt**: *"Show me the diff for the run we just created."*

#### `execute_workflow`

```python
execute_workflow(run_id: str) -> dict
```

Run the composed workflow locally. Synchronous wrt MCP — holds
until terminal state. Returns `{run_id, status, output_artifact_id,
reason}`. `status` is the actual DB status; `reason` is non-null
only when another writer beat the executor to the terminal
transition.

**Prompt**: *"Run the workflow."*

### Approval lifecycle

#### `list_pending_approvals`

```python
list_pending_approvals(user_id: str) -> dict
```

Per-scientist queue. **Prompt**: *"What approvals am I waiting on?"*

#### `approve`

```python
approve(approval_id: str, comment: str = "", decided_by: str = "api_user") -> dict
```

#### `reject`

```python
reject(approval_id: str, reason: str, decided_by: str = "api_user") -> dict
```

`reason` is required (min length 1) — a reviewer who rejects must
justify it.

#### `correct`

```python
correct(approval_id: str, modifications: dict, decided_by: str = "api_user") -> dict
```

Approve with reviewer-supplied modifications.

### HPC export lane

`/hpc/submit` is intentionally not exposed (the live HPC executor is
501 at the Control Plane). The expected sequence:

```
estimate_cost → confirm_allocation → export_hpc_bundle
                ↓
[scientist runs qsub manually on HPC; transfers result back]
                ↓
ingest_hpc_bundle
```

#### `estimate_cost`

```python
estimate_cost(run_id: str) -> dict
```

Returns `{total_core_hours, per_step_core_hours, confidence_interval,
endpoint, novel_python_capped_at}`.

#### `confirm_allocation`

```python
confirm_allocation(run_id: str, confirmed_core_hours: float) -> dict
```

#### `export_hpc_bundle`

```python
export_hpc_bundle(run_id: str, target_system: str, output_directory: str) -> dict
```

Returns `{bundle_path, submit_command}`. Bundle layout matches AP
§5.5: `submit.pbs`, `run.sh`, `workflow.yml`, `staging_plan.yml`,
`provenance_seed.json`, `README.md`.

#### `ingest_hpc_bundle`

```python
ingest_hpc_bundle(bundle_path: str) -> dict
```

Consumes `provenance_seed.json` + run output files and lands a
terminal-state Run row.

## Other MCP clients

### Direct stdio

Any MCP client that speaks JSON-RPC over stdio works:

```bash
.venv/bin/apecx-mcp
# Reads JSON-RPC on stdin, writes responses on stdout, logs on stderr.
```

### MCP Inspector (debugging)

```bash
.venv/bin/pip install 'mcp[cli]'
mcp dev .venv/bin/apecx-mcp
# Opens an interactive debugger at http://localhost:5173
```

### Custom Python client

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command="/path/to/.venv/bin/apecx-mcp",
    args=[],
    env={
        "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
        "APECX_LLM_MODEL": "mistral-nemo:latest",
        "APECX_LLM_API_KEY": "unused",
    },
)

async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool(
            "start_workflow",
            arguments={"description": "find EEEV vaccines", "user_id": "alex"},
        )
```

## Troubleshooting

### Tools don't appear in Claude Desktop

In order of likelihood:

1. **Config-shape error.** The `apecx` block is OUTSIDE `mcpServers`.
   Check that it is indented as a child of `mcpServers`, not a
   sibling.
2. **Wrong `command` path.** It points at the venv directory, not at
   `.venv/bin/apecx-mcp`. Use the binary's absolute path.
3. **MCP server died at startup.** Tail
   `~/Library/Logs/Claude/mcp-server-apecx.log` (macOS) /
   `%LOCALAPPDATA%\Claude\Logs\mcp-server-apecx.log` (Windows).
   Empty log = the binary couldn't be exec'd (back to #2).
4. **Autostart backend failed.** The MCP server log will show
   `autostart spawned backend ... did not become ready within 60s`
   followed by the autostart log tail. Common causes:
   - **Port 8000 already in use** by something other than apecx-cp.
     Either free the port or set
     `APECX_CONTROL_PLANE_URL=http://localhost:<other-port>`.
   - **Missing dependency** — a sibling-repo editable install was
     skipped. Run `pip install -e ../nanobrain ../apecx-harvesters .`
     in the venv.
   - **Migration error** on first run — usually permissions on the
     working directory where SQLite tries to create `apecx_cp.db`.
5. **Restart Claude Desktop.** Full quit, not just minimize. The
   config is read once at launch.

### Tool call returns "preferred_executor=...is not a valid executor"

The argument must be one of `local`, `hpc`. Fail-fast.

### Tool call hangs

`execute_workflow` is synchronous wrt the MCP call — it holds until
the local executor reaches terminal state. For long workflows,
expect the call to wait. Cancel from Claude Desktop's UI; the
sweeper will mark the run FAILED if it stays in RUNNING longer than
its timeout.

### Citation grounding errors from RAG synthesis

The local LLM emitted a citation for a DOI/genome/synonym that was
not in the retrieved context. Either:

1. The LLM is hallucinating — try a stronger model or set
   `APECX_LLM_TEMPERATURE=0.0`.
2. Inputs are too sparse — increase `max_rag_chunks` in the
   synthesis config.

### "Connection refused" on tool calls but no autostart logs

The Control Plane URL points at a host the autostart can't manage
(non-loopback). The autostart deliberately refuses to spawn against
public IPs — that's a security shape, not a bug. Either point
`APECX_CONTROL_PLANE_URL` at `localhost:8000` or start the backend
manually on the remote host and set
`APECX_MCP_AUTOSTART_BACKEND=0`.

## Honest limitations

What you should know if you're betting production work on this:

- **No PyPI publication yet.** `pip install apecx-mcp` is the goal
  but is gated on publishing `nanobrain` and `apecx-harvesters`. For
  now the install requires three local clones + one editable install
  command. Pipeline to PyPI is tracked in `docs/future_work.md`.
- **No auth on the Control Plane HTTP API.** The autostart path
  binds to localhost, so this is fine for the single-user case. For
  shared deployments behind a real network, put an auth proxy in
  front and set `APECX_CONTROL_PLANE_URL` to the proxy.
- **Autostart runs as the same user as Claude Desktop.** SQLite
  files land in the cwd Claude Desktop spawned `apecx-mcp` from
  (varies per platform). For production, set `APECX_CP_POSTGRES_URL`
  to pin state somewhere durable.
- **No automatic backend upgrade.** When you `git pull` updates,
  `pip install -e .` is required to pick up the changes; an old
  cached binary on PATH would otherwise be stale. The MCP layer
  doesn't detect this.
- **No multi-user namespacing.** All state is shared across whoever
  hits the same Control Plane. The `user_id` field is informational,
  not an isolation boundary.
- **No prompt-injection hardening on tool inputs.** The composer's
  prompts are designed to be robust against accidental injection
  from the workflow description, but any tool that interprets the
  free-form `modifications` payload from `correct(...)` should
  validate before acting. Citation grounding (RAG synthesis) is
  defense-in-depth, not a clinical-trust boundary.

## Updating

```bash
cd apecx-mcp-integration
git pull
.venv/bin/pip install -e ../nanobrain ../apecx-harvesters .
```

Restart Claude Desktop. The MCP server picks up the new code; the
autostarted backend uses the same binary so it gets the update too.

## Reference

- `src/apecx_integration/mcp_surface/server.py` — server entry +
  autostart
- `src/apecx_integration/mcp_surface/tools/{workflows,approvals,hpc}.py`
- `src/apecx_integration/mcp_surface/control_plane_client.py`
- `src/apecx_integration/control_plane/schemas/api.py` — request /
  response shapes
- `src/apecx_integration/control_plane/schemas/enums.py`
- `configs/approval_policy.yml` — category → action mapping
- `docs/api_contract.yaml` — full HTTP API spec
