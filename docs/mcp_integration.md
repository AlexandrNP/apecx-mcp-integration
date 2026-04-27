# apecx-mcp — MCP Integration Guide

The `apecx-mcp` server exposes the apecx workflow platform to any
[Model Context Protocol](https://modelcontextprotocol.io) client —
Claude Desktop, the `mcp` CLI, custom MCP clients, etc. Scientists
ask questions or describe workflows in natural language; the server
composes, surfaces a diff for review, executes, and reports.

This document is the canonical install + reference. It is accurate to
the code as of **2026-04-27** and is updated on every change to
`src/apecx_integration/mcp_surface/`.

---

## TL;DR — Claude Desktop in 5 minutes

```bash
# 1. Clone the workspace + install editable into a venv.
git clone <apecx-mcp-integration> ~/code/apecx-mcp-integration
cd ~/code/apecx-mcp-integration
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install -e ../apecx-harvesters    # required sibling
.venv/bin/pip install -e ../nanobrain           # required sibling

# 2. Bring up the Control Plane backend (Postgres + apecx-cp HTTP server).
docker compose up -d postgres
export APECX_CP_POSTGRES_URL="postgresql+psycopg://apecx:apecx@localhost:5433/apecx_cp"
.venv/bin/apecx-cp serve &                       # backend on :8000

# 3. Tell Claude Desktop about the apecx-mcp server.
#    macOS: edit ~/Library/Application Support/Claude/claude_desktop_config.json
#    Windows: %APPDATA%/Claude/claude_desktop_config.json
```

```jsonc
{
  "mcpServers": {
    "apecx": {
      "command": "/Users/<you>/code/apecx-mcp-integration/.venv/bin/apecx-mcp",
      "args": [],
      "env": {
        "APECX_CONTROL_PLANE_URL": "http://localhost:8000",
        "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
        "APECX_LLM_MODEL": "mistral-nemo:latest",
        "APECX_LLM_API_KEY": "unused"
      }
    }
  }
}
```

Restart Claude Desktop. The 11 apecx tools appear in the tool picker.
If the Control Plane is unreachable at startup, the server logs a
clear error to stderr and exits with code `2` — Claude Desktop will
surface a "server failed to start" notification.

---

## What this server is (and is not)

**Is**: a thin MCP-stdio adapter over the apecx Control Plane HTTP
API (`apecx-cp`). Each tool is a one-call wrapper that marshals the
client's input into the right JSON envelope and returns the parsed
response.

**Is not**:

- An LLM. The tools call the Control Plane, which calls the local LLM
  (Ollama / vLLM / OpenAI) via the `APECX_LLM_*` env vars. The MCP
  server itself does not invoke an LLM.
- A scheduler. HPC submission (`/hpc/submit`) is deliberately not
  exposed — the user runs `qsub` themselves on the bundle this
  server hands them.
- A persistence layer. State (runs, approvals, artifacts, provenance
  events) lives in the Control Plane's Postgres or SQLite.

The full architecture is

```
  ┌────────────────┐   stdio JSON-RPC   ┌────────────────┐   HTTP   ┌──────────┐
  │ Claude Desktop │───────────────────▶│   apecx-mcp    │─────────▶│ apecx-cp │
  │  (MCP client)  │◀───────────────────│  (this server) │◀─────────│ (backend)│
  └────────────────┘                    └────────────────┘          └──────────┘
                                                                          │
                                                              Postgres / SQLite
                                                              + local LLM (Ollama)
```

---

## Prerequisites

| Component | Required | Notes |
|---|---|---|
| Python 3.12 | yes | Earlier versions reject the type syntax |
| The `nanobrain` sibling repo | yes | Editable-installed into the venv |
| The `apecx-harvesters` sibling repo | yes | DataCite-shaped publication adapter |
| Docker | yes for Postgres backend | Skip if you run with the SQLite default |
| Ollama (or another OpenAI-compatible LLM endpoint) | yes for compose / synth | The composer + RAG synthesis call an LLM |
| MCP client | yes | Claude Desktop is the canonical one; any client works |

**Why two sibling repos?** Workspace policy: `apecx-mcp-integration`
depends on `nanobrain` (the framework) and `apecx-harvesters` (the
DataCite-shaped publication source). Day 1+ migrations pulled the
formerly-external `apecx_db_integration` and `apecx_rag` code into
this repo so the dependency surface is exactly two siblings.

---

## Installation

### 1. Clone the workspace

The workspace expects sibling repos at the same level:

```
apecx-cowork/
├── apecx-mcp-integration/    ← this repo
├── apecx-harvesters/         ← sibling (required)
├── nanobrain/                ← sibling (required)
└── data/                     ← optional, for E2E tests
```

### 2. Create the venv and install editable

```bash
cd apecx-mcp-integration
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
.venv/bin/pip install -e ../apecx-harvesters
.venv/bin/pip install -e ../nanobrain
```

This installs two console entry points into the venv:

| Command | Purpose |
|---|---|
| `apecx-cp` | Control Plane HTTP server (FastAPI, port 8000) |
| `apecx-mcp` | This MCP stdio server |

Verify:

```bash
.venv/bin/apecx-mcp --help     # FastMCP prints "apecx-mcp" + tool list
.venv/bin/apecx-cp --help      # uvicorn entry-point options
```

### 3. (Optional) Build the RAG component index

The composer's retrieval over component manifests is faster with a
prebuilt FAISS index. Build it out-of-band:

```bash
PYTHONPATH=../nanobrain:src .venv/bin/python \
  scripts/build_rag_index.py \
  src/apecx_integration/composition/composer_config.yml
```

The composer falls back to a linear-scan catalog when the index is
absent, so this step is optional for first-run setup.

---

## Configuration — environment variables

Every variable the server reads. Most have defaults; override as
needed.

### Control Plane connection

| Variable | Default | What it does |
|---|---|---|
| `APECX_CONTROL_PLANE_URL` | `http://localhost:8000` | Where this MCP server forwards each tool call |
| `APECX_MCP_SKIP_HEALTHCHECK` | unset | Set to `1` to skip the startup `/healthz` probe (offline dev only) |

The server hits `GET /healthz` at startup. If unreachable, it logs to
stderr and exits with code `2` — Claude Desktop displays "MCP server
failed to start". Without this guard, scientists would only learn the
backend was misconfigured on the first tool call, by which point the
operator has no signal that anything is wrong.

### Local LLM (used by the Control Plane's composer + by RAG
synthesis)

