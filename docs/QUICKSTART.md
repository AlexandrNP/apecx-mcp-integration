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
```

You also need, for the data transfer (Step 3):
- A **Globus account** (any institutional or Globus ID — free).
- **[Globus Connect Personal](https://www.globus.org/globus-connect-personal)**
  installed + running, to give you a local destination endpoint UUID.

(`gh` is NOT needed — the GitHub-release data download was retired; data now
comes over Globus.)

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

## Step 3 — Configure Globus (one command)

Data is transferred over Globus. Run the setup with **no arguments** — it does
the whole thing: web-based login (no secret), applies the default source
directories silently, and records your destination endpoint (prompts once,
then remembers it in `~/.apecx/globus_config.json`):

```bash
apecx-globus-setup          # opens your browser; then asks for your
                            # destination endpoint UUID (one-time).
```

You only need the **destination** endpoint (your local Globus Connect Personal
UUID, from Settings → Endpoints). The source collection + directories are
built-in defaults.

Want extra data beyond the BV-BRC/VIOLIN defaults? Register additional source
directories (fetched recursively):

```bash
apecx-globus-setup add-dir /apecx-ramanathan-anl/path/to/more-data
```

Headless / CI (no browser)? Use the secret path instead:
`export APECX_GLOBUS_AUTH_MODE=client_credentials` then
`apecx-globus-setup store --client-id <id> --client-secret <secret>` and set
`APECX_GLOBUS_SOURCE_ENDPOINT_ID` / `APECX_GLOBUS_DEST_ENDPOINT_ID` in the env.
Full details: [`globus_data_transfer.md`](globus_data_transfer.md).

---

## Step 4 — Run apecx-setup (~30 sec, longer on the first data transfer)

```bash
apecx-setup
```

Runs these steps in order (each prints what it will do; consent prompts gate
any system-touching action):

1. **`globus`** — preflight: SDK + auth + endpoint UUIDs. Surfaces readiness.
2. **`data`** — confirms the data directory (default `~/.apecx/data`), runs the
   Globus **verify→transfer** workflow (BV-BRC is required; VIOLIN is optional
   and skipped with a loud warning if your identity lacks `apecx-project-all`
   Group access — the install still completes), then patches
   `claude_desktop_config.json` with the `apecx` MCP server block (prompts for
   the three LLM env vars on first install).
3. **`infra`** — starts Postgres + Redis Docker containers if Docker is
   available; skipped if not (SQLite is the default backend, so optional).
4. **`llm`** — if `ollama` isn't on PATH, prompts to install (the exact command
   is printed BEFORE the y/N prompt); starts the daemon if needed; pulls
   `mistral-nemo:latest` (or `$APECX_LLM_MODEL`).
5. **`verify`** — health-checks every component; prints a summary table.

(`rag` and `rhea` are opt-in extra steps — `apecx-setup --with-rag` /
`--with-rhea` — and are skipped from the default chain.)

---

## Step 5 — Restart Claude Desktop

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

## Step 6 — First query

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
apecx-setup                     # re-transfer data over Globus (prompts before overwrite)
uv tool install --reinstall --python 3.12 \
  git+https://github.com/AlexandrNP/apecx-mcp-integration.git   # update
```

---

## Honest limitations

- **`APECX_LLM_API_KEY` is plaintext** in `claude_desktop_config.json`
  if you use a paid cloud LLM. Operator-managed; no built-in vault
  integration.
- **Globus access** — the data lives on a Globus collection. BV-BRC is on the
  public path; VIOLIN is gated by the `apecx-project-all` Globus Group. Without
  Group membership the install completes on BV-BRC alone (loud warning) and
  VIOLIN-dependent lookups return empty until access is granted. Headless/CI
  installs must use the secret auth path (no browser for the device-code flow).
- **First-launch latency** — apecx-mcp autostarts the Control Plane
  backend on first MCP call (~2–5 s the first time; sub-second
  thereafter).
- **Composer is model-sensitive.** mistral-nemo and larger work;
  small / heavily-quantized models hallucinate workflow YAML in
  ways that break execution.
