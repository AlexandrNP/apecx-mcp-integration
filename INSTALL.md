# Install — apecx-mcp

## The one-liner

```bash
uv tool install --python 3.12 git+https://github.com/AlexandrNP/apecx-mcp-integration.git@day2-rag-synthesis-agent
```

That's the entire install. It pulls `apecx-mcp-integration` plus the
two sibling repos (`nanobrain @ academy-integration`,
`apecx-harvesters @ main`) from git in one shot, builds them, and
exposes `apecx-mcp` + `apecx-cp` on your `PATH`.

Don't have `uv` yet? Add it first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

After install, find the binary path with `which apecx-mcp` and paste
it into Claude Desktop (snippet below).

## Prerequisites

- **Python ≥ 3.12** on your `PATH`.
- **Ollama** running on `localhost:11434` with `mistral-nemo:latest`
  pulled. (Or any other OpenAI-compatible endpoint — see
  `docs/mcp_integration.md` for alternatives.)
- **Docker is OPTIONAL.** The autostart backend uses SQLite by
  default. Set `APECX_CP_POSTGRES_URL` only if you want Postgres.

## Alternative installers

If you don't want to use `uv`:

```bash
# Option B: pipx
pipx install git+https://github.com/AlexandrNP/apecx-mcp-integration.git@day2-rag-synthesis-agent

# Option C: pip --user (does NOT isolate; use only as a last resort)
python3.12 -m pip install --user git+https://github.com/AlexandrNP/apecx-mcp-integration.git@day2-rag-synthesis-agent
```

After install, verify:

```bash
which apecx-mcp                # /Users/<you>/.local/bin/apecx-mcp (uv) or similar
apecx-mcp --help               # FastMCP help banner
```

## Or: the bundled installer script

Equivalent to running the one-liner yourself, plus it picks the
right installer for your machine and prints the Claude Desktop
config block ready to paste:

```bash
curl -fsSL https://raw.githubusercontent.com/AlexandrNP/apecx-mcp-integration/day2-rag-synthesis-agent/scripts/install.sh | bash
```

## Connect Claude Desktop

Edit your Claude Desktop config:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

If the file already has an `mcpServers` key, add the `apecx` entry as
a **child** of that key (not a sibling — Claude Desktop only scans
inside `mcpServers`).

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

**Use the absolute path** to the binary (find it with
`which apecx-mcp`). Tilde and `$PATH` are not expanded by Claude
Desktop's spawner.

Fully quit and relaunch Claude Desktop. The 20 apecx tools appear
in the tool picker.

## What just happened

1. The installer pulled `apecx-mcp-integration` from GitHub.
2. It also pulled `nanobrain @ academy-integration` and
   `apecx-harvesters @ main` (declared as git dependencies in
   `pyproject.toml`).
3. It built and installed both into an isolated environment, exposing
   `apecx-mcp` and `apecx-cp` on your PATH.
4. When Claude Desktop spawns `apecx-mcp`, it probes the local
   Control Plane on `localhost:8000`. If absent, it spawns
   `apecx-cp serve` as a child process, polls `/healthz` until ready
   (~5–15 s on a cold first run while SQLite migrations run), then
   serves MCP tools to Claude.
5. On exit, the autostarted backend is terminated cleanly.

State (runs, approvals, artifacts, provenance events) lives in
`apecx_cp.db` in whatever directory Claude Desktop spawned the
binary from.

## Updating

```bash
uv tool upgrade apecx-integration --reinstall   # uv
pipx upgrade apecx-integration --force          # pipx
```

The reinstall pulls the latest commits from the same branch you
installed from. Fully quit and relaunch Claude Desktop.

## Uninstalling

```bash
uv tool uninstall apecx-integration             # uv
pipx uninstall apecx-integration                # pipx
```

Then remove the `apecx` block from
`claude_desktop_config.json` and relaunch Claude Desktop.

## Troubleshooting

### Tools don't appear in Claude Desktop

Tail the log:

```bash
# macOS
tail -f ~/Library/Logs/Claude/mcp-server-apecx.log
```

Two pitfalls cause empty tool lists:

1. The `apecx` block is OUTSIDE `mcpServers` (sibling, not child).
2. The `command` path doesn't point at the installed binary. Run
   `which apecx-mcp` and use that exact path.

### `apecx-cp serve` startup fails with "alembic.ini not found"

You're on a pre-2026-04-27 install. Reinstall with `--reinstall` /
`--force` to pick up the bundled migrations:

```bash
uv tool install --reinstall git+https://github.com/AlexandrNP/apecx-mcp-integration.git@day2-rag-synthesis-agent
```

### Ollama unreachable

If `APECX_LLM_BASE_URL` points at Ollama and the daemon isn't
running, the composer's first invocation fails with a connection
error. Confirm Ollama is up: `ollama ps` (should list at least one
running model) or `curl -s http://localhost:11434/api/tags`.

## Developer setup

Skip this section if you are only running `apecx-mcp` from a
released install. This is for contributors editing the source.

### One-time per checkout: install pre-commit hooks

`.pre-commit-config.yaml` declares ruff + ruff-format hooks but they
are inert until you wire them into the local `.git/hooks/`. Without
this step, lint findings (import order, unused imports, formatting)
silently land in commits — even though the same checks fail in CI.

```bash
.venv/bin/pip install pre-commit       # if not already present
.venv/bin/pre-commit install            # writes .git/hooks/pre-commit
```

### Per-worktree gotcha

`git worktree add` creates a new working tree but **does not share
hooks** with the main checkout — each worktree has its own
`worktrees/<name>/hooks/` directory under the parent `.git/`, and
that directory starts empty. After creating a worktree:

```bash
git worktree add ../wt-my-task -b my-task
cd ../wt-my-task
.venv/bin/pre-commit install            # required separately
```

If you forget, the symptom is "I ran ruff manually after the fact
and found violations that should have been blocked at commit time."
That happened during the 2026-04-27 MCP discovery + DB-tools rollout
(commits e3372a2, 9e26e82) and required a follow-up cleanup commit
(d184f5b).

### Running the test suite

The canonical runner sets `PYTHONPATH=src`, uses `.venv/bin/python`,
and runs from the repo root — use it instead of `pytest` directly:

```bash
scripts/run_tests.sh                    # full suite
scripts/run_tests.sh tests/unit         # subset
APECX_DATA_ROOT=/path/to/data \
  scripts/run_tests.sh tests/integration/test_db_tools_real_data.py
```

Database integration tests auto-skip when `APECX_DATA_ROOT` (or
`APECX_ROOT/data`) doesn't contain `violin/Vaccine_Information.csv`.

## Reference

- `docs/mcp_integration.md` — full per-tool reference, env vars,
  architecture, security notes
- `pyproject.toml` — git dependencies on nanobrain + apecx-harvesters
- `scripts/install.sh` — the one-shot installer this doc describes
- `.pre-commit-config.yaml` — ruff + ruff-format hooks (developer
  only; not required for end users running the released binary)