| Variable | Default | What it does |
|---|---|---|
| `APECX_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible endpoint |
| `APECX_LLM_MODEL` | `mistral-small:latest` | Model name the endpoint exposes |
| `APECX_LLM_API_KEY` | empty | Required by `langchain-openai` even when the endpoint ignores it; set to any non-empty string for Ollama |
| `APECX_LLM_TEMPERATURE` | `0.0` | Composer determinism — leave at 0 for reproducibility |
| `APECX_LLM_MAX_TOKENS` | `4096` | Per-call budget |
| `APECX_LLM_MAX_RETRIES` | `0` | Override `composer_config.yml` `max_retries` |

These are read by the Control Plane process (`apecx-cp`) and by the
RAG synthesis pipeline. The MCP server itself does not call an LLM
directly — it delegates everything via HTTP.

### Postgres backend (optional; SQLite is the default)

| Variable | Default | What it does |
|---|---|---|
| `APECX_CP_POSTGRES_URL` | unset → SQLite | Switches the Control Plane from `apecx_cp.db` to Postgres |

The bundled `docker-compose.yml` brings up a Postgres on port `5433`
(deliberately not 5432 to avoid colliding with a system install) with
matching credentials. Use this for production-shape state durability.

### Other (workflow-step level)

| Variable | Used by | Notes |
|---|---|---|
| `APECX_DB_DATA_DIR` | VIOLIN steps | Path to the directory carrying the VIOLIN CSVs |
| `APECX_BVBRC_CACHE_DIR` | BV-BRC step | Path to the BV-BRC TSV snapshot cache |
| `APECX_SKIP_LIVE_LLM` | tests only | `=1` makes live-LLM tests skip silently |

---

## Claude Desktop setup

The Claude Desktop config file lives at:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux (Claude Desktop preview) | `~/.config/Claude/claude_desktop_config.json` |

### Recommended config

```jsonc
{
  "mcpServers": {
    "apecx": {
      "command": "/absolute/path/to/apecx-mcp-integration/.venv/bin/apecx-mcp",
      "args": [],
      "env": {
        "APECX_CONTROL_PLANE_URL": "http://localhost:8000",
        "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
        "APECX_LLM_MODEL": "mistral-nemo:latest",
        "APECX_LLM_API_KEY": "unused",
        "APECX_LLM_TEMPERATURE": "0.0",
        "APECX_LLM_MAX_TOKENS": "4096"
      }
    }
  }
}
```

**Use the absolute path to the venv's `apecx-mcp` binary.** Claude
Desktop spawns the process directly without your shell — `~`, `$PATH`,
and shell aliases are not expanded.

After saving the file, fully quit and relaunch Claude Desktop. The
tool picker should show 11 tools prefixed with `apecx`. Hover any
tool to see its docstring + parameter schema.

### Multiple installations

You can run more than one apecx instance against different Control
Planes (e.g., a local dev backend and a staging shared backend):

```jsonc
{
  "mcpServers": {
    "apecx-local": {
      "command": "/path/local/.venv/bin/apecx-mcp",
      "env": { "APECX_CONTROL_PLANE_URL": "http://localhost:8000" }
    },
    "apecx-staging": {
      "command": "/path/staging/.venv/bin/apecx-mcp",
      "env": { "APECX_CONTROL_PLANE_URL": "https://staging.apecx.example.com" }
    }
  }
}
```

Each appears as its own tool group in the picker.

---

## Other MCP clients

### Direct stdio

Any MCP client that speaks JSON-RPC over stdio works:

```bash
.venv/bin/apecx-mcp
# server reads JSON-RPC requests on stdin, writes responses on stdout,
# logs on stderr.
```

### MCP Inspector (debugging)

```bash
.venv/bin/pip install mcp[cli]
mcp dev .venv/bin/apecx-mcp
# opens an interactive debugger at http://localhost:5173
```

### Custom Python client

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command="/path/to/.venv/bin/apecx-mcp",
    args=[],
    env={
        "APECX_CONTROL_PLANE_URL": "http://localhost:8000",
        # ... LLM env vars ...
    },
)

async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool(
            "start_workflow",
            arguments={
                "description": "find EEEV vaccines",
                "user_id": "alex",
            },
        )
```

