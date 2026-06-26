#!/usr/bin/env bash
# apecx-mcp server installer — one command to bring up the full backend stack (Postgres /
# Redis / MinIO / Ollama / Rhea MCP) on one docker network, then prepare apecx-mcp to run over
# HTTP. Idempotent: re-running reuses healthy containers + existing volumes.
# See deploy/SERVER_DEPLOYMENT.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.server.yml"
ENV_FILE="$SCRIPT_DIR/.env"
WRAPPER="$SCRIPT_DIR/run-mcp.sh"
INGEST_TIMEOUT="${INGEST_TIMEOUT:-1800}"   # seconds the one-shots may take (model pulls + ingest)

note() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# --- 1. preflight -------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker not found. Install Docker first."
docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) not found. Install the Compose plugin."
command -v uv >/dev/null 2>&1 || die "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
command -v openssl >/dev/null 2>&1 || die "openssl not found (needed to generate deploy/.env secrets)."
docker info >/dev/null 2>&1 || die "Docker daemon not reachable. Start Docker and retry."

# --- 2. env -------------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$SCRIPT_DIR/.env.server.example" "$ENV_FILE"
  # Generate strong unique secrets so the example defaults never reach a deployment. Only fills
  # lines left EMPTY (VAR=); an operator-provided value is preserved. (No Redis password — rhea's
  # redis client passes none; requirepass needs a rhea-side change. See SECURITY_AUDIT_PLAN.md.)
  _pg="$(openssl rand -hex 24)"; _mu="apecx-$(openssl rand -hex 4)"; _mp="$(openssl rand -hex 24)"
  awk -v pg="$_pg" -v mu="$_mu" -v mp="$_mp" '
    /^POSTGRES_PASSWORD=$/   { print "POSTGRES_PASSWORD=" pg; next }
    /^MINIO_ROOT_USER=$/     { print "MINIO_ROOT_USER=" mu; next }
    /^MINIO_ROOT_PASSWORD=$/ { print "MINIO_ROOT_PASSWORD=" mp; next }
    { print }
  ' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  note "Created $ENV_FILE (mode 600) with generated Postgres/MinIO secrets. Review it (esp. RHEA_REPO_PATH, APECX_DATA_ROOT), then re-run."
  note "KEEP these secrets: the Postgres/MinIO data volumes are initialized with them. Regenerating later (deleting .env) requires recreating the volumes ('docker compose -f $COMPOSE_FILE down -v') or the services will reject the new credentials."
  exit 0
fi
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

# Resolve RHEA_REPO_PATH to an absolute path so the compose build context is unambiguous
# regardless of the cwd compose runs from. Skip if the operator opted into a prebuilt RHEA_IMAGE.
if [[ -z "${RHEA_IMAGE:-}" ]]; then
  [[ -n "${RHEA_REPO_PATH:-}" ]] || die "RHEA_REPO_PATH unset in $ENV_FILE (path to a rhea checkout). Clone rhea as a sibling, or set RHEA_IMAGE to a prebuilt image (and drop the build: blocks)."
  RHEA_REPO_PATH="$(cd "$REPO_ROOT" && cd "$RHEA_REPO_PATH" 2>/dev/null && pwd)" \
    || die "RHEA_REPO_PATH does not resolve to a directory. Clone rhea there (git clone <rhea repo> ../rhea)."
  [[ -f "$RHEA_REPO_PATH/Dockerfile" ]] || die "No Dockerfile at $RHEA_REPO_PATH — is that the rhea repo root?"
  export RHEA_REPO_PATH
  note "Rhea build context: $RHEA_REPO_PATH"
fi

