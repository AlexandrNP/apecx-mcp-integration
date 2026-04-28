# apecx-mcp — Quick start (fresh machine to first query)

Single-page walkthrough.  ~15–20 minutes including downloads.

This guide assumes **macOS or Linux**.  Windows works but the
command syntax differs; see `docs/mcp_integration.md` for Windows
specifics.

For depth on any step, see:
- `INSTALL.md` — alternative installers, post-install verification
- `docs/mcp_integration.md` — env-var reference, troubleshooting,
  per-tool input/output shapes

---

## What you'll have at the end

- An OpenAI-compatible LLM running locally (Ollama + mistral-nemo).
- `apecx-mcp` and `apecx-setup` on your `PATH`.
- VIOLIN + BV-BRC datasets unpacked in `~/.apecx/data` (~15 MB).
- A patched `claude_desktop_config.json` with the `apecx` MCP server
  registered.
- The 20 apecx tools visible in Claude Desktop's tool picker after
  a relaunch.

You will NOT need: Docker, Postgres, root/admin, GPU.

---

## Step 0 — Prerequisites (verify, install only what's missing)

```bash
python3 --version    # need 3.12+
uv --version         # any recent
gh auth status       # authenticated to GitHub
```

If `python3 --version` reports < 3.12:
```bash
brew install python@3.12          # macOS
sudo apt install python3.12       # Debian/Ubuntu
```

If `uv` is missing:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# Restart the shell or:  source ~/.cargo/env
```

If `gh` is missing or unauthenticated:
```bash
brew install gh                   # macOS
sudo apt install gh               # Debian/Ubuntu
gh auth login                     # follow the browser prompt
```

`gh` is required because `apecx-setup` downloads VIOLIN + BV-BRC
data from a private GitHub release; auth piggybacks on `gh`'s
session, no PAT setup needed.

---

## Step 1 — Install Ollama and pull a model (~3 min, ~7 GB download)

```bash
brew install ollama                      # macOS
# or:  curl -fsSL https://ollama.ai/install.sh | sh   (Linux)

# Start the daemon (it stays running in the background).
ollama serve &

# Pull the recommended model.  ~7 GB; this is the slowest step.
ollama pull mistral-nemo:latest

# Verify reachability.
curl -fsS http://localhost:11434/api/tags | python3 -m json.tool | head -5
```

Expected: a JSON object listing `mistral-nemo:latest` under `models`.

**Don't want Ollama / mistral-nemo?** Any OpenAI-compatible endpoint
works (vLLM, llama.cpp's server, OpenAI proper, Anthropic via a
proxy).  Just pick a model and note the base URL — you'll enter
both in Step 4.

**Skipping Docker?** Yes, deliberately.  apecx-mcp's Control Plane
backend autostarts as a child process and persists state to SQLite
in your CWD.  Docker is only useful if you want to swap to a
managed Postgres for shared/HA deployments — out-of-scope for
quick-start.

---

## Step 2 — Install apecx-mcp (~1–2 min)

```bash
uv tool install --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git
```

This pulls apecx-mcp + its two sibling repos (`nanobrain`,
`apecx-harvesters`) from GitHub, builds them, and installs the
three console scripts (`apecx-mcp`, `apecx-cp`, `apecx-setup`)
under `~/.local/bin/`.

Verify:
```bash
which apecx-mcp           # /Users/<you>/.local/bin/apecx-mcp
which apecx-setup         # /Users/<you>/.local/bin/apecx-setup
apecx-setup --help        # argparse banner with --reconfigure-llm
```

If `which` returns nothing: add `~/.local/bin` to your PATH:
```bash
export PATH="$HOME/.local/bin:$PATH"        # add to ~/.zshrc or ~/.bashrc
```

---

## Step 3 — Run apecx-setup (~30 sec)

```bash
apecx-setup
```

Walks you through:
1. **Data directory** (default `~/.apecx/data`) — press Enter to accept.
2. **Download** the VIOLIN + BV-BRC tarball from the private repo
   via your `gh` session (~1.5 MB compressed, ~15 MB extracted).
3. **Claude Desktop config** — auto-detects the standard config
   path; confirms before writing.  On first install (no `apecx`
   block in config), shows the **full proposed JSON block** and
   prompts for the three LLM env vars (URL, model, API key).

   Press Enter for each LLM prompt to accept the Ollama defaults
   from Step 1.  If you're using a different endpoint, paste the
   URL / model / key you noted at the end of Step 1.

After the run completes, your `claude_desktop_config.json` will
have an `apecx` server entry.  Verify with:
```bash
python3 -m json.tool < \
  ~/Library/Application\ Support/Claude/claude_desktop_config.json \
  | grep -A 10 '"apecx"'