---

## Tool reference

The server exposes 11 tools across three lifecycles. Each entry below
shows the tool's signature, the input/output JSON shape, and a usage
example as the scientist would phrase it to Claude.

### Workflow lifecycle (3 tools)

#### `start_workflow`

Compose a workflow from a natural-language description.

```python
start_workflow(
    description: str,            # required, min length 1
    user_id: str,                # required
    preferred_executor: str = "local",   # one of: "local", "hpc"
) -> dict
```

**Returns**:

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
step as requiring human review (novel Python, novel YAML). Use
`list_pending_approvals` to find what's waiting; `approve` / `reject`
/ `correct` to act on it.

**Example prompt**: *"Compose a workflow that finds EEEV vaccines."*

#### `show_diff`

Surface the differential-review payload for a run — the YAML the
composer produced, any novel Python it generated, and per-step
categorization.

```python
show_diff(run_id: str) -> dict
```

**Returns**:

```jsonc
{
  "yaml_text": "name: ...\nsteps:\n  ...",
  "novel_python_by_step": {
    "step_id": "def custom_transform(...): ...",
    "...": "..."
  },
  "categorization": [
    {
      "step_id": "entity_extraction",
      "step_class": "...EntityExtractionStep",
      "category": "composed_standard",
      "reason": "Wrapper YAML matches catalog canonical path."
    }
  ],
  "summary_sentence": "This workflow has 6 step(s). 5 compose library components..."
}
```

