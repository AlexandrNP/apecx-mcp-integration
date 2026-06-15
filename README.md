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
