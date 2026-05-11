# Install — alternatives + maintenance

The canonical install lives in [`README.md`](README.md): three
commands and `apecx-setup`. This file covers the alternatives + the
update / uninstall flows.

## Install alternatives

```bash
# Option A — uv (canonical; fastest)
uv tool install --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git

# Option B — pipx
pipx install \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git

# Option C — pip --user (does NOT isolate; last resort)
python3.12 -m pip install --user \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git

# Option D — bundled installer script (picks the best installer
#            for your machine + prints the Claude Desktop block)
curl -fsSL \
  https://raw.githubusercontent.com/AlexandrNP/apecx-mcp-integration/main/scripts/install.sh \
  | bash
```

All four pull the two sibling repos
(`nanobrain @ academy-integration`, `apecx-harvesters @ main`) from
git automatically — no manual clones.

## Verify

```bash
which apecx-mcp           # /Users/<you>/.local/bin/apecx-mcp (uv default)
which apecx-setup
apecx-mcp --help          # FastMCP help banner
apecx-setup --help        # argparse banner with --reconfigure-llm
```

If `which` returns nothing, add `~/.local/bin` (or your installer's
shim dir) to `PATH` in your shell rc.

## Developer mode (editable)

```bash
git clone --recurse-submodules \
  https://github.com/AlexandrNP/apecx-mcp-integration.git
cd apecx-mcp-integration
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
```

The `[dev]` extra includes pytest + ruff + pre-commit. Run tests via
`scripts/run_tests.sh` (sets `PYTHONPATH=src` + uses `.venv/bin/python`
explicitly — anaconda Python on PATH bites otherwise).

## Update

```bash
uv tool install --reinstall --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git
```

Or, if installed with pipx:

```bash
pipx upgrade apecx-mcp-integration
```

`apecx-setup --reconfigure-llm` rewrites just the LLM env vars in
`claude_desktop_config.json` (preserves `APECX_DATA_ROOT`, command,
args, and any unrelated MCP servers).

## Uninstall

```bash
uv tool uninstall apecx-mcp-integration       # uv
pipx uninstall apecx-mcp-integration          # pipx
pip uninstall apecx-mcp-integration           # pip --user
```

Manual cleanup (only if needed):
- Remove the `apecx` block from `claude_desktop_config.json`.
- Delete `~/.apecx/` (data + control-plane SQLite) if you don't want
  to keep it for a future reinstall.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `apecx-mcp: command not found` | `~/.local/bin` not on PATH. Add to shell rc. |
| `ModuleNotFoundError: No module named 'nanobrain'` from a test | Wrong-Python pitfall. Use `.venv/bin/python` explicitly (see [`scripts/run_tests.sh`](scripts/run_tests.sh)). |
| `gh: unrecognized command` during `apecx-setup` | `gh` not installed. `brew install gh && gh auth login`. |
| Tools missing in Claude Desktop after restart | See [`README.md`](README.md) — "two pitfalls" + tail the log. |

Deeper per-tool / per-env-var reference:
[`docs/mcp_integration.md`](docs/mcp_integration.md).
