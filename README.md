# apecx-mcp-integration

MCP server for the APECx scientific platform. Exposes **23
scientist-facing tools** to Claude Desktop (or any MCP client): query
VIOLIN + BV-BRC + Globus Search databases, compose workflows from
natural-language descriptions, review diffs, execute locally, export
to HPC.

> **License: All Rights Reserved (proprietary, source-available).**
> Public for transparency; reuse, redistribution, and derivative
> works require explicit written permission. See [`LICENSE`](LICENSE).

## Install in three commands

```bash
# 1. install uv (skip if you already have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. install apecx-mcp + its two sibling repos in one shot
uv tool install --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git

# 3. configure Claude Desktop, download datasets, restart Claude
apecx-setup
```

`apecx-setup` is interactive: it confirms the data directory,
downloads ~15 MB of domain CSVs via your `gh` session, **offers to
install Ollama if missing** (Homebrew on macOS / the official
install script on Linux — every command printed before a y/N
prompt), starts the daemon, pulls the configured model
(`mistral-nemo:latest` by default), and patches
`claude_desktop_config.json` with the right paths and LLM env vars.

After it finishes, **fully quit Claude Desktop** (Cmd-Q on macOS —
closing the window is not enough) and reopen. The 23 apecx tools
appear in the tool picker after 2–5 seconds.

## Prerequisites

| Tool | Why |
|---|---|
| **Python ≥ 3.12** | `pyproject.toml` minimum. |
| **`gh` (authenticated)** | `apecx-setup` pulls domain data from a private GitHub release; auth piggybacks on `gh`'s session — no PAT setup. |
| **Homebrew (macOS) OR the ability to `curl \| sh` (Linux)** | `apecx-setup` uses these to install Ollama for you. Decline the prompt and install yourself if you'd rather. |

You will **NOT** need: Docker, Postgres, root/admin, GPU. The
control-plane backend autostarts as a child process and persists
state to SQLite under your CWD. **You also don't need to install
Ollama yourself** — `apecx-setup` handles it (asks first) unless
you prefer to use a remote OpenAI-compatible endpoint (vLLM,
OpenAI, hosted Anthropic-proxy), in which case set
`APECX_LLM_BASE_URL` and decline the install prompt.

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
