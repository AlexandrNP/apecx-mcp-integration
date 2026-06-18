#!/usr/bin/env bash
# Run apecx-mcp as a long-lived service so the InfraOrchestrator brings up + KEEPS the rhea-server
# online for the process lifetime (rhea = apecx-mcp lifetime; torn down cleanly on stop via the
# orchestrator's atexit SIGTERM->SIGKILL). Reproducible replacement for the ad-hoc
# `sleep | apecx-mcp` hold-stdin trick.
#
# Two operating modes:
#   * INTERACTIVE (Claude Desktop / an IDE): the MCP client IS apecx-mcp's stdin — just run
#     `apecx-mcp`; it lives for the client session and tears rhea down on disconnect.
#   * HEADLESS / SUPERVISED (a server box that wants rhea online for programmatic run_workflow):
#     a stdio server exits on stdin-EOF, so this script holds stdin open. Run it under a
#     supervisor (launchd/systemd/`nohup`) that restarts it on exit.
#
# Health of the rhea-server it manages: GET http://localhost:3001/mcp/ -> 406 = healthy
# (correct MCP reply to a plain GET), 500 = backend down, 000 = not listening. If redis/postgres
# are restarted underneath, restart this service so the orchestrator re-points rhea at them.
#
# Usage:
#   scripts/serve_apecx_mcp.sh                 # infra + apecx-mcp (rhea spawned if configured)
#   APECX_SERVE_WITH_RHEA=1 scripts/serve_apecx_mcp.sh   # also run the one-time `apecx-setup rhea`
#   extra args are forwarded to apecx-mcp (e.g. --locus agent)
set -euo pipefail

echo "[serve] bringing up infrastructure (idempotent) ..."
apecx-setup infra --non-interactive

if [ "${APECX_SERVE_WITH_RHEA:-0}" = "1" ]; then
  echo "[serve] ensuring rhea venv + tool ingestion (idempotent, one-time ~10min) ..."
  apecx-setup rhea --non-interactive
fi

echo "[serve] starting apecx-mcp (long-lived; stdin held open for headless supervision) ..."
# `tail -f /dev/null` keeps stdin open so apecx-mcp does not exit on EOF; the orchestrator's
# background thread spawns + keeps the rhea-server for this process's lifetime.
exec sh -c 'tail -f /dev/null | apecx-mcp "$@"' -- "$@"