The `category` values are: `composed_standard`,
`composed_parameterized`, `composed_wrapped`, `novel`. Approval
policy (`configs/approval_policy.yml`) maps each category to an
action: `auto`, `require_review`, `require_expert_review`.

**Example prompt**: *"Show me the diff for the run we just created."*

#### `execute_workflow`

Run the composed workflow locally. Synchronous wrt MCP — the call
holds until the executor reaches a terminal state.

```python
execute_workflow(run_id: str) -> dict
```

**Returns**:

```jsonc
{
  "run_id": "9c1f...uuid",
  "status": "completed" | "failed" | "cancelled" | "running",
  "output_artifact_id": "1a2b...uuid" | null,
  "reason": null | "<diagnostic string>"
}
```

`status` is the **actual** DB status after `execute()` returned, NOT
the executor's intended status (cluster AJ regression fix,
2026-04-26). `reason` is `null` on a clean executor-driven completion;
non-null when another writer (sweeper, future cancel route) landed
the terminal transition first.

**Example prompt**: *"Run the workflow."*

---

### Approval lifecycle (4 tools)

#### `list_pending_approvals`

List approvals waiting for a given user.

```python
list_pending_approvals(user_id: str) -> dict
```

**Returns**:

```jsonc
{
  "approvals": [
    {
      "id": "abc123-uuid",
      "kind": "step_approval" | "workflow_approval",
      "status": "pending",
      "run_id": "...",
      "proposed_action": { /* free-form */ },
      "created_at": "..."
    }
  ]
}
```

The approval queue is **per-scientist** — there is no global view
exposed via this tool. Operators with admin access can hit the
Control Plane's HTTP API directly.

**Example prompt**: *"What approvals am I waiting on?"*

#### `approve`

Approve a pending approval.

```python
approve(
    approval_id: str,
    comment: str = "",
    decided_by: str = "api_user",
) -> dict
```

**Returns**: `{ "approval": { ... updated approval ... } }`

**Example prompt**: *"Approve approval `abc123` with comment 'looks
good'."*

#### `reject`

Reject a pending approval. **`reason` is required and non-empty** —
a reviewer who rejects must justify it.

```python
reject(
    approval_id: str,
    reason: str,         # min length 1
    decided_by: str = "api_user",
) -> dict
```

**Example prompt**: *"Reject `abc123` because the novel Python touches
the filesystem."*

#### `correct`

Approve with reviewer-supplied modifications. The `modifications`
payload is free-form; downstream consumers (e.g., a synonym-cache
writeback step) interpret its shape.

```python
correct(
    approval_id: str,
    modifications: dict,    # arbitrary JSON
    decided_by: str = "api_user",
) -> dict
```

**Example prompt**: *"Correct `abc123` to use VIOLIN ID `VO_205` instead
of `VO_99`."*

---

### HPC export lane (4 tools)

For workflows that need cluster-grade compute, the scientist exports
a qsub-able bundle, runs it manually on the HPC, and ingests the
results back. `/hpc/submit` is intentionally not exposed (the live
HPC executor is 501 at the Control Plane).

The expected sequence:

```
estimate_cost(run_id)
   ↓
confirm_allocation(run_id, confirmed_core_hours)
   ↓
export_hpc_bundle(run_id, target_system, output_directory)
   ↓
[scientist runs qsub manually on HPC; transfers result back]
   ↓
ingest_hpc_bundle(bundle_path)
```

