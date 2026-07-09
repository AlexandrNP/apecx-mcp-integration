# apecx-mcp deployment — security audit plan

Validates the hardened `deploy/` profile (see `SERVER_DEPLOYMENT.md` "Security profile") against the
agreed access model: one public, **unauthenticated** MCP URL as the only ingress; no direct access
to any component; all on one walled host. Two phases:

- **Phase L (local / on-host)** — run on the deploy host; proves configuration + containment.
- **Phase D (external)** — run from a *different* machine; proves nothing but `:443` is reachable.

Run every phase on each deploy and record the output (the policy's "no record, no go-live"). Each
row is `change → command → pass-condition`. Substitute `<host>` / `<COMPOSE>` =
`deploy/docker-compose.server.yml` / set the secret env first (`set -a; . deploy/.env; set +a`).

## Phase L — on-host

| # | Change | Command | Pass condition |
|---|---|---|---|
| L0 | 3.2/3.4 MCP + CP loopback | `ss -tlnp \| grep -E ':8001\|:8000'` | both bound `127.0.0.1`, never `0.0.0.0` |
| L1 | 3.1 backends loopback | `docker compose -f <COMPOSE> config \| grep -A2 published` | every `host_ip: 127.0.0.1`; no `0.0.0.0` publish |
| L2 | 3.5 no default creds | `docker compose -f <COMPOSE> config \| grep -iE 'minioadmin\|postgres:postgres@'` | **no matches** (defaults gone) |
| L2b| 3.5 fail-loud | unset `POSTGRES_PASSWORD`; `docker compose -f <COMPOSE> config -q` | errors `required variable POSTGRES_PASSWORD is missing` (non-zero exit) |
| L2c| 3.5 Redis/Postgres auth | `docker exec apecx-rhea-postgres psql -U postgres -c '\\l'` with the WRONG password | rejected. (Redis: no password yet — KNOWN GAP below) |
| L3 | 3.6 resource limits | `docker inspect apecx-ollama --format '{{.HostConfig.Memory}} {{.HostConfig.PidsLimit}} {{.HostConfig.NanoCpus}}'` (repeat per service) | all non-zero (limits applied) |
| L3b| 3.10 digest pins | `docker compose -f <COMPOSE> config \| grep 'image:'` | the 4 public images carry `@sha256:`; rhea = `apecx-rhea-server:local` |
| L4 | 3.3 edge abuse controls | through nginx: `for i in $(seq 50); do curl -s -o /dev/null -w '%{http_code}\n' https://<host>/mcp -d @big.json; done` | oversized (`>2m`) rejected `413`; flood throttled (`429`/`503`); other paths `404` |
| L5 | 3.7 code-exec containment | trigger a PyMOL/sandbox run, then `docker inspect <spawned> --format '{{.HostConfig.NetworkMode}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.ReadonlyRootfs}}'` | `none` / `[ALL]` / `no-new-privileges` / (sandbox) `true`; **no** `/var/run/docker.sock` mount |
| L6 | #1 spawn cap | `APECX_MAX_CONCURRENT_DOCKER_RUNS=2` then drive 4 concurrent code-exec runs | ≤2 containers run at once (2 waves); `tests/integration/test_docker_sandbox_runtime.py::test_admission_cap_serializes_concurrent_spawns` (docker-gated) |
| L7 | 3.9 secrets/perms | `stat -c '%a' deploy/.env deploy/run-mcp.sh`; `gitleaks detect`; grep tool outputs for host paths/secrets | `.env` 600, wrapper 700; gitleaks clean; no secrets/paths in outputs |
| L8 | 3.3 nginx valid | `sudo nginx -t` | syntax OK (deferred from CI — sandbox blocked the containerized check) |
| L9 | 3.10 vuln baseline | `trivy image <each pinned image>` + `trivy image apecx-rhea-server:local` | report recorded as the per-deploy baseline |

## Phase D — external (from another machine)

| # | Change | Command | Pass condition |
|---|---|---|---|
| D1 | 3.1/3.2/3.4/3.8 | `nmap -Pn <host>` | **only** `443/tcp` open (+ SSH if allowed); 5435/6379/9000/9001/11434/3001/8001/8000 all closed/filtered |
| D2 | 3.8 Docker ∌ ufw | `nmap -p 5435,6379,9000,11434,3001 <host>` | all filtered — confirms loopback binds + ufw hold, Docker did not punch through |
| D3 | 3.2/3.3 ingress | `curl -i https://<host>/mcp` (initialize handshake) vs `curl http://<host>:8001/mcp` | `:443/mcp` works; direct `:8001` refused/unreachable |

## Known gaps (carried, not blockers — see SERVER_DEPLOYMENT.md)

- **Redis has no password** (L2c). rhea's redis client passes none (`rhea/server/utils.py:166`,
  `launch_agent.py:30`); `requirepass` needs a rhea-side change. Compensating control: Redis is
  loopback + firewall only. Track as a rhea T-ticket.
- **Regenerate-secrets vs. stale volumes.** New `.env` secrets + persisted Postgres/MinIO volumes →
  auth failure; recreate volumes (`down -v`) or keep the secrets.
- **`nginx -t` + Trivy are Phase-L (deploy-time)**, not CI — the config + scan baseline are verified
  on the host, not in the repo pipeline.
- **rhea-server is daemon-privileged** (per-tool-container execution). The orchestrator mounts
  `/var/run/docker.sock` into `apecx-rhea-server` so the in-container agent can `docker run` each
  Galaxy tool's biocontainer on the host daemon — this grants rhea-server root-equivalent control of
  the host Docker daemon. It is INHERENT to the per-tool-container design, not a misconfiguration.
  Compensating controls: rhea-server binds loopback + firewall only (never world-visible), and the
  socket is NOT passed to the spawned tool containers (L5 still holds — the biocontainers rhea
  launches carry no socket). On HPC the socket is not used at all (Apptainer is daemonless). Track a
  least-privilege follow-up (rootless/socket-proxy) as a rhea T-ticket.

## Exit criteria (go-live gate)

Go-live only when ALL hold, with output recorded:
1. Phase D1 shows only `:443` (+SSH) externally.
2. Phase L1/L2/L2b — every backend loopback; zero default creds; fail-loud confirmed.
3. Phase L4 — oversized/flooding requests rejected through nginx.
4. Phase L5/L6 — code-exec containment flags intact; the spawn cap serializes.
5. Phase L7 — secrets mode-600, gitleaks clean, no secret/path leakage in tool outputs/errors.
6. The deploy record: resolved `docker compose config`, image digests, the Trivy baseline, and a
   fresh external `nmap` showing only `:443`.
