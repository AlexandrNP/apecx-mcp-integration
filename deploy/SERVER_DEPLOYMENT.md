# apecx-mcp — server / remote deployment

`apecx-setup` targets a single-user **local** install (native Ollama for Metal/GPU, backends on
`localhost`, stdio transport). This `deploy/` bundle is the **server** path: every backing
service in containers on one network, the MCP server exposed over HTTP, ready to tunnel to a
Claude/ChatGPT connector.

## What it brings up

One `docker compose` stack on the `apecx-net` network:

| Service | Image | Host port | Role |
|---|---|---|---|
| postgres | `pgvector/pgvector:0.8.0-pg17` | 5435 | apecx caches **+** Rhea vector store (`rhea` DB) |
| redis | `redis:7` | 6379 | caches / task queue / ProxyStore |
| minio | `minio/minio` | 9000, 9001 | S3-compatible object store |
| ollama | `ollama/ollama` | 11434 | chat synthesis **+** Rhea tool-RAG embeddings |
| rhea | built from your rhea checkout | 3001 | Rhea MCP worker (`/mcp/`) |

Plus three one-shot jobs that exit 0: `ollama-init` (pull models), `rhea-db-init` (create the
`vector` extension + `galaxytools` schema), `rhea-ingest` (populate the Galaxy tool catalog).

**`apecx-mcp` itself runs on the host** (via `uv tool install`), pointed at the published
host-ports, with autostart disabled so it *probes* these containers instead of spawning its own.

## Prerequisites

- Docker + the Compose v2 plugin (`docker compose version`), daemon running.
- `uv` (https://docs.astral.sh/uv/).
- A **rhea checkout** as a sibling clone (the compose builds the Rhea image from it):
  `git clone <rhea repo> ../rhea`. (Or set `RHEA_IMAGE` to a prebuilt image and drop the
  `build:` blocks — see the env file.)
- The apecx dataset + dictionary on the host for the database/harmonized-search tools: run
  `apecx-setup data dict` once (or copy an existing `~/.apecx`), and set `APECX_DATA_ROOT`.

## Quick start

```bash
cp deploy/.env.server.example deploy/.env
$EDITOR deploy/.env            # set RHEA_REPO_PATH (absolute or ../rhea) + APECX_DATA_ROOT
bash deploy/install-server.sh  # builds, pulls models, ingests, verifies; first run ~10-15 min
```

The script gates on container health **and** on a non-empty Rhea catalog (a real `count(*)`,
not just a port check), installs `apecx-mcp`, and generates `deploy/run-mcp.sh`.

Run the server via the generated wrapper:

```bash
bash deploy/run-mcp.sh
# or under a session manager:  screen -dmS apecx-mcp bash deploy/run-mcp.sh
```

The wrapper sources `deploy/.env` and execs `apecx-mcp --transport streamable-http --host … --port …`.
This matters: **`apecx-mcp` reads `os.environ` only — it does not load `deploy/.env` itself**, so
running the bare binary would miss `APECX_MCP_AUTOSTART_INFRA`, `RHEA_MCP_URL`, `APECX_DATA_ROOT`,
etc. Always launch it through the wrapper (or otherwise export those vars first).

## Two services, two ports — don't collide

The apecx **control plane** (FastAPI, Swagger at `/docs`) and the **MCP** server are *different*
services. Keep them on different ports:

- MCP server → **8001** (`--host/--port`, set in `deploy/.env`).
- control plane → **8000**. By default `apecx-mcp` **autostarts a control-plane child** (on
  SQLite, :8000) at boot and the MCP tools POST to it — leave that on. Do **not** set
  `APECX_MCP_AUTOSTART_BACKEND=0` unless you also run `apecx-cp serve` yourself or set
  `APECX_MCP_SKIP_HEALTHCHECK=1`; otherwise startup exits with code 2.

The deployer's "`localhost:8000` is not connected to `/mcp`" was exactly this confusion: `:8000`
is the control plane; `/mcp` lives on the MCP server, which must run on its own port (8001).

## Expose over HTTPS (Claude / ChatGPT connector)

A connector needs a public **HTTPS** URL. Install a tunnel yourself (not bundled):

```bash
ngrok http 8001
# or: cloudflared tunnel --url http://localhost:8001
```

Then in Claude/ChatGPT → connectors → **Connector URL = `https://<your-tunnel>/mcp`**,
Authentication: None.

## GPU

Ollama runs CPU-only by default (works, slower). For NVIDIA GPUs, install
`nvidia-container-toolkit` on the host and uncomment the `deploy.resources` block on the
`ollama` service in `docker-compose.server.yml`. macOS Docker has no GPU passthrough — embeddings
+ synthesis run on CPU there (this is why the *local* `apecx-setup` keeps **native** Ollama for
Metal; do not "dockerize Ollama" locally).

## Troubleshooting — the six things a field deployment hit

1. **Could not set MCP host/port; `$FASTMCP_HOST/$FASTMCP_PORT` ignored.** Use the apecx-owned
   `apecx-mcp --host/--port` flags (or `APECX_MCP_HOST`/`APECX_MCP_PORT`). No hand-patching.
2. **Ollama needs a local admin install.** It's a container here (`ollama` service); no host
   install or admin rights.
3. **Manual `docker network create` + connect.** The compose puts everything on `apecx-net`;
   services resolve each other by name (`postgres`, `redis`, `ollama`, …).
4. **`:8000` had no `/mcp`.** See "Two services, two ports" — run MCP on 8001.
5. **No HTTPS.** See "Expose over HTTPS" — tunnel 8001.
6. **Rhea silently empty.** `rhea-db-init` provisions the schema and `install-server.sh` asserts
   `galaxytools` is non-empty — a stack with green ports but an empty catalog is rejected, not
   reported "ready". If ingest fails: `docker compose -f deploy/docker-compose.server.yml logs rhea-ingest`.

## Operations

```bash
# status / logs
docker compose -f deploy/docker-compose.server.yml ps
docker compose -f deploy/docker-compose.server.yml logs -f rhea
# stop (keeps volumes/data)            # full teardown (DROPS data)
docker compose -f deploy/docker-compose.server.yml down
docker compose -f deploy/docker-compose.server.yml down -v
```

`infrastructure_status` (an MCP tool) reports postgres/redis/minio/ollama/rhea_mcp health from
the running server.