#### `estimate_cost`

```python
estimate_cost(run_id: str) -> dict
```

**Returns**:

```jsonc
{
  "total_core_hours": 12.5,
  "per_step_core_hours": { "step_id": 3.2, "...": "..." },
  "confidence_interval": [10.0, 15.0],
  "endpoint": "polaris" | "aurora" | "...",
  "novel_python_capped_at": 4.0
}
```

Novel Python steps are capped at a conservative ceiling because their
cost is unknowable at compose time. `novel_python_capped_at` is
`null` when no novel Python is present.

#### `confirm_allocation`

```python
confirm_allocation(
    run_id: str,
    confirmed_core_hours: float,
) -> dict
```

**Returns**: `{ "run_id": "...", "confirmed": true | false }`

The scientist confirms they have an allocation grant for the cited
core-hours. Confirms are recorded as a provenance event.

#### `export_hpc_bundle`

```python
export_hpc_bundle(
    run_id: str,
    target_system: str,         # e.g. "polaris", "aurora"
    output_directory: str,      # local path to write the bundle to
) -> dict
```

**Returns**:

```jsonc
{
  "bundle_path": "/path/to/bundle/",
  "submit_command": "qsub /path/to/bundle/submit.pbs"
}
```

Bundle layout matches AP §5.5 exactly:

```
bundle/
├── submit.pbs              ← qsub-ready PBS script
├── run.sh                  ← shell entry point
├── workflow.yml            ← composed workflow
├── staging_plan.yml        ← what to copy where on the cluster
├── provenance_seed.json    ← consumed by ingest_hpc_bundle
└── README.md               ← scientist-facing instructions
```

#### `ingest_hpc_bundle`

After the scientist runs `qsub` and transfers the result directory
back, this tool consumes `provenance_seed.json` + the run output
files and lands a terminal-state Run row.

```python
ingest_hpc_bundle(bundle_path: str) -> dict
```

**Returns**:

```jsonc
{
  "run_id": "...",
  "status": "completed" | "failed",
  "output_artifact_id": "..." | null
}
```

---

## Architecture details

### Why a healthcheck at startup

If `APECX_CONTROL_PLANE_URL` points at a dead backend, scientists see
nothing wrong until the first tool call — by which point the operator
configuring Claude Desktop has already moved on. The startup `/healthz`
probe surfaces config errors **before** a scientist invokes a tool.
The error log line includes the URL the server tried, the exception
type, and remediation hints (set the env var, or set
`APECX_MCP_SKIP_HEALTHCHECK=1` to bypass).

### Why two separate HTTP clients

The startup health check builds an **ephemeral**
`ControlPlaneClient` and closes it immediately. Tool calls use a
**lazily-built singleton** client, constructed in the event loop
FastMCP runs against. This separation is load-bearing: an
`httpx.AsyncClient` binds to the event loop active when it is
constructed. The ephemeral client lives in a short-lived
`asyncio.run()` loop; if we accidentally cached it as the singleton,
every subsequent tool call would hit a client bound to a closed loop
and fail with `Event loop is closed`. Discovered via stdio JSON-RPC
e2e probing on 2026-04-25.

### Why HPC submit is not a tool

`/hpc/submit` returns 501 at the Control Plane until a live HPC
executor lands (T04/T05). Exposing a tool that always errors is
strictly worse than "tool absent" — Claude would attempt it, fail,
and the scientist would lose the run-orientation cue that "this
platform doesn't submit for you". The export bundle pattern is the
deliberate design.

### How RAG synthesis fits in

A RAG synthesis step (`RagSynthesisStep`, Day 2 v9) is registered in
the `violin_bvbrc` workflow. It assembles BV-BRC genomes + VIOLIN
mappings + RAG semantic chunks + DataCite-shaped publications into a
Markdown response with **inline citations grounded in the input
data**. The synthesizer's gates (citation grounding, min response
length, distinct-citation count) raise rather than return garbage —
silent-failure-resistant by design.