```

Expected: a block showing `command`, `args`, and `env` with
`APECX_DATA_ROOT`, `APECX_LLM_BASE_URL`, etc.

---

## Step 4 — Restart Claude Desktop

**Fully quit** (Cmd-Q on macOS — closing the window doesn't restart
the MCP subprocesses) and reopen.

After 2–5 seconds, the apecx tools appear in the tool picker.

If they don't appear:
```bash
tail -50 ~/Library/Logs/Claude/mcp-server-apecx.log
```

The most common failure modes (each prints a clear banner):
- `APECx data tools DISABLED` → `APECX_DATA_ROOT` env var missing
  in the Claude Desktop config.  Re-run `apecx-setup` and accept
  the config-update prompt.
- `Control Plane … unreachable AND APECX_MCP_AUTOSTART_BACKEND=0`
  → autostart was explicitly disabled.  Either run `apecx-cp serve`
  in a separate terminal, or remove the disable from the env block.
- `gh: unrecognized command` → step 0 wasn't completed.

---

## Step 5 — First query

In Claude Desktop, with the apecx tools enabled, try:

> *"Use the apecx tools to find all vaccines targeting Eastern Equine
> Encephalitis Virus."*

Claude should call `resolve_entity` (to canonicalize the pathogen
name), then `query_vaccines` with `pathogen="EEEV"` (or similar).
The response should list ~5–10 vaccines from VIOLIN.

Other things you can ask:
- *"How many alphavirus genomes from BV-BRC do we have, broken
  down by host species?"* — exercises `query_bvbrc_genomes`.
- *"Show me the gene-vaccine-pathogen relationships for chikungunya."*
  — exercises `get_vaccine_pathogen_genes`.
- *"What's in our database statistically?"* — exercises
  `database_statistics`.

The composer-orchestrated tools (`start_workflow`, `show_diff`,
`execute_workflow`, the approval/HPC tools) require an LLM that
can reason about workflow YAML.  `mistral-nemo` works for the
composer; smaller / quantized models are unreliable.

---

## What's actually exposed (full inventory)

The `apecx-mcp` server exposes 20 tools.  In case Claude
Desktop's UI summarizes them by category, the complete list is:

| Module | Tool |
|---|---|
| `tools/workflows.py` | `start_workflow`, `show_diff`, `execute_workflow` |
| `tools/discovery.py` | `list_workflows`, `describe_workflow` |
| `tools/database_tools.py` | `query_vaccines`, `query_pathogens`, `query_genes`, `query_bvbrc_genomes`, `get_vaccine_pathogen_genes`, `resolve_entity`, `database_statistics` |
| `tools/approvals.py` | `list_pending_approvals`, `approve`, `reject`, `correct` |
| `tools/hpc.py` | `estimate_cost`, `confirm_allocation`, `export_hpc_bundle`, `ingest_hpc_bundle` |

To verify directly (independent of Claude Desktop's display):
```bash
grep '"name":' ~/Library/Logs/Claude/mcp-server-apecx.log \
  | grep -oE '"name":"[^"]+"' | sort -u | head -25
```

Should print 20 distinct names.  If you see fewer, the apecx-mcp
server failed to register some — see the log for the actual
exception.

---

## Reconfiguration & maintenance

**Change LLM endpoint** (e.g., switch from Ollama to OpenAI):
```bash
apecx-setup --reconfigure-llm
```
Prompts prefill with current values; touches only `APECX_LLM_*`
env vars; preserves `APECX_DATA_ROOT`, command, args, and any
unrelated MCP servers in the config.  See PR #9.

**Re-download / refresh data**:
```bash
apecx-setup        # Overwrites ~/.apecx/data after a confirmation prompt.
```

**Update apecx-mcp** to the latest commit on `main`:
```bash
uv tool install --reinstall --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git
```

---

## Honest limitations

- **`APECX_LLM_API_KEY` is plaintext** in `claude_desktop_config.json`
  if you use a paid cloud LLM.  See "Secrets handling" in
  `docs/mcp_integration.md`.
- **Private data repo** — `apecx-data` requires `gh auth` access
  to `AlexandrNP/apecx-data`.  Anyone outside the org can't
  download the dataset.
- **First-launch latency** — apecx-mcp autostarts the Control
  Plane backend on first MCP call.  ~2–5 seconds the first time;
  subsequent calls are sub-second.
- **Composer is sensitive to model choice.** mistral-nemo and
  larger models work; small / heavily-quantized models hallucinate
  workflow YAML in ways that break execution.