# --- 3. bring up the stack ----------------------------------------------------------------
# depends_on conditions enforce ordering (postgres-healthy → db-init; +ollama-init → ingest;
# redis+postgres-healthy+db-init → rhea). We DON'T rely on `up --wait` (its handling of a leaf
# one-shot's exit is version-dependent); instead we `docker wait` each one-shot explicitly below.
note "Building + starting the stack (first run pulls models + ingests the Galaxy catalog; up to ~$((INGEST_TIMEOUT/60)) min)…"
compose up -d --build || die "compose up failed. Inspect: docker compose -f $COMPOSE_FILE logs"

# Block on each one-shot and assert exit 0. `docker wait` blocks until the (already-created)
# container reaches stopped state, even while it's still gated behind its depends_on.
for c in apecx-ollama-init apecx-rhea-db-init apecx-rhea-ingest; do
  note "Waiting for one-shot: $c …"
  code="$(timeout "$INGEST_TIMEOUT" docker wait "$c" 2>/dev/null || echo timeout)"
  [[ "$code" == "0" ]] || die "$c did not succeed (result=$code). Logs: docker logs $c"
done

# --- 4. post-ingest gate (defeats the false-green probes) ---------------------------------
# postgres `SELECT 1` and rhea `tools/list` (find_tools is ALWAYS registered) both go green on an
# EMPTY catalog. Assert real rows before declaring the stack ready.
note "Verifying the Rhea tool catalog is populated…"
TOOL_COUNT="$(compose exec -T postgres psql -U postgres -d rhea -tAc 'SELECT count(*) FROM galaxytools' 2>/dev/null | tr -d '[:space:]' || echo 0)"
[[ "${TOOL_COUNT:-0}" -gt 0 ]] || die "galaxytools is empty (count=$TOOL_COUNT) — Rhea ingest produced no tools. Check: docker logs apecx-rhea-ingest"
note "Rhea catalog: $TOOL_COUNT tool(s) ingested."

# --- 5. install apecx-mcp on the host -----------------------------------------------------
note "Installing apecx-mcp on the host (uv tool install)…"
( cd "$REPO_ROOT" && uv tool install --python 3.12 --force . ) || die "uv tool install failed."

# --- 6. generate the run wrapper (delivers deploy/.env to the host process) ---------------
# apecx-mcp reads os.environ only — it does NOT load deploy/.env. This wrapper sources the env
# file and execs the server so AUTOSTART_INFRA / RHEA_MCP_URL / APECX_DATA_ROOT / host+port all
# take effect. Run it directly, or wrap it in screen/systemd.
HOST="${APECX_MCP_HOST:-127.0.0.1}"; PORT="${APECX_MCP_PORT:-8001}"
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
# Generated by install-server.sh — runs apecx-mcp over HTTP with deploy/.env loaded.
set -euo pipefail
set -a; source "$ENV_FILE"; set +a
exec apecx-mcp --transport streamable-http --host "\${APECX_MCP_HOST:-$HOST}" --port "\${APECX_MCP_PORT:-$PORT}"
EOF
chmod 700 "$WRAPPER"  # owner-only: it sources deploy/.env (secrets)

# --- 7. how to run + verify + expose ------------------------------------------------------
cat <<EOF

$(note "Stack is up. Backends on 127.0.0.1, default ports postgres:5435  redis:6379  minio:9000/9001  ollama:11434  rhea:3001/mcp/  (override any via *_HOST_PORT in deploy/.env)")

Run the MCP server over HTTP (the wrapper loads deploy/.env so autostart/Rhea/data vars apply):

    bash $WRAPPER
    # or under a session manager:  screen -dmS apecx-mcp bash $WRAPPER

It binds http://$HOST:$PORT/mcp and autostarts a control-plane child on :8000 (separate service).

Verify the /mcp handshake:

    curl -i -s http://localhost:$PORT/mcp \\
      -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \\
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0.1"}}}'

Expose over HTTPS for a Claude/ChatGPT connector (install a tunnel separately — NOT bundled):

    ngrok http $PORT
    # or: cloudflared tunnel --url http://localhost:$PORT
    # then point the connector at https://<your-tunnel>/mcp

EOF
