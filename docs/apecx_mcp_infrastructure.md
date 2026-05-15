# `apecx-mcp` infrastructure orchestrator

This document is the operator's reference for the startup-time
infrastructure orchestrator: what it brings up, what it merely probes,
which env vars steer its behavior, what operator prerequisites the
orchestrator can NOT install, and how the `infrastructure_status` MCP
tool reports state at runtime.

The orchestrator is launched as a background daemon thread from
`build_server()` in `src/apecx_integration/mcp_surface/server.py`.
Source code lives at `src/apecx_integration/infrastructure/`.

## 1. The 5-backend roster

| Backend     | Kind              | Required | Default endpoint              | Probe                                                        |
|-------------|-------------------|----------|-------------------------------|--------------------------------------------------------------|
| `postgres`  | `docker_container`| yes      | `localhost:5435`              | `psycopg.connect()` + `SELECT 1`                             |
| `redis`     | `docker_container`| yes      | `localhost:6379`              | `redis.Redis(...).ping()`                                    |
| `minio`     | `docker_container`| yes      | `localhost:9000`              | `httpx.get("/minio/health/live")`                            |
| `ollama`    | `external`        | yes      | `http://localhost:11434`      | `httpx.get("/api/tags")` + model count                       |
| `rhea_mcp`  | `host_process`    | yes      | `http://localhost:3001/mcp/`  | `MCPTransport.call("tools/list", {})` + tool count           |

What "kind" means:

- **`docker_container`** — the orchestrator will `docker run` it when
  the probe is down AND `APECX_MCP_AUTOSTART_INFRA` is enabled.
  A stopped-but-existing container is `docker start`-ed (preserves
  volume state); a missing container is freshly spawned from the
  pinned image.
- **`host_process`** — the orchestrator can `Popen` the process IF the
  prereq env vars (`RHEA_REPO_PATH`, `RHEA_PYTHON_PATH`) are set. Without
  them, the backend reports `external_unconfigured` with an actionable
  remedy. The orchestrator does NOT install Rhea or its miniconda env.
- **`external`** — operator-managed entirely. The orchestrator
  probes only; reports `external_missing` if down. Installing Ollama
  is the operator's job.

## 2. Per-backend state machine

```
missing ──start_all──► starting ──probe-ok──► ready
                              │
                              └─probe-fail──► error_starting (autostart attempted, failed)
                              │
reused: backend was already up at start_all() time.
external_skipped: APECX_MCP_AUTOSTART_INFRA=0.
external_missing: probe down + cannot autostart (Docker missing, Ollama missing).
external_unconfigured: host_process prereq env-vars unset (Rhea MCP).
degraded: was ready; latest re-probe came back unhealthy. The next status call re-probes; recovery flips back to ready/reused.
```

The `infrastructure_status` MCP tool always re-probes ready backends
on every call (with a short per-probe timeout). The tool will NEVER
return stale green from N minutes ago.

### 2.1 Data persistence and the fresh-create warning

The orchestrator's container-spawn path discriminates between two
cases:

* **Operator's container exists but is stopped** → `docker start
  <name>`. Volume state is preserved. No warning.
* **No container by that name exists** → `docker run -d --name <name>
  -v <named_volume>:<path> <image>`. The named volume in
  `ContainerSpec.volumes` survives `docker rm`, but if the operator
  ALSO removed the named volume (`docker volume rm`) the new container
  starts empty.

That second case is a real silent-failure shape: the probe goes green
on the empty fresh container, every test passes, and the operator
discovers their pgvector rows are gone only when a workflow returns
zero results. To surface this honestly the orchestrator sets a
`fresh_create_warning` on the `BackendRuntime` whenever it takes the
`docker run` path; `infrastructure_status` lifts the warning into the
`actionable` list. The warning fires unconditionally on fresh creation
— "the named volume MAY survive prior data" is not the same as "it
DOES" — and the operator's response is to verify (volume list, row
counts) before trusting the backend.

Backends with declared named volumes:

| Backend                | Volume                          | Mounted at                    |
|------------------------|---------------------------------|-------------------------------|
| `apecx-rhea-postgres`  | `apecx-rhea-postgres-data`      | `/var/lib/postgresql/data`    |
| `apecx-rhea-minio`     | `apecx-rhea-minio-data`         | `/data`                       |
| `apecx-redis`          | *(intentionally none)*          | — (ephemeral cache only)      |

If you've been running with the pre-volume orchestrator (before this
commit), your existing `apecx-rhea-postgres` / `apecx-rhea-minio`
containers do NOT have these named volumes attached. They keep
working — the orchestrator reuses them on probe-green and never
touches them. To migrate: stop + remove the container, let the
orchestrator (or `apecx-setup`) recreate it from the new spec. You
WILL lose the data inside the old container; export it first if it
matters (`pg_dump` for Postgres, `mc mirror` for MinIO).

## 3. Environment variables

| Variable                    | Default                          | Effect                                                                                                                                                                  |
|-----------------------------|----------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `APECX_MCP_AUTOSTART_INFRA` | `1` (on)                         | When `0`, the orchestrator runs in probe-only mode: it never spawns containers/processes, but still reports current state through the status tool.                       |
| `APECX_LLM_BASE_URL`        | `http://localhost:11434/v1`      | Where the Ollama probe looks. A trailing `/v1` is stripped (Ollama's REST API is rooted at the host, not under `/v1`).                                                  |
| `RHEA_MCP_URL`              | `http://localhost:3001/mcp/`     | Where the Rhea MCP probe connects.                                                                                                                                       |
| `RHEA_REPO_PATH`            | unset                            | Path to the Rhea checkout. Required for the orchestrator to attempt autostart of the Rhea MCP host process. Unset → state is `external_unconfigured`.                  |
| `RHEA_PYTHON_PATH`          | unset                            | Path to the miniconda `bin/` directory whose Python carries Rhea's dependencies. Required for Rhea autostart. Unset → state is `external_unconfigured`.                  |
| `APECX_MCP_SKIP_HEALTHCHECK`| unset (off)                      | Skips the **control-plane** healthcheck (the legacy `_verify_control_plane_reachable` path). Does NOT affect the infrastructure orchestrator; use `APECX_MCP_AUTOSTART_INFRA=0` for that. |

## 4. Operator prerequisites we can't install

The orchestrator will tell you what's missing with an actionable
message. The three things it cannot install for you:

### Docker Desktop

- The orchestrator can't `brew install --cask docker` for you (it
  requires sudo and a UI permission dance). Get it from
  <https://www.docker.com/products/docker-desktop/>. Start it before
  launching `apecx-mcp`.
- Detection: when `docker info` returns non-zero, every `docker_container`
  backend transitions to `external_missing` with the install link in
  the actionable message.

### Ollama

- Install: macOS `brew install ollama`, Linux
  `curl -fsSL https://ollama.ai/install.sh | sh`, or download from
  <https://ollama.com/download>.
- Start: `ollama serve` (or `brew services start ollama` on macOS).
- Detection: probe at `$APECX_LLM_BASE_URL` (default
  `http://localhost:11434`) fails → `external_missing` with the
  install link.

### miniconda / Rhea checkout

- Required only if you want the orchestrator to autostart the Rhea
  MCP host process. Otherwise: start `python -m
  rhea.server.mcp_server --transport streamable-http` yourself from
  inside Rhea's checkout.
- Detection: `RHEA_REPO_PATH` and/or `RHEA_PYTHON_PATH` unset →
  `external_unconfigured`.

## 5. The `infrastructure_status` MCP tool

Returns a JSON dict the model renders back to the operator. Shape:

```jsonc
{
  "overall": "ready",
  "autostart_enabled": true,
  "orchestrator_uptime_seconds": 23.4,
  "start_all_completed": true,
  "backends": [
    {
      "name": "postgres",
      "display_name": "Postgres (apecx-rhea-postgres / pgvector)",
      "kind": "docker_container",
      "required": true,
      "state": "reused",
      "detail": "postgres OK on localhost:5435 (db=rhea, user=postgres)",
      "last_probe_at": 1778824361.46,
      "latency_ms": 27.4,
      "spawned_by_us": false,
      "tags": ["vector-store", "rhea-deps"]
    },
    // … 4 more entries: redis, minio, ollama, rhea_mcp
  ],
  "actionable": []
}
```

### Field meaning

- **`overall`** — one of `ready`, `starting`, `degraded`, `down`,
  `disabled`. `down` means at least one required backend is in a
  terminal-failure state (`error_starting` / `down`).
- **`autostart_enabled`** — the singleton's at-construction-time
  reading of `APECX_MCP_AUTOSTART_INFRA`. Once the singleton is
  constructed, this is fixed for the process lifetime.
- **`orchestrator_uptime_seconds`** — seconds since `start_all()` was
  first invoked.
- **`start_all_completed`** — `false` while bring-up is in flight;
  `true` after every backend has finished its initial probe → spawn
  → poll cycle.