The synthesis step is loaded but not yet auto-linked into the T01
chain (the chain doesn't yet produce RAG chunks or harvester
publications). A scientist using the MCP server today exercises the
synthesis pathway via the E2E test infrastructure or via custom
workflow YAML; the standard `start_workflow` -> `execute_workflow`
flow will pick up synthesis once the upstream retrieval steps are
wired (Phase-2).

---

## Running the Control Plane backend

The MCP server speaks to the Control Plane HTTP service. Two ways to
run it:

### SQLite (zero infra; fine for dev)

```bash
.venv/bin/apecx-cp serve
```

State lands in `apecx_cp.db` in the current directory. WAL mode is
enabled for crash safety. Killing the process and restarting picks
up where you left off.

### Postgres (production-shape, persistent volumes)

```bash
docker compose up -d postgres
export APECX_CP_POSTGRES_URL="postgresql+psycopg://apecx:apecx@localhost:5433/apecx_cp"
.venv/bin/apecx-cp serve
```

The bundled `docker-compose.yml` exposes Postgres on port `5433` (not
the default 5432, to avoid colliding with a system install) with
credentials `apecx:apecx`. CI / ephemeral usage adds the
`docker-compose.ci.yml` overlay so volumes are tmpfs and nothing
survives container restart.

To tear down with destructive data removal (asks for `yes`
confirmation):

```bash
.venv/bin/apecx-cp teardown --remove-data
```

---

## Local LLM setup

The composer (used by `start_workflow`) and the optional RAG
synthesis path call an OpenAI-compatible LLM endpoint. The default
profile is Ollama with `mistral-nemo:latest`.

```bash
# 1. Install Ollama (https://ollama.ai/) and pull a model.
ollama pull mistral-nemo:latest

# 2. Confirm the daemon is reachable.
curl -s http://localhost:11434/api/tags | jq '.models[].name'

# 3. Set env vars to point at it.
export APECX_LLM_BASE_URL=http://localhost:11434/v1
export APECX_LLM_MODEL=mistral-nemo:latest
export APECX_LLM_API_KEY=unused      # any non-empty string
export APECX_LLM_TEMPERATURE=0.0
export APECX_LLM_MAX_TOKENS=4096
```

For Claude Desktop, put these in the `env` block of the MCP server
config (see "Claude Desktop setup" above) so they are inherited by
the spawned process.

**Other endpoints work** — vLLM, llama.cpp's OpenAI mode, hosted
OpenAI proper, etc. Anything that speaks the OpenAI v1 chat
completions API. Set `APECX_LLM_BASE_URL` accordingly. If you point
at hosted OpenAI, set a real API key.

---

## Troubleshooting

### "MCP server failed to start" in Claude Desktop

Check `stderr` for the apecx-mcp process. Claude Desktop captures it;
on macOS:

```bash
tail -f ~/Library/Logs/Claude/mcp-server-apecx.log
```

Common causes:

| Log line | Fix |
|---|---|
| `Control Plane at http://localhost:8000 is unreachable` | Start `apecx-cp serve`, or fix `APECX_CONTROL_PLANE_URL` |
| `ModuleNotFoundError: No module named 'apecx_integration'` | The `command` in `claude_desktop_config.json` points at the wrong venv. Use the absolute path to the venv's `apecx-mcp` binary. |
| `ModuleNotFoundError: No module named 'apecx_harvesters'` | Run `pip install -e ../apecx-harvesters` into the venv |
| `ModuleNotFoundError: No module named 'nanobrain'` | Run `pip install -e ../nanobrain` into the venv |

### Tool call returns "preferred_executor=...is not a valid executor"

The `preferred_executor` argument must be one of `local`, `hpc`. The
error message lists the allowed values. This is fail-fast; before the
fix the LLM saw an opaque pydantic enum-coercion error.

