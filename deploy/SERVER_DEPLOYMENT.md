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

The **Host port** column shows the defaults; each is configurable via a `*_HOST_PORT` var in
`deploy/.env` (`POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`, `MINIO_HOST_PORT`, `MINIO_CONSOLE_HOST_PORT`,
`OLLAMA_HOST_PORT`, `RHEA_HOST_PORT`) — change one to avoid a host clash. The **bind host stays
`127.0.0.1`** (not configurable by design: these backends are unauthenticated, so loopback-only is the
threat-model boundary). The host-side `APECX_LLM_BASE_URL` / `RHEA_MCP_URL` derive from the Ollama/Rhea
port vars, so one edit moves both the publish and the consumer.

Plus three one-shot jobs that exit 0: `ollama-init` (pull models), `rhea-db-init` (create the
`vector` extension + `galaxytools` schema), `rhea-ingest` (populate the Galaxy tool catalog).

**`apecx-mcp` itself runs on the host** (via `uv tool install`), pointed at the published
host-ports, with autostart disabled so it *probes* these containers instead of spawning its own.

## Security profile (hardened by default)

This bundle assumes the agreed access model: a single public, **unauthenticated** MCP URL as the
only ingress, no direct access to any component, all on one walled host. The hardening that enforces
it (details in the companion `SECURITY_AUDIT_PLAN.md`):

- **Loopback binds (3.1/3.2).** Every backend publishes on `127.0.0.1` and `apecx-mcp` binds
  `127.0.0.1:8001`. The host-port numbers in the table above are loopback-only — unreachable
  off-host regardless of the firewall (and a `127.0.0.1` publish never inserts the DNAT rule that
  bypasses `ufw`).
- **nginx edge + firewall (3.3/3.8).** The only public surface is the nginx proxy on `:443`
  (`deploy/nginx/`) with rate/size/concurrency/timeout limits; `deploy/firewall.sh` is the second
  default-deny wall layer. See "Expose over HTTPS" + "Host firewall" below.
- **Secrets from `.env` (3.5/3.9).** `install-server.sh` generates strong Postgres/MinIO secrets
  into `deploy/.env` (mode-600); the old `postgres`/`minioadmin` defaults fail-loud. (Redis has no
  password yet — rhea's client passes none; see the known gap in `SECURITY_AUDIT_PLAN.md`.)
- **Resource limits + spawn cap (3.6/#1).** Per-service mem/cpu/pids limits + Ollama queue caps;
  and `APECX_MAX_CONCURRENT_DOCKER_RUNS` (default 4) bounds simultaneous per-request code-exec
  containers (PyMOL/sandbox), which the compose-service limits do not reach.
- **Digest-pinned images (3.10).** The four public images are pinned by `@sha256:`; rhea is built
  from source (scan-only).

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

## Expose over HTTPS — the nginx edge (the ONLY public surface)

`apecx-mcp` binds **127.0.0.1:8001** (loopback). The only thing reachable from off-host is the
**nginx reverse proxy on :443**, which terminates TLS and imposes the abuse controls that stand in
for the (deliberately absent) authentication — per-IP + global rate/concurrency limits, body-size
cap, timeouts, access logging. **Never expose `:8001` directly — that bypasses every limit.** Config
+ setup live in `deploy/nginx/` (the README covers the two ingress modes + the real-client-IP caveat).

```bash
sudo cp deploy/nginx/apecx-mcp.conf /etc/nginx/conf.d/apecx-mcp.conf
sudo nginx -t && sudo systemctl reload nginx
```

**Two ingress modes** (`deploy/nginx/README.md`):
- **Direct `:443`** — host with a public IP + DNS; nginx terminates TLS (provide a cert, e.g.
  certbot). Connector URL = `https://<your-host>/mcp`.
- **Behind a tunnel** — host with no public IP: a `cloudflared`/`ngrok` tunnel terminates public TLS
  and forwards to nginx's local `127.0.0.1:8443` (NOT to `:8001`). Enable Mode A in the config and
  set `set_real_ip_from` to the tunnel's source, or per-IP rate limits collapse to one global bucket.

Then in Claude/ChatGPT → connectors → **Connector URL = `https://<your-edge>/mcp`**,
Authentication: None (see "Accepted residual risk" below for what that means).

## Host firewall (the second wall layer)

Loopback binds (above) already make the backends unreachable off-host; the firewall is the
independent backstop the policy requires (P1 — two layers, neither removable). Run on the host:

```bash
SSH_CIDR=<your-admin-CIDR> sudo bash deploy/firewall.sh   # default-deny inbound; allow 443 + SSH
```

Prove it from off-host: `nmap <host>` shows **only** `443` (+SSH) — see `deploy/SECURITY_AUDIT_PLAN.md`.

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
5. **No HTTPS.** See "Expose over HTTPS" — the nginx edge on :443 fronts `127.0.0.1:8001` (don't
   tunnel straight to 8001; that bypasses the abuse controls).
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

## Accepted residual risk (the endpoint is unauthenticated — by design)

The MCP endpoint takes **no authentication** (an explicit product decision). The hardening above
contains the blast radius; it does **not** make anonymous access safe. So that the acceptance is
explicit and revisitable, here is what an anonymous caller *can* still do after all of it:

- **Invoke any catalogued tool / workflow** — there is no per-caller authorization.
- **Consume your LLM / GPU inference and compute** — bounded by the Ollama queue + nginx rate
  limits, but anonymous callers spend your resources.
- **Trigger code-exec containers** (PyMOL, the sandbox) — **capped** in count
  (`APECX_MAX_CONCURRENT_DOCKER_RUNS`) and **contained** (`--network none`, `--cap-drop ALL`,
  no-new-privileges, read-only/tmpfs, non-root, pids/mem limits — a protected contract; weakening
  any flag requires a threat-model update + audit re-run), but they do run on your host.
- **Hold result `data_handle`s** — unguessable `uuid4` capability tokens; safe from enumeration,
  but anyone who obtains one (e.g. from a leaked log) can read that run's artifacts.

**Mitigated by:** containment (code-exec flags + the cap) + edge limits (rate/size/concurrency/
timeout) + the two-layer wall (loopback + firewall). **NOT mitigated:** abuse-of-function — a caller
using the (rate-limited) tools and compute for their own ends, within the limits. Eliminating that
needs authentication at the edge; the accepted trade is "rate-limited anonymous code-exec on a
walled host." Revisit this if the audience or threat model changes (e.g. add a shared bearer token
at nginx for the cheapest 10x risk reduction).

## Known gaps (tracked)

- **Redis has no password.** rhea's redis client passes none; adding `requirepass` needs a rhea-side
  change first. Until then Redis is reachable only on loopback + behind the firewall.
- **Regenerating secrets vs. stale volumes.** If you delete `deploy/.env` and re-run
  `install-server.sh`, NEW secrets generate but the Postgres/MinIO data volumes persist with the
  OLD ones → auth failure. Recreate the volumes (`docker compose … down -v`) or keep the secrets.
- **`nginx -t` / Trivy run at deploy.** The nginx config + the image vuln-scan baseline are verified
  on the host at rollout (Phases L/D), not in CI.
