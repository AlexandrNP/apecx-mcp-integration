"""Canonical Docker container specs for the apecx-mcp stack.

Single source of truth shared by:

* ``apecx_integration.infrastructure.orchestrator`` — startup-time
  bring-up + runtime status probing.
* ``apecx_integration.cli.setup`` — the ``apecx-setup`` operator CLI
  (one-shot bring-up + ``apecx-setup verify``).

Two sources of truth = guaranteed drift; everything lives here.

The names and pinned images mirror the operator-running containers
documented in the deployment runbook:

* ``apecx-rhea-postgres`` — ``pgvector/pgvector:0.8.0-pg17``, pgvector
  extension required by Rhea's structural-search workflows. Host port
  ``5435`` is deliberately not the Postgres default (``5432``) so it
  doesn't collide with the operator's local Postgres install or the
  legacy ``apecx-postgres`` container that pre-existed this work.
* ``apecx-redis`` — ``redis:7``. Used by Rhea's task queue and by
  apecx-mcp's session caches.
* ``apecx-rhea-minio`` — ``minio/minio``, runs ``server /data``.
  Exposes the S3 API on ``9000`` and the console on ``9001``.

When a future container is added (e.g. an embedding worker), add its
:class:`ContainerSpec` here and import the new symbol from both
``cli/setup.py`` and the orchestrator.

A note on idempotence
---------------------
The orchestrator's bring-up path always probes the backend FIRST. If
the probe is green, the container is reused (``BackendState.REUSED``)
and no ``docker run`` is invoked. Only when the probe is red does it
attempt a spawn. A pre-existing-but-stopped container with the same
name is started via ``docker start <name>`` rather than re-created
(``docker run`` would error on name collision and we'd lose volume
state). The orchestrator's ``atexit`` teardown ONLY touches containers
recorded in its ``_spawned`` set — operator-pre-existing containers
survive shutdown.
"""

from __future__ import annotations

from apecx_integration.infrastructure.backends import ContainerSpec

APECX_RHEA_POSTGRES: ContainerSpec = ContainerSpec(
    image="pgvector/pgvector:0.8.0-pg17",
    container_name="apecx-rhea-postgres",
    ports=((5435, 5432),),
    env=(
        ("POSTGRES_PASSWORD", "postgres"),
        ("POSTGRES_DB", "rhea"),
    ),
    command=(),
    # Named volume — survives `docker rm` so the operator's pgvector
    # data persists across container respawns. Without this, a fresh
    # `docker run` of this spec would silently drop all rows.
    volumes=(("apecx-rhea-postgres-data", "/var/lib/postgresql/data"),),
    ready_timeout_s=30.0,
)


APECX_REDIS: ContainerSpec = ContainerSpec(
    image="redis:7",
    container_name="apecx-redis",
    ports=((6379, 6379),),
    env=(),
    command=(),
    # Redis is used purely as an ephemeral cache + ProxyStore /
    # agent-handle bus in this stack — no persistence needed across
    # container respawns. (If a workflow ever depends on Redis
    # durability, add a named volume here + an AOF/RDB config flag.)
    volumes=(),
    ready_timeout_s=15.0,
)


APECX_RHEA_MINIO: ContainerSpec = ContainerSpec(
    image="minio/minio",
    container_name="apecx-rhea-minio",
    ports=((9000, 9000), (9001, 9001)),
    env=(
        ("MINIO_ROOT_USER", "minioadmin"),
        ("MINIO_ROOT_PASSWORD", "minioadmin"),
    ),
    command=("server", "/data"),
    # Named volume so the object store survives container respawn.
    volumes=(("apecx-rhea-minio-data", "/data"),),
    ready_timeout_s=20.0,
)


def all_container_specs() -> tuple[ContainerSpec, ...]:
    """Return every container spec in a deterministic order.

    Order matches the dependency intent: Postgres first (everything
    needs it), Redis second (caches + queues), MinIO last
    (object storage). The orchestrator launches them in parallel
    regardless — ordering here is for human-facing UI only.
    """
    return (APECX_RHEA_POSTGRES, APECX_REDIS, APECX_RHEA_MINIO)


def container_run_args(spec: ContainerSpec) -> list[str]:
    """Build the ``docker run`` argv (without the leading ``docker run``).

    Produces a deterministic argv that round-trips through tests
    cleanly. Shape:

    ``["docker", "run", "-d", "--name", <name>, "-p", "H:C", ...,
       "-e", "K=V", ..., <image>, *<command>]``

    Callers prepend ``docker`` themselves so they can use ``shutil.which``
    or a custom binary path.
    """
    args = ["run", "-d", "--name", spec.container_name]
    for host, container in spec.ports:
        args.extend(["-p", f"{host}:{container}"])
    for key, value in spec.env:
        args.extend(["-e", f"{key}={value}"])
    for source, container_path in spec.volumes:
        args.extend(["-v", f"{source}:{container_path}"])
    args.append(spec.image)
    args.extend(spec.command)
    return args


__all__ = [
    "APECX_REDIS",
    "APECX_RHEA_MINIO",
    "APECX_RHEA_POSTGRES",
    "all_container_specs",
    "container_run_args",
]