- **`backends`** — per-backend state. `latency_ms` is the last probe's
  RTT. `last_probe_at` is the Unix timestamp of the last probe.
  `spawned_by_us` is `true` only when the orchestrator brought this
  backend up (in which case `atexit` will tear it down on MCP-server
  exit).
- **`actionable`** — a list of one-line strings per non-ready backend.
  These are the strings you should follow to fix things.

### Diagnosing a stuck startup from Claude Desktop

Ask Claude to call `infrastructure_status`. The tool's return tells
you which backends are down and what to do. Typical patterns:

- **"overall: starting"** for >30 seconds → a docker container is
  taking forever to come up. Check its state via
  `docker logs apecx-rhea-postgres` (or the other container name).
- **"overall: degraded"** → one or more backends were ready earlier
  but died. The `detail` field for each backend tells you why.
- **"overall: down"** → a required backend is in `error_starting`
  (autostart was attempted but failed). The actionable message tells
  you the remedy.
- **"overall: disabled"** → `APECX_MCP_AUTOSTART_INFRA=0` and
  `start_all` has not run yet. Status tool still reports what's
  reachable.

## 6. Connecting to Claude Desktop

Add or merge into `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS path; analogous on other OSes):

```json
{
  "mcpServers": {
    "apecx": {
      "command": "/path/to/apecx-mcp-integration/.venv/bin/apecx-mcp",
      "env": {
        "APECX_DATA_ROOT": "/path/to/apecx-data",
        "APECX_MCP_AUTOSTART_INFRA": "1",
        "APECX_LLM_BASE_URL": "http://localhost:11434/v1",
        "APECX_LLM_MODEL": "mistral-nemo:latest",
        "RHEA_MCP_URL": "http://localhost:3001/mcp/",
        "RHEA_REPO_PATH": "/path/to/rhea",
        "RHEA_PYTHON_PATH": "/opt/miniconda3/envs/rhea/bin"
      }
    }
  }
}
```

`RHEA_REPO_PATH` + `RHEA_PYTHON_PATH` are optional — without them
the orchestrator marks `rhea_mcp` as `external_unconfigured` and the
operator runs the Rhea MCP server by hand.

After editing `claude_desktop_config.json`, fully quit and relaunch
Claude Desktop.

## 7. Degraded-mode story

When the orchestrator is in a non-ready state, Rhea-backed tools
(catalog tool, structural search, etc.) return `UNAVAILABLE` with a
reason rather than failing silently. The model can show the operator
the actionable remedy.

Recovery path:

1. Operator reads the actionable message in `infrastructure_status` —
   e.g. `"Rhea MCP is unreachable at http://localhost:3001/mcp/. To
   enable autostart, set $RHEA_REPO_PATH ..."`.
2. Operator sets the env var (or manually starts Rhea MCP).
3. Operator either restarts `apecx-mcp` (which re-runs `start_all`)
   or calls `infrastructure_status` again — the per-call re-probe
   will flip the backend back to `ready` or `reused` on the next
   call.

Note: a backend that has been brought down and back up by the operator
is not automatically re-spawned by the orchestrator's `start_all` —
that path runs once per `apecx-mcp` start. The per-call re-probe in
`infrastructure_status` does, however, catch the recovery.

## 8. Reference

- Source: `src/apecx_integration/infrastructure/`
  - `backends.py` — dataclasses + state enum
  - `containers.py` — shared Docker container specs (also used by
    `apecx-setup`)
  - `probes.py` — per-backend health probes
  - `orchestrator.py` — `InfraOrchestrator` + singleton accessor +
    background-thread launcher
- MCP tool: `src/apecx_integration/mcp_surface/tools/infrastructure_status.py`
- Tests: `tests/unit/test_infrastructure_orchestrator.py`,
  `tests/integration/test_infrastructure_orchestrator_live.py`

## 9. Why this isn't a nanobrain workflow component

The orchestrator is operational plumbing — startup-time bring-up plus
runtime status reporting. Forcing it through `from_config` +
`Workflow.from_config(...)` + DataUnit/Trigger/Link wiring would add
ceremony without buying anything: the orchestrator has no event-driven
data flow, no per-step business logic, no LLM dispatch. It is one
async function (`start_all`) and one snapshot function (`status`),
guarded by a `threading.Lock` so cross-loop status reads are safe.

This is consistent with how `_verify_control_plane_reachable` and
`_ensure_synonym_dict_or_warn` are written today (also in `server.py`)
— they're operational plumbing, not nanobrain components.
