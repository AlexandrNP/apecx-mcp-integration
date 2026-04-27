# apecx-mcp-integration

MCP server for the APECx scientific platform. Exposes 11 scientist-facing
tools to Claude Desktop (or any MCP client) over stdio: compose a
workflow from a natural-language description, review the diff, execute
locally, optionally export to HPC.

> **License: All Rights Reserved (proprietary, source-available).**
> See [`LICENSE`](LICENSE). Public for transparency; reuse, redistribution,
> and derivative works require explicit written permission.

## Install

```bash
uv tool install --python 3.12 git+https://github.com/AlexandrNP/apecx-mcp-integration.git@day2-rag-synthesis-agent
```

That single command pulls this repo + the two required sibling repos
([`nanobrain @ academy-integration`](https://github.com/AlexandrNP/nanobrain/tree/academy-integration) and
[`apecx-harvesters @ main`](https://github.com/abought/apecx-harvesters)) directly from git, builds them, and exposes
the `apecx-mcp` and `apecx-cp` binaries on your `PATH` (typically `~/.local/bin/`).
No manual clones, no Docker required (SQLite default).

Don't have `uv`?

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Prerequisites: Python ≥ 3.12 and Ollama running on `localhost:11434`
with a model pulled (default: `mistral-nemo:latest`).

Alternative installers (`pipx`, `pip --user`) and the full troubleshooting
flow are in [`INSTALL.md`](INSTALL.md).

## Connect to Claude Desktop

Open the Claude Desktop config:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

Add the `apecx` block as a child of `mcpServers` (not a sibling — Claude
Desktop only scans inside `mcpServers`). Use the **absolute path** to
`apecx-mcp` from `which apecx-mcp` (tilde and `$PATH` are not expanded
by Claude Desktop's spawner):

```jsonc
{
  "mcpServers": {
    "apecx": {
      "command": "/Users/<you>/.local/bin/apecx-mcp",
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

Fully quit and relaunch Claude Desktop. The 11 apecx tools appear in the
tool picker. The first launch takes ~5–15 s while the Control Plane
backend autostarts and runs SQLite migrations; subsequent launches are
< 1 s.

If tools don't appear, tail the Claude Desktop log:

```bash
# macOS
tail -f ~/Library/Logs/Claude/mcp-server-apecx.log
```

Two pitfalls cause silent failure (Claude Desktop shows no error):

1. The `apecx` block placed OUTSIDE `mcpServers`.
2. The `command` path pointing at the venv directory rather than the
   binary itself.

Both are documented in [`docs/mcp_integration.md`](docs/mcp_integration.md).

## Pointers

- [`INSTALL.md`](INSTALL.md) — install one-liner + alternatives + update / uninstall flow + troubleshooting.
- [`docs/mcp_integration.md`](docs/mcp_integration.md) — full MCP integration reference: per-tool input/output, env vars, architecture, security notes.
- [`docs/tutorial/`](docs/tutorial/README.md) — 5-chapter walkthrough from clean laptop to reproducible run.
- [`LICENSE`](LICENSE) — proprietary, source-available terms.
- `../architectural_plan.md` — architectural source of truth (workspace-local).
- `../implementation_plan.md` — task table + scoreboard (workspace-local).
- [`AlexandrNP/nanobrain` (academy-integration)](https://github.com/AlexandrNP/nanobrain/tree/academy-integration) — required sibling: framework (Steps, Workflows, Agents, Triggers, Links, Executors).
- [`abought/apecx-harvesters`](https://github.com/abought/apecx-harvesters) — required sibling: DataCite-shaped publication metadata loaders.