### Tool call hangs

`execute_workflow` is synchronous wrt the MCP call — it holds until
the local executor reaches terminal state. For workflows that take
> a few minutes, expect the call to wait. Cancel from Claude
Desktop's UI; the Control Plane's sweeper will mark the run FAILED if
it stays in RUNNING longer than its timeout.

### Citation grounding errors from RAG synthesis

Two error shapes you might see:

```
synthesize_response: LLM cited N token(s) that were NOT in the
retrieval inputs. The LLM is hallucinating IDs (citation grounding
has been violated). Hallucinated tokens: [...]. Allowed tokens
(from inputs): [...]
```

The local LLM emitted a citation for a DOI/genome/synonym that was
not in the retrieved context. Either:

1. The LLM is genuinely hallucinating — try a stronger model
   (`mistral-small`) or set `APECX_LLM_TEMPERATURE=0.0`.
2. Your inputs are too sparse — a single chunk gives the LLM nothing
   to ground in. Increase `max_rag_chunks` or feed richer fixtures.

```
synthesize_response: LLM response is curtailed (len=N <
min_response_chars=200)
```

The LLM returned a response below the configured floor (200 chars
default). Either your fixture is too thin (allow with
`min_response_chars: 0`) or the model timed out / was rate-limited.

### Healthcheck-blocked offline development

`APECX_MCP_SKIP_HEALTHCHECK=1` bypasses the startup probe. **Don't
ship this flag in production configs** — it's a developer escape
hatch, not a recommended deployment pattern. Tool calls still fail
when the backend is unreachable; the only difference is that the
failure surfaces on first call rather than at startup.

---

## Updating apecx-mcp

```bash
cd apecx-mcp-integration
git pull
.venv/bin/pip install -e .              # editable; usually no-op
.venv/bin/pip install -e ../apecx-harvesters
.venv/bin/pip install -e ../nanobrain
```

Restart Claude Desktop to pick up new tool signatures. The MCP server
re-runs the startup `/healthz` probe; any backend incompatibilities
surface as a clear startup error rather than silent stale tool
behavior.

If a `pyproject.toml` change adds a new dependency, the editable
install picks it up. If a sibling repo adds one, run the editable
install for that sibling too.

---

## Security notes

- **The MCP server runs on the user's machine** (not as a network
  service). It speaks JSON-RPC over stdio to a single MCP client.
- **No auth on the Control Plane HTTP API by default.** Treat it as
  localhost-only. Production deployments behind a real network must
  add an auth proxy; the `APECX_CONTROL_PLANE_URL` can point at one.
- **Local LLM calls are unencrypted** to localhost. If you point at a
  remote hosted LLM, ensure `APECX_LLM_BASE_URL` uses HTTPS.
- **Tool inputs are not sanitized for LLM-prompt-injection patterns**
  by the MCP layer. The composer's prompts are designed to be robust,
  but downstream tools that interpret the `modifications` payload
  from `correct(...)` should validate before applying.
- **Citation grounding (RAG synthesis)** is a defense-in-depth
  mitigation for LLM hallucination; do not rely on it as the only
  trust boundary for medical / clinical decisions.

---

## Reference

- `src/apecx_integration/mcp_surface/server.py` — server entry point
- `src/apecx_integration/mcp_surface/tools/{workflows,approvals,hpc}.py` —
  tool implementations
- `src/apecx_integration/mcp_surface/control_plane_client.py` — HTTP
  client targeting `apecx-cp`
- `src/apecx_integration/control_plane/schemas/api.py` — request /
  response Pydantic shapes
- `src/apecx_integration/control_plane/schemas/enums.py` — enum
  values (run status, executor kind, approval action, …)
- `configs/approval_policy.yml` — category → action mapping
- `docker-compose.yml` — Postgres backend
- `docs/api_contract.yaml` — full HTTP API spec (apecx-cp surface)