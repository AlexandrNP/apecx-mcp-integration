# apecx-mcp-integration

MCP server for the APECx scientific platform. Exposes **23
scientist-facing tools** to Claude Desktop (or any MCP client): query
VIOLIN + BV-BRC + Globus Search databases, compose workflows from
natural-language descriptions, review diffs, execute locally, export
to HPC.

> **License: MIT.** See [`LICENSE`](LICENSE).

## Install

```bash
# 1. install uv (skip if you already have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. install apecx-mcp + its two sibling repos in one shot
uv tool install --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git

# 3. configure Claude Desktop and (optionally) transfer datasets
apecx-setup
```

`apecx-setup` is interactive: it confirms the data directory,
**offers to install Ollama if missing** (Homebrew on macOS / the official
install script on Linux — every command printed before a y/N prompt),
starts the daemon, pulls the configured model (`nemotron-3-nano:4b` by
default), and patches `claude_desktop_config.json` with the right paths.

**Two LLM roles — don't conflate them.** In the **desktop / MCP** mode you'll
normally use, **Claude Desktop itself is the analysis & synthesis LLM**: it
calls the apecx tools, which return deterministic data + scaffolds for it to
reason over. No apecx-side LLM endpoint is needed for that path. The Ollama
model `apecx-setup` pulls powers the separate **backend / headless** path —
the workflows that synthesize markdown *internally* (`run_workflow`,
`synthesize_query`). That backend LLM defaults to local Ollama
(`localhost:11434`); point it elsewhere with `APECX_LLM_BASE_URL` (there is no
remote default). So the Ollama install is optional precisely *because* the
desktop LLM covers the primary analysis path — not because some default
endpoint exists.

**The synonym dictionary auto-downloads anonymously** on first MCP launch
(~47 MB compressed, ~30 s on a typical home connection) from a public
Globus HTTPS path. No credentials, no env vars, no `apecx-globus-setup`
needed for this — it just works.

**Globus authentication is OPTIONAL** and only required when you also want
to transfer the VIOLIN + BV-BRC genomic datasets via `apecx-setup data`
(BV-BRC required for that path; VIOLIN optional). To enable that step:

```bash
apecx-globus-setup login                                            # opens browser
export APECX_GLOBUS_SOURCE_ENDPOINT_ID=<ask the data steward>
export APECX_GLOBUS_DEST_ENDPOINT_ID=<your Globus Connect Personal endpoint UUID>
apecx-setup data
```

If you skip Globus setup, the MCP server and all dictionary-backed
lookup tools still work; only the domain-data transfer step is unavailable.

After it finishes, **fully quit Claude Desktop** (Cmd-Q on macOS —
closing the window is not enough) and reopen. The 23 apecx tools
appear in the tool picker after 2–5 seconds.

### Check what your install can do

From the terminal:

```bash
apecx-setup capabilities
```

Or **from inside Claude Desktop**, ask it to call the `apecx_capabilities`
MCP tool — same data, queried live over MCP (it also drives `list_workflows`
+ `infrastructure_status` under the hood).

Both show: which workflows are **runnable now** versus **need configuration**
(with the missing prerequisite *and* an honest fallback — e.g. a Docker/Rhea
workflow points you at the MAFFT or LLM-only path), plus the backend roster.
A fresh install with zero infrastructure already covers the primary path:
entity resolution + harmonized multi-source search (anonymous public Globus
index, no credentials), with **Claude Desktop doing the analysis** (no
apecx-side LLM needed). Sequence alignment (MAFFT), structural SASA (Docker +
PyMOL), Rhea/MUSCLE tools (Docker), and the internal-synthesis backend LLM
(Ollama) are opt-in unlocks. `apecx-setup verify` re-checks component health
and treats every component except the synonym dictionary as optional.

## Prerequisites

| Tool | Why |
|---|---|
| **Python ≥ 3.12** | `pyproject.toml` minimum. |
| **(Optional) Globus account + [Globus Connect Personal](https://www.globus.org/globus-connect-personal)** | Required ONLY for transferring VIOLIN + BV-BRC genomic datasets via `apecx-setup data`. The MCP server, synonym dictionary, and lookup tools work without Globus authentication — the dictionary auto-downloads anonymously from a public Globus HTTPS path on first launch. See "Globus data access" for the data-transfer setup. |
| **Homebrew (macOS) OR the ability to `curl \| sh` (Linux)** | `apecx-setup` uses these to install Ollama for you. Decline the prompt and install yourself if you'd rather. |

You will **NOT** need: Docker, Postgres, root/admin, GPU. The
control-plane backend autostarts as a child process and persists
state to SQLite under your CWD. **You also don't need to install
Ollama yourself** — `apecx-setup` handles it (asks first) unless
you prefer to use a remote OpenAI-compatible endpoint (vLLM,
OpenAI, hosted Anthropic-proxy), in which case set
`APECX_LLM_BASE_URL` and decline the install prompt.

## Globus data access (optional — for VIOLIN / BV-BRC datasets)

**Skip this section if you only need dictionary lookups, query tools, and
the synthesis pipeline.** The synonym dictionary downloads anonymously
on first MCP launch — no Globus credentials required.

Globus authentication is required ONLY when transferring the domain
genomic datasets via `apecx-setup data`. There are two ways to authenticate.

**Default — web-based login (thick client, no secret).** Recommended for
workstations:

```bash
apecx-globus-setup login          # opens a browser; log in with your
                                  # institutional Globus identity. No secret to
                                  # obtain or store; the token auto-refreshes.
```

This is the default — `apecx-setup` uses it with no extra configuration. A
built-in public native-client id ships with the tool (override with
`$APECX_GLOBUS_NATIVE_CLIENT_ID` to use your own native app).

**Option — confidential client (thin client, secret).** For headless installs,
CI, automation, or HPC, where no browser is available:

```bash
export APECX_GLOBUS_AUTH_MODE=client_credentials
apecx-globus-setup store --client-id <id> --client-secret <secret>
# or, for CI: export GLOBUS_COMPUTE_CLIENT_ID / GLOBUS_COMPUTE_CLIENT_SECRET
```

Either way, set the endpoints:

```bash
export APECX_GLOBUS_SOURCE_ENDPOINT_ID=<the APECx data collection UUID>   # ask the data steward
export APECX_GLOBUS_DEST_ENDPOINT_ID=<your Globus Connect Personal endpoint UUID>
```

**Datasets.** BV-BRC is on the public collection (always available). VIOLIN is
gated by the `apecx-project-all` Globus Group — if your identity isn't a member,
the install completes on BV-BRC alone and prints a loud warning telling you how
to request access; re-run `apecx-setup data` once granted. Full operator guide:
[`docs/globus_data_transfer.md`](docs/globus_data_transfer.md).

## First query

In Claude Desktop after restart, try:

> *Use the apecx tools to find all VIOLIN entries for entity "EEEV".*

Claude calls `resolve_entity` to canonicalize, then `query_vaccines`
/ `query_pathogens` with the canonical name. Other working prompts:

- *How many genome records by organism?* → `query_bvbrc_genomes`.
- *Compose a workflow that fetches BV-BRC genomes for VEEV and
  exports an HPC bundle.* → `start_workflow` → `show_diff` →
  `execute_workflow` → `export_hpc_bundle`.

## When something doesn't work

```bash
tail -50 ~/Library/Logs/Claude/mcp-server-apecx.log
```

The two pitfalls that cause silent failure in Claude Desktop (no
visible error — just an empty tool picker):

1. The `apecx` block placed OUTSIDE `mcpServers` in
   `claude_desktop_config.json`. Indent it as a child.
2. The `command` path pointing at the venv directory rather than
   the binary itself. Use the absolute path to `apecx-mcp` from
   `which apecx-mcp` (typically `~/.local/bin/apecx-mcp`).

For more on Claude Desktop wiring, env vars, per-tool inputs/outputs,
or troubleshooting: [`docs/mcp_integration.md`](docs/mcp_integration.md).

## Running as a backend server (HTTP MCP + control plane)

The default install spawns a **stdio** MCP server that Claude Desktop launches per session. To
instead run apecx as a **long-lived backend server** — reachable over HTTP by remote clients
(ChatGPT, a hosted agent, your own service) and/or synthesizing answers **headlessly** (no desktop
LLM) — run the two processes yourself on ports you choose. Everything below is flag- and
env-var-driven and was reproduced end-to-end on non-default ports.

### The two processes

| Process | Command | Default port | Role |
|---|---|---|---|
| **Control plane** (`apecx-cp`) | `apecx-cp serve` | **8000** | run-store + state backend (SQLite by default; Postgres optional). The MCP server health-checks it. |
| **MCP server** (`apecx-mcp`) | `apecx-mcp --transport streamable-http` | **8000** | the FastMCP tool surface over HTTP at `POST /mcp`. |

> ⚠️ **Both default to 8000 — you MUST give them distinct ports** (the `apecx-mcp` help calls this
> out explicitly). Below the control plane keeps 8000 and the MCP server takes 8001.

### Minimal bring-up (verified)

```bash
# 1. Control plane on 8000. SQLite state — no Docker/Postgres needed.
#    APECX_CP_DB_URL must be a SYNC sqlite URL (sqlite:///…), NOT sqlite+aiosqlite:// —
#    the alembic migration uses the sync driver; the runtime converts to async itself.
APECX_CP_DB_URL="sqlite:////var/lib/apecx/cp.db" \
  apecx-cp serve --host 127.0.0.1 --port 8000 &
curl -s http://127.0.0.1:8000/healthz          # -> {"status":"ok","phase":"scaffold"}

# 2. MCP server over HTTP on a DISTINCT port, headless synthesis (agent locus),
#    pointed at the control plane you just started.
APECX_CONTROL_PLANE_URL=http://127.0.0.1:8000 \
  apecx-mcp --transport streamable-http --host 0.0.0.0 --port 8001 --locus agent
# -> "Control Plane at http://127.0.0.1:8000 reachable" ; "execution_locus=agent" ;
#    "Uvicorn running on http://0.0.0.0:8001"  — MCP endpoint at http://<host>:8001/mcp
```

Use `--host 0.0.0.0` to accept remote connections (`127.0.0.1` is local-only). Point your MCP
client at `http://<host>:8001/mcp`. For ChatGPT specifically, `apecx-setup chatgpt` writes the
matching config.

### Non-default ports — the knobs

| What | Flag | Env var | Default |
|---|---|---|---|
| MCP HTTP bind host | `--host` | `APECX_MCP_HOST` | FastMCP default (`127.0.0.1`) |
| MCP HTTP port | `--port` | `APECX_MCP_PORT` | `8000` |
| Control-plane host / port | `--host` / `--port` | — | `8000` |
| Control-plane URL the MCP server checks | — | `APECX_CONTROL_PLANE_URL` | `http://localhost:8000` |
| Rhea MCP probe | — | `RHEA_MCP_URL` | `http://localhost:3001/mcp/` |

Whenever you move the control plane off 8000, set `APECX_CONTROL_PLANE_URL` to match, or the MCP
server's startup health check won't find it.

### Headless synthesis (the `agent` locus) + the backend LLM

`--locus agent` makes the apecx server synthesize answers itself instead of returning assembled
evidence for a desktop LLM (the default `desktop` locus). The agent locus needs an
OpenAI-compatible LLM backend — **there is no remote default**, so it is off until you set one:

```bash
export APECX_LLM_BASE_URL=http://localhost:11434/v1   # Ollama default; or vLLM / OpenAI / a proxy
export APECX_LLM_MODEL=nemotron-3-nano:4b
export APECX_LLM_API_KEY=EMPTY                          # for keyless local servers
# optional tuning: APECX_LLM_TEMPERATURE, APECX_LLM_MAX_TOKENS, APECX_LLM_TIMEOUT,
#                  APECX_LLM_MAX_RETRIES, APECX_LLM_MAX_VALIDATION_RETRIES
```

In `desktop` locus these are unused — the calling client is the synthesizer.

### Control-plane database

`apecx-cp` resolves its DB in order: `APECX_CP_DB_URL` → `APECX_CP_POSTGRES_URL` → a SQLite default
(`sqlite:///$APECX_CP_HOME/cp.db`). For SQLite pass a **sync** URL (`sqlite:///abs/path.db`); for
Postgres set `APECX_CP_POSTGRES_URL`. `apecx-cp teardown` stops a locally-managed Postgres
container (a no-op for SQLite / bring-your-own).

### Trimming the startup for a lean server

The MCP server boots a control-plane health check, the infra orchestrator, and a lazy dictionary
build. Skip what you run separately:

| Env var | Effect |
|---|---|
| `APECX_MCP_SKIP_HEALTHCHECK=1` | skip the control-plane reachability check at startup |
| `APECX_MCP_AUTOSTART_BACKEND=0` | do NOT auto-spawn the control plane (you run `apecx-cp` yourself) |
| `APECX_MCP_AUTOSTART_INFRA=0` | run the infra orchestrator in probe-only mode (no Docker / Rhea autostart) |
| `APECX_SKIP_DICT_BUILD=1` | skip the lazy dictionary build (assumes the SQLite already exists) |

### Advanced capabilities

**Optional Python extras** (`uv tool install 'apecx-mcp-integration[<extra>]'`, or
`pip install -e '.[<extra>]'` from a checkout):

| Extra | Unlocks |
|---|---|
| `viz` | the sequence-conservation **PNG/PDF** figures (matplotlib; degrades to a text track without it) |
| `rag` | the **domain-RAG** synthesis branch (FAISS + sentence-transformers; build the index with `apecx-setup rag`) |
| `hpc` | the **Globus Compute / PBS** HPC execution path (globus-compute-sdk, globus-sdk, keyring) |
| `academy` | the **Academy** distributed-agent runtime |

**Data, dictionary, and catalog overrides:**

| Env var | Purpose |
|---|---|
| `APECX_DATA_ROOT` | path to the VIOLIN/BV-BRC data dir → enables the direct DB-lookup tools |
| `APECX_SYNONYM_DICT_PATH` | override the synonym-dictionary SQLite path (default: the one `apecx-setup` provisions) |
| `APECX_MCP_WORKFLOW_CATALOG` | override the packaged catalog of MCP-exposed workflows (path to YAML) |

The full environment-variable table lives in
[`docs/apecx_mcp_infrastructure.md`](docs/apecx_mcp_infrastructure.md) §3.

## Deeper pointers

| Doc | When to read |
|---|---|
| [`docs/QUICKSTART.md`](docs/QUICKSTART.md) | Step-by-step walkthrough with verification commands at each stage. |
| [`docs/mcp_integration.md`](docs/mcp_integration.md) | Per-tool reference, env-var matrix, advanced troubleshooting. |
| [`docs/tutorial/`](docs/tutorial/README.md) | Multi-chapter walkthrough from install to reproducible run. |
| [`INSTALL.md`](INSTALL.md) | Alternative installers (pipx, pip --user, bundled script), update / uninstall flows. |
| [`docs/architecture.md`](docs/architecture.md) | Canonical end-to-end architecture map. |
| [`docs/CONTRACTS.md`](docs/CONTRACTS.md) | Design contracts cited from source docstrings (anchored sections). |

## Required sibling repos (pulled automatically by `uv tool install`)

- [`AlexandrNP/nanobrain` (academy-integration)](https://github.com/AlexandrNP/nanobrain/tree/academy-integration)
  — framework: Steps, Workflows, Agents, Triggers, Links, Executors.
- [`abought/apecx-harvesters`](https://github.com/abought/apecx-harvesters)
  — DataCite-shaped publication metadata loaders.
