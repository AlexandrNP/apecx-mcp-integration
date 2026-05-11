# apecx-mcp — Quick start (fresh machine to first query)

Single-page walkthrough. ~15 minutes including downloads.

Assumes **macOS or Linux**. Windows works but the command syntax
differs; see `mcp_integration.md` for Windows specifics.

At the end you'll have:
- An OpenAI-compatible LLM running locally (Ollama + mistral-nemo).
- `apecx-mcp` and `apecx-setup` on your `PATH`.
- Domain datasets unpacked in `~/.apecx/data` (~15 MB).
- A patched `claude_desktop_config.json` with the `apecx` MCP server
  registered.
- 23 apecx tools visible in Claude Desktop's tool picker after a
  relaunch.

You will NOT need: Docker, Postgres, root/admin, GPU.

For depth on any step, see `INSTALL.md` (alt installers) and
`mcp_integration.md` (env vars, troubleshooting, per-tool shapes).

---

## Step 0 — Prerequisites

```bash
python3 --version    # need 3.12+
uv --version         # any recent — install with: curl -LsSf https://astral.sh/uv/install.sh | sh
gh auth status       # authenticated to GitHub — required for the private data download
```

`gh` is required because `apecx-setup` downloads domain data from a
private GitHub release; auth piggybacks on `gh`'s session, no PAT
setup needed.

---

## Step 1 — (optional, recommended) pre-install Ollama

You can skip this step entirely: `apecx-setup` (Step 3) offers to
install Ollama for you with a y/N prompt before running `brew
install` (macOS) or the official `curl | sh` installer (Linux).
This pre-install path is for users who'd rather install it
themselves OR want to be sure the daemon is fully warmed up
before `apecx-setup` runs:

```bash
brew install ollama                                  # macOS
# Linux: curl -fsSL https://ollama.ai/install.sh | sh
ollama serve &
ollama pull mistral-nemo:latest
curl -fsS http://localhost:11434/api/tags | head -1  # verify
```

**Any OpenAI-compatible endpoint works** (vLLM, llama.cpp's server,
OpenAI proper, Anthropic via a proxy). Set `APECX_LLM_BASE_URL` +
`APECX_LLM_MODEL` before running `apecx-setup` and decline the
Ollama install prompt.

**Why no Docker?** apecx-mcp's Control Plane backend autostarts as a
child process and persists state to SQLite. Docker is only useful for
swapping to managed Postgres for shared/HA deployments — out of
scope for quick-start.

---

## Step 2 — Install apecx-mcp (~1–2 min)

```bash
uv tool install --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git
```

Pulls apecx-mcp + the two sibling repos (`nanobrain`, `apecx-harvesters`)
from GitHub and installs three console scripts under `~/.local/bin/`:
`apecx-mcp`, `apecx-cp`, `apecx-setup`.

Verify:
```bash
which apecx-mcp apecx-setup
```

If `which` returns nothing, add `~/.local/bin` to `PATH` in your
shell rc file.

---

## Step 3 — Run apecx-setup (~30 sec)

```bash
apecx-setup
```

Walks you through (each step prints what it will do; consent
prompts gate any system-touching action):

1. **`data`** — confirms the data directory (default
   `~/.apecx/data`), downloads the domain tarball via your `gh`
   session (~1.5 MB compressed, ~15 MB extracted), AND patches
   `claude_desktop_config.json` with the `apecx` MCP server block
   (prompts for the three LLM env vars on first install).
2. **`infra`** — starts Postgres + Redis Docker containers if
   Docker is available; skipped if not (SQLite is the default
   backend, so this is purely optional).
3. **`llm`** — if `ollama` is not on PATH, prompts to install via
   `brew install ollama` (macOS) or `curl -fsSL
   https://ollama.ai/install.sh | sh` (Linux). The exact command
   is printed BEFORE the prompt. If the CLI is present but the
   daemon isn't responding, prompts to start it in the background.
   Pulls `mistral-nemo:latest` (or whatever `APECX_LLM_MODEL` is
   set to) if it's not already there.
4. **`rag`** — builds the FAISS index from the downloaded data.
5. **`verify`** — health-checks every component; prints a summary
   table.

---

## Step 4 — Restart Claude Desktop

**Fully quit** (Cmd-Q on macOS — closing the window doesn't restart
the MCP subprocesses) and reopen. After 2–5 seconds, the apecx
tools appear in the tool picker.

If they don't appear:
```bash
tail -50 ~/Library/Logs/Claude/mcp-server-apecx.log
```

Common failure banners (each clearly indicates the cause):
- `APECx data tools DISABLED` → `APECX_DATA_ROOT` missing in the
  Claude Desktop config. Re-run `apecx-setup`.
- `Control Plane … unreachable AND APECX_MCP_AUTOSTART_BACKEND=0`
  → autostart was explicitly disabled. Run `apecx-cp serve` in a
  separate terminal, or remove the disable from the env block.

---

## Step 5 — First query

In Claude Desktop, try:

> *"Use the apecx tools to find all entries in the domain database
> for a given entity name."*

Claude calls `resolve_entity` (canonicalize) then
`query_vaccines` / `query_pathogens` with the canonical name.
Other working starting prompts:
- *"How many genome records by organism?"* → `query_bvbrc_genomes`.
- *"Show me the gene-target relationships."* → `get_vaccine_pathogen_genes`.

Composer-orchestrated tools (`start_workflow`, `show_diff`,
`execute_workflow`, approval/HPC tools) require an LLM that can
reason about workflow YAML — `mistral-nemo` works; small or heavily
quantized models do not.

For the full tool inventory + per-tool input/output shapes, see
`mcp_integration.md`.

---

## Reconfiguration

```bash
apecx-setup --reconfigure-llm   # change LLM endpoint; preserves other env
apecx-setup                     # re-download data (prompts before overwrite)
uv tool install --reinstall --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git   # update
```

---

## Honest limitations

- **`APECX_LLM_API_KEY` is plaintext** in `claude_desktop_config.json`
  if you use a paid cloud LLM. Operator-managed; no built-in vault
  integration.
- **Private data repo** — `apecx-data` requires `gh auth` access to
  `AlexandrNP/apecx-data`. Outside the org you can't download the
  dataset.
- **First-launch latency** — apecx-mcp autostarts the Control Plane
  backend on first MCP call (~2–5 s the first time; sub-second
  thereafter).
- **Composer is model-sensitive.** mistral-nemo and larger work;
  small / heavily-quantized models hallucinate workflow YAML in
  ways that break execution.
