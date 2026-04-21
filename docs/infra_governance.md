# Infra governance: auto-provisioning the Control Plane's database

## Design intent

The target user is a scientist who installs the MCP server + backend and
starts using Claude Desktop. They should not have to run
`docker compose up` or set environment variables before their first
interaction. The backend is the thing that owns its own persistence
stack on first boot, reuses it on subsequent boots, and gets out of the
way when someone deliberately points at a shared/remote database.

The three supported deployment modes are decided from the DB URL alone:

| DB URL                                    | Mode                  | What the app does on startup             |
| ----------------------------------------- | --------------------- | ---------------------------------------- |
| `sqlite:///...`                           | SQLITE_NO_INFRA       | alembic upgrade head (creates file)      |
| `postgresql://...@localhost:5433/apecx_cp`| LOCAL_POSTGRES_MANAGED| ensure container up; alembic upgrade head |
| `postgresql://...@<other host|port>/...`  | REMOTE_POSTGRES_BYO   | alembic upgrade head only (trust operator) |

The logic lives in `src/apecx_integration/control_plane/infra/urls.py`
(`decide_infra_mode`) and in `src/.../infra/lifecycle.py`
(`ensure_infra_ready`, `teardown_infra`).

## Why remote Postgres is BYO, not managed

The third row of the table above deserves a rationale. Linking a remote
Postgres is **not** a substitute for the app providing infrastructure —
the three modes solve three genuinely different problems:

- **LOCAL_POSTGRES_MANAGED** (the "we provide it" path) exists because
  the scientist shouldn't have to install or configure anything. The
  backend uses whatever local container runtime is available to bring
  up a process on the same host it runs on. This is the default ergonomic
  we promise.
- **SQLITE_NO_INFRA** is the "we provide it, but even lighter" path:
  the DB is a file, so there's nothing to provision.
- **REMOTE_POSTGRES_BYO** is the escape hatch for operators who
  deliberately want a different backing store (managed RDS, an
  organization-provided Postgres cluster, a shared dev DB, a test harness
  pointing at a scratch DB for a specific pipeline). The app honors their
  choice by **not interfering** with whatever is upstream.

There is a **hard architectural reason the app cannot manage remote
infra**, beyond preference: the only levers we have for provisioning
(Docker daemon socket, Apptainer CLI) are **local**. Managing a container
on a remote host would require SSH orchestration, remote runtime binaries,
credentials, firewall assumptions — i.e., a deployment tool (Ansible,
Terraform, k8s operators) bolted into the Control Plane. That scope
explosion would turn the backend into a devops framework, which is the
opposite of "scientist installs MCP server + backend and interacts with
Claude." The BYO mode is the explicit, honest boundary between "the app
owns its infra" and "the operator owns the infra"; pretending to manage
something upstream we can't actually reach would be worse than refusing.

The remaining real question — "what if an ops team needs this for a
shared deployment?" — is answered by the existing escape hatches. They
bring their own Postgres via `APECX_CP_DB_URL`; we migrate schema and
stay out of their way.

## Runtime selection

`detect_runtime()` in `src/.../infra/runtime.py` picks between Docker
and Apptainer/Singularity:

1. **DockerRuntime** is preferred when the Docker daemon is reachable
   (not just when the client is installed). This covers scientist
   laptops, dev boxes, and CI.
2. **ApptainerRuntime** is the fallback for HPC login and compute nodes
   where Docker's daemon requirement is incompatible with
   unprivileged-user policy. Apptainer/Singularity runs without root and
   without a daemon.
3. If neither is available, the app raises `ContainerRuntimeUnavailable`
   with a message pointing the user at SQLite or a BYO Postgres URL as
   their escape hatches.

## Parity gaps between Docker and Apptainer

The Apptainer runtime is intentionally a subset, per user directive
2026-04-21 ("feel free to leave some features docker-only").

| Feature                            | Docker | Apptainer | Notes                                                                                  |
| ---------------------------------- | ------ | --------- | -------------------------------------------------------------------------------------- |
| Bring container up                 | ✅     | ✅        | `docker compose up` / `apptainer instance start`                                       |
| Named volume                       | ✅     | ❌        | Apptainer uses a bind-mounted host path (`~/.apecx_cp/postgres_data` by default).      |
| Healthcheck built into the runtime | ✅     | ❌        | Docker Compose emits `pg_isready` healthcheck; Apptainer is probed from Python via psycopg.SELECT 1. |
| Compose-style multi-service        | ✅     | ❌        | We only orchestrate one service (Postgres) for now.                                    |
| Ephemeral/CI override (tmpfs)      | ✅     | ❌        | See `docker-compose.ci.yml`. Apptainer users who need ephemerality can point `data_dir` at `/tmp` and delete it themselves. |

## Lifecycle commands

```
apecx-cp serve                 # default: ensure infra + run uvicorn
apecx-cp teardown              # stop the container, preserve data
apecx-cp teardown --remove-data # stop + delete volume / bind-mount dir
```

`serve` is idempotent: repeated starts detect an already-running
container and no-op rather than creating a new one.

## Tests

- `tests/unit/test_infra_urls.py`         — URL classification (fast)
- `tests/unit/test_apptainer_commands.py` — Apptainer argv (fast, no apptainer needed)
- `tests/integration/test_docker_runtime.py`   — live Docker daemon
- `tests/integration/test_apptainer_runtime.py` — skipped unless apptainer is on `$PATH`
- `tests/integration/test_infra_lifecycle.py`  — the full `ensure_infra_ready` flow against a live Docker daemon
