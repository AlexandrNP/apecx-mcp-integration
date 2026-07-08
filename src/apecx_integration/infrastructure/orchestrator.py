"""The process-singleton orchestrator that brings the stack up.

Lifecycle
---------
1. ``apecx_integration.mcp_surface.server.build_server`` schedules
   ``get_orchestrator().start_all()`` as a fire-and-forget asyncio
   task. The MCP-tool-surface registration finishes immediately so
   Claude Desktop sees the server respond fast; the orchestrator
   races in the background.
2. Per backend, the orchestrator runs the probe. If it succeeds,
   the backend transitions to ``REUSED`` (we did not spawn it) and
   we keep its handle. If it fails AND ``APECX_MCP_AUTOSTART_INFRA``
   is set, we attempt to bring it up — ``docker run`` for containers —
   then poll the probe until healthy or the timeout fires.
3. The ``infrastructure_status`` MCP tool reads ``status()`` on each
   call, which RE-PROBES every ready backend (cheap; <50 ms each).
   This way a backend that died after startup is reported as
   ``DEGRADED`` immediately, never as stale-green.
4. ``atexit`` invokes ``shutdown()``. That tears down ONLY containers
   / processes the orchestrator spawned (tracked via the ``_spawned``
   set on each :class:`BackendRuntime`). Operator-pre-existing
   containers + processes survive — they may want them persistent.

Concurrency model
-----------------
``start_all()`` launches per-backend tasks via ``asyncio.gather``.
Each backend's bring-up is serial within itself (probe → spawn →
poll). The ``_lock`` guards ``BackendRuntime`` mutation so a status
re-probe racing against an in-flight bring-up doesn't corrupt state.

Honesty contract
----------------
* The status tool ALWAYS re-probes ready backends. We never return
  stale green.
* A probe failure on a previously-ready backend flips it to
  ``DEGRADED`` (not ``DOWN``) — the next status call re-probes; the
  failure may have been a transient network blip.
* Idempotence: ``start_all()`` called twice does not double-start.
  Each backend runtime tracks whether it's been processed; subsequent
  calls only re-probe.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlparse

from apecx_integration.infrastructure.backends import (
    BackendRuntime,
    BackendSpec,
    BackendState,
    ContainerSpec,
    Probe,
    ProbeResult,
)
from apecx_integration.infrastructure.containers import (
    APECX_OLLAMA,
    APECX_REDIS,
    APECX_RHEA_MINIO,
    APECX_RHEA_POSTGRES,
    container_run_args,
)
from apecx_integration.infrastructure.probes import (
    minio_probe,
    ollama_probe,
    postgres_probe,
    redis_probe,
    rhea_mcp_probe,
)
from apecx_integration.infrastructure.rhea_server_provisioner import (
    ensure_rhea_image_built,
    resolve_rhea_image_tag,
)

log = logging.getLogger(__name__)


_AUTOSTART_ENV_VAR = "APECX_MCP_AUTOSTART_INFRA"
# Optional embedding-model name for Rhea (used by the ToolShed catalog
# embedding step). 1024-dim default matches the Ollama mxbai-embed-large
# model bundled by apecx-setup; Rhea's bare default is for an HF
# embedding image that nobody on macOS actually runs.
_RHEA_EMBEDDING_MODEL_ENV = "RHEA_EMBEDDING_MODEL"
_OLLAMA_BASE_URL_ENV = "APECX_LLM_BASE_URL"
_RHEA_MCP_URL_ENV = "RHEA_MCP_URL"
# Which tool set the auto-ingestion seeds into the rhea catalog when it is empty.
# Default "muscle" = the fast (~10s) zero-config path; an operator can widen it
# (e.g. "muscle,blast") to seed more tools, at the cost of a slower first boot.
_RHEA_INGEST_ONLY_ENV = "APECX_RHEA_INGEST_ONLY"
# Image tag the container backend runs. The orchestrator AUTO-BUILDS this from the local
# rhea source before `docker run` (the container spec's `image_builder` hook) — no
# `apecx-setup rhea` needed. The tag is resolved via `resolve_rhea_image_tag()` (single
# source of truth, shared with the builder) so the tag we RUN is always the tag it BUILDS.
# The rhea-server ALWAYS runs as a Docker container — no host-process alternative.


def _autostart_enabled() -> bool:
    return os.environ.get(_AUTOSTART_ENV_VAR, "1") != "0"


# ---------------------------------------------------------------------------
# Default backend roster
# ---------------------------------------------------------------------------


def _make_postgres_spec() -> BackendSpec:
    container = APECX_RHEA_POSTGRES
    host = "localhost"
    host_port = container.ports[0][0]
    env = dict(container.env)
    user = "postgres"  # pgvector image default
    db = env.get("POSTGRES_DB", "postgres")
    password = env.get("POSTGRES_PASSWORD", "postgres")

    async def _probe() -> ProbeResult:
        return await postgres_probe(host=host, port=host_port, user=user, db=db, password=password)

    return BackendSpec(
        name="postgres",
        display_name="Postgres (apecx-rhea-postgres / pgvector)",
        kind="docker_container",
        required=True,
        probe=Probe(name="postgres", fn=_probe),
        actionable_message=(
            "Postgres is unreachable at localhost:5435. The container "
            f"image is {container.image!r}. If Docker is installed, the "
            "orchestrator can spawn it; otherwise install Docker Desktop "
            "from https://www.docker.com/products/docker-desktop/ and "
            f"start it, then re-run. Manual recovery: docker start "
            f"{container.container_name}."
        ),
        container=container,
        tags=("vector-store", "rhea-deps"),
    )


def _make_redis_spec() -> BackendSpec:
    container = APECX_REDIS
    host = "localhost"
    host_port = container.ports[0][0]

    async def _probe() -> ProbeResult:
        return await redis_probe(host=host, port=host_port)

    return BackendSpec(
        name="redis",
        display_name="Redis (apecx-redis)",
        kind="docker_container",
        required=True,
        probe=Probe(name="redis", fn=_probe),
        actionable_message=(
            "Redis is unreachable at localhost:6379. If Docker is "
            "available, the orchestrator can spawn it; otherwise install "
            "Docker Desktop from https://www.docker.com/products/docker-desktop/ "
            f"and start it. Manual recovery: docker start "
            f"{container.container_name}."
        ),
        container=container,
        tags=("cache", "task-queue"),
    )


def _make_minio_spec() -> BackendSpec:
    container = APECX_RHEA_MINIO
    host = "localhost"
    host_port = container.ports[0][0]

    async def _probe() -> ProbeResult:
        return await minio_probe(host=host, port=host_port)

    return BackendSpec(
        name="minio",
        display_name="MinIO (apecx-rhea-minio)",
        kind="docker_container",
        required=True,
        probe=Probe(name="minio", fn=_probe),
        actionable_message=(
            "MinIO is unreachable at localhost:9000. If Docker is "
            "available, the orchestrator can spawn it; otherwise install "
            "Docker Desktop from https://www.docker.com/products/docker-desktop/ "
            f"and start it. Manual recovery: docker start "
            f"{container.container_name}."
        ),
        container=container,
        tags=("object-store", "rhea-deps"),
    )


def _make_ollama_spec() -> BackendSpec:
    base_url = os.environ.get(_OLLAMA_BASE_URL_ENV, "http://localhost:11434/v1")

    async def _probe() -> ProbeResult:
        # Model-aware readiness (#7): require the model the synthesis runtime resolves to, so a
        # reachable-but-model-less Ollama reads DEGRADED, not ready. Lazy import + per-probe resolve
        # so an env change (APECX_LLM_MODEL) is reflected without a restart.
        from apecx_integration.agents._llm_config import resolve_llm_model

        return await ollama_probe(base_url=base_url, required_model=resolve_llm_model())

    # Local endpoint → the orchestrator manages Ollama as a CONTAINER (#7 default — no manual host
    # install). A REMOTE APECX_LLM_BASE_URL (operator points at their own Ollama elsewhere) stays
    # probe-only external — we never containerize someone else's endpoint. The model is provisioned
    # separately by `apecx-setup llm` (container-aware pull); a reachable-but-model-less container
    # reads DEGRADED via the model-aware probe until then.
    hostname = urlparse(base_url).hostname
    if hostname in (None, "localhost", "127.0.0.1", "0.0.0.0"):
        return BackendSpec(
            name="ollama",
            display_name="Ollama (container)",
            kind="docker_container",
            required=True,
            probe=Probe(name="ollama", fn=_probe),
            container=APECX_OLLAMA,
            actionable_message=(
                "The apecx-ollama container is not running or has no model. Ensure Docker is "
                "running, then provision the model with `apecx-setup llm` (pulls the configured "
                "model into the apecx-ollama container). A reachable container with no model reads "
                "DEGRADED until the model is pulled."
            ),
            tags=("llm", "container"),
        )
    return BackendSpec(
        name="ollama",
        display_name="Ollama (remote)",
        kind="external",
        required=True,
        probe=Probe(name="ollama", fn=_probe),
        actionable_message=(
            f"Ollama not reachable at the configured remote endpoint {base_url}. Verify "
            "APECX_LLM_BASE_URL and that the remote Ollama is running with the configured model "
            "pulled. The orchestrator does not manage a remote endpoint — it is operator-owned."
        ),
        tags=("llm", "remote"),
    )


def _compose_rhea_env(
    *,
    postgres: ContainerSpec,
    redis_c: ContainerSpec,
    minio: ContainerSpec,
    ollama_base_url: str,
    infra_host: str = "localhost",
) -> dict[str, str]:
    """Derive the env vars Rhea needs, from the orchestrator's own specs.

    Rhea's ``Settings`` defaults DO NOT match the apecx-stack ports /
    Ollama / Parsl-on-macOS reality. If we just ``Popen`` rhea-server
    with default Settings, its MCP transport answers ``tools/list`` —
    making the probe go green — but every actual tool call fails
    because postgres is at the wrong port, the embedding URL points at
    a nonexistent TEI server, etc. That is the canonical silent-failure
    shape this orchestrator exists to refuse. We derive the right env
    from our backend specs so the spawn produces a *working* Rhea.
    """
    pg_host_port = postgres.ports[0][0]
    pg_env = dict(postgres.env)
    db_user = "postgres"
    db_name = pg_env.get("POSTGRES_DB", "rhea")
    db_password = pg_env.get("POSTGRES_PASSWORD", "postgres")
    database_url = (
        f"postgresql+asyncpg://{db_user}:{db_password}@{infra_host}:{pg_host_port}/{db_name}"
    )
    redis_host_port = redis_c.ports[0][0]
    minio_host_port = minio.ports[0][0]
    minio_env = dict(minio.env)
    # Ollama base URL: Rhea uses an OpenAI-compatible /v1 endpoint;
    # APECX_LLM_BASE_URL may already carry /v1 or may not.
    embedding_url = ollama_base_url
    if not embedding_url.rstrip("/").endswith("/v1"):
        embedding_url = embedding_url.rstrip("/") + "/v1"
    embedding_model = os.environ.get(_RHEA_EMBEDDING_MODEL_ENV, "mxbai-embed-large")
    # Rhea's serve port follows $RHEA_MCP_URL (which apecx-mcp derives from the config's rhea.host/
    # port), so the spawned server listens where the probe + workflow consumers expect it. Defaults
    # to 3001 — the common path is unchanged. The container variant overrides HOST to 0.0.0.0.
    rhea_serve_port = (
        urlparse(os.environ.get(_RHEA_MCP_URL_ENV, "http://localhost:3001/mcp/")).port or 3001
    )
    return {
        # Server bind (Rhea's own MCP host:port — not the upstream MCP URL).
        "HOST": "localhost",
        "PORT": str(rhea_serve_port),
        # DB / object store / cache.
        "DATABASE_URL": database_url,
        "REDIS_HOST": infra_host,
        "REDIS_PORT": str(redis_host_port),
        "AGENT_REDIS_HOST": infra_host,
        "AGENT_REDIS_PORT": str(redis_host_port),
        "MINIO_ENDPOINT": f"{infra_host}:{minio_host_port}",
        "MINIO_ACCESS_KEY": minio_env.get("MINIO_ROOT_USER", "minioadmin"),
        "MINIO_SECRET_KEY": minio_env.get("MINIO_ROOT_PASSWORD", "minioadmin"),
        # Embedding service (Ollama).
        "EMBEDDING_URL": embedding_url,
        "EMBEDDING_KEY": "EMPTY",
        "MODEL": embedding_model,
        # Parsl on macOS: the Docker-sibling worker can't reach the
        # interchange; force the local-process backend. The operator
        # can override via the env.
        "PARSL_CONTAINER_BACKEND": os.environ.get("PARSL_CONTAINER_BACKEND", "local"),
        # Rhea's tool actor unpacks the conda-pack archive from Redis
        # to a host filesystem path. The default (/home/rhea/conda/envs)
        # is a Linux container path that's structurally inaccessible on
        # macOS (/home is an autofs read-only mount) — writing there
        # raises PermissionError, agent_on_startup raises, the Academy
        # actor wedges, and every subsequent run_tool returns "Action
        # 'run_tool' was cancelled by the agent" for the rest of the
        # rhea-server's lifetime. We pin it to a writable scratch
        # directory under the operator's home so the unpack succeeds
        # cleanly. ~/.cache/apecx-rhea/conda/envs survives reboots,
        # is XDG-compliant, and stays out of $TMPDIR (which some
        # macOS cleanup tools wipe aggressively, defeating the cache).
        # Operator can override (e.g. point at a faster SSD scratch
        # mount) via the env var.
        "RHEA_CONDA_ENVS_DIR": os.environ.get(
            "RHEA_CONDA_ENVS_DIR",
            os.path.expanduser("~/.cache/apecx-rhea/conda/envs"),
        ),
    }


def _compose_rhea_container_env(
    *,
    postgres: ContainerSpec,
    redis_c: ContainerSpec,
    minio: ContainerSpec,
    ollama_base_url: str,
) -> dict[str, str]:
    """Rhea env for the CONTAINER backend.

    Same derivation as the host-process env, with three container-specific
    differences:
      * every infra endpoint is reached via ``host.docker.internal`` (the
        container talks to the host-published postgres/redis/minio/ollama ports)
        instead of ``localhost``;
      * the server binds ``0.0.0.0`` so ``-p 3001:3001`` is reachable from the
        host (the image bakes this too — set here for belt-and-braces);
      * ``AGENT_HANDLE_TIMEOUT`` is raised: the first call to a tool cold-builds
        its per-tool conda env INSIDE the container and the agent handle is
        written only after that build finishes, so the 30s default would time
        out with the opaque "Never received handle from Parsl worker".
    The per-tool conda dir is left to the image's baked ``RHEA_CONDA_ENVS_DIR``
    (``/opt/rhea-conda/envs``), NOT the macOS host-cache path the host-process
    variant pins — that path does not exist inside the container.
    """
    # Ollama runs on the HOST; rewrite a localhost/127.0.0.1 base URL to the
    # docker-desktop host alias so the container can reach it.
    container_ollama = ollama_base_url.replace("localhost", "host.docker.internal").replace(
        "127.0.0.1", "host.docker.internal"
    )
    env = _compose_rhea_env(
        postgres=postgres,
        redis_c=redis_c,
        minio=minio,
        ollama_base_url=container_ollama,
        infra_host="host.docker.internal",
    )
    env["HOST"] = "0.0.0.0"
    env["AGENT_HANDLE_TIMEOUT"] = os.environ.get("AGENT_HANDLE_TIMEOUT", "900")
    # Use the image's baked RHEA_CONDA_ENVS_DIR (/opt/rhea-conda/envs) by default
    # — the host-process variant's ~/.cache path does not exist in the container.
    # But HONOR an explicit operator override (e.g. a mounted persistent
    # conda-cache volume) rather than silently dropping it.
    if os.environ.get("RHEA_CONDA_ENVS_DIR"):
        env["RHEA_CONDA_ENVS_DIR"] = os.environ["RHEA_CONDA_ENVS_DIR"]
    else:
        env.pop("RHEA_CONDA_ENVS_DIR", None)
    return env


def _make_rhea_container_spec(
    *,
    postgres_container: ContainerSpec,
    redis_container: ContainerSpec,
    minio_container: ContainerSpec,
    ollama_base_url: str,
) -> BackendSpec:
    """Rhea backend that runs the rhea-server DOCKER IMAGE (host-conda-independent).

    Tool execution + per-tool conda envs build INSIDE the container using its
    baked conda, so a broken/missing HOST conda (the canonical Apple-Silicon
    failure) is irrelevant. The worker is a Parsl LOCAL subprocess inside the
    container — it shares the server's network namespace, so there is no
    Docker-Desktop interchange-reachability problem (the reason the sibling
    container WORKER backend fails on macOS).

    The orchestrator AUTO-BUILDS the image from the local rhea source before
    ``docker run`` (the container spec's ``image_builder`` hook =
    :func:`ensure_rhea_image_built`), so rhea is zero-config — no
    ``apecx-setup rhea`` build step. A missing/unbuildable source surfaces a LOUD
    ERROR_STARTING with the cause. Reaching the host-published infra ports uses
    ``extra_hosts=("host.docker.internal:host-gateway",)`` (needed on Linux; a
    no-op but harmless on Docker Desktop).
    """
    mcp_url = os.environ.get(_RHEA_MCP_URL_ENV, "http://localhost:3001/mcp/")
    image = resolve_rhea_image_tag()

    async def _probe() -> ProbeResult:
        return await rhea_mcp_probe(mcp_url=mcp_url)

    rhea_env = _compose_rhea_container_env(
        postgres=postgres_container,
        redis_c=redis_container,
        minio=minio_container,
        ollama_base_url=ollama_base_url,
    )
    container_spec = ContainerSpec(
        image=image,
        container_name="apecx-rhea-server",
        ports=((3001, 3001),),
        # Sorted for a deterministic argv (tests pin the generated docker run).
        env=tuple(sorted(rhea_env.items())),
        extra_hosts=("host.docker.internal:host-gateway",),
        # Auto-build the image from local rhea source before `docker run` (zero-config).
        image_builder=ensure_rhea_image_built,
        # The server boots (probe = :3001 health) in ~10s; the slow per-tool
        # conda build happens later, on the first tool CALL, not at startup.
        ready_timeout_s=120.0,
        # Docker auto-restarts rhea-server across host reboots without anything
        # relaunching apecx-mcp; also marks it long-lived (never teardown-tracked).
        restart="unless-stopped",
    )

    return BackendSpec(
        name="rhea_mcp",
        display_name="Rhea MCP (container)",
        kind="docker_container",
        required=True,
        probe=Probe(name="rhea_mcp", fn=_probe),
        actionable_message=(
            f"Rhea MCP container is unreachable at {mcp_url}. The orchestrator "
            f"auto-builds the image {image!r} from your local rhea source and runs "
            f"it — no `apecx-setup rhea` build step. If this persists: make sure "
            f"Docker is running and a rhea source checkout is present (set "
            f"RHEA_REPO_PATH if it lives in a nonstandard location), then retry."
        ),
        container=container_spec,
        tags=("mcp", "rhea"),
    )


def _default_backend_specs() -> tuple[BackendSpec, ...]:
    """Build the default 5-backend roster.

    The Rhea spec is composed *from* the postgres/redis/minio/ollama
    specs so its spawned environment exactly matches those backends'
    actual host:port/credentials. Single source of truth — if the
    postgres host port moves, Rhea's DATABASE_URL moves with it.
    """
    pg = _make_postgres_spec()
    redis_s = _make_redis_spec()
    minio_s = _make_minio_spec()
    ollama_s = _make_ollama_spec()
    ollama_base_url = os.environ.get(_OLLAMA_BASE_URL_ENV, "http://localhost:11434/v1")
    # rhea-server ALWAYS runs as a Docker container — the single, host-conda-
    # independent path (there is no host-process alternative).
    rhea = _make_rhea_container_spec(
        postgres_container=pg.container,  # type: ignore[arg-type]
        redis_container=redis_s.container,  # type: ignore[arg-type]
        minio_container=minio_s.container,  # type: ignore[arg-type]
        ollama_base_url=ollama_base_url,
    )
    return (pg, redis_s, minio_s, ollama_s, rhea)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


# Minimum seconds between reconcile() RE-ATTEMPTS of a stuck docker backend (the expensive
# `docker info` + start_all path). Tool calls invoke reconcile() freely; this bounds the retry
# rate so a persistently-down Docker daemon isn't probed on every single call.
_RECONCILE_THROTTLE_S = 15.0


class InfraOrchestrator:
    """Process-singleton infra orchestrator.

    Construction takes a list of :class:`BackendSpec` (defaults to the
    canonical 5-backend roster) and an optional ``autostart_enabled``
    override (defaults to reading ``APECX_MCP_AUTOSTART_INFRA``). The
    orchestrator never reads the env var post-construction — once
    instantiated, its policy is fixed (test predictability).

    The orchestrator is async-friendly but synchronous-safe to
    construct. ``start_all()`` must be ``await``-ed.
    """

    def __init__(
        self,
        specs: list[BackendSpec] | None = None,
        *,
        autostart_enabled: bool | None = None,
        docker_binary: str | None = None,
    ) -> None:
        roster = specs if specs is not None else list(_default_backend_specs())
        self._runtimes: dict[str, BackendRuntime] = {
            spec.name: BackendRuntime(spec=spec) for spec in roster
        }
        self._autostart = (
            autostart_enabled if autostart_enabled is not None else _autostart_enabled()
        )
        # The docker binary is resolved at construction so a missing
        # docker daemon is reported once, not every probe cycle.
        self._docker = docker_binary if docker_binary is not None else shutil.which("docker")
        # ``threading.Lock`` (not ``asyncio.Lock``) so the orchestrator
        # can be touched from any thread / loop. The status tool runs
        # in FastMCP's loop; ``start_all`` may be driven from a separate
        # bring-up thread. The lock-held regions are tiny (in-memory
        # field assignment) so a sync lock is the right tool.
        self._lock = threading.Lock()
        self._singleton_loop: asyncio.AbstractEventLoop | None = None
        self._started_at: float | None = None
        self._start_all_done = False
        # Populated by ``prewarm_workflow_tools()`` after start_all
        # completes. The status tool surfaces this so an operator
        # diagnosing slow first-call latency or wedged Rhea actor
        # state can see which tools are pre-installed.
        self._prewarm_report: Any | None = None
        # Track spawned containers for atexit cleanup. We hold the names
        # directly so atexit can still reach them even if all other
        # references go out of scope. Restart-policy containers are
        # Docker-lifecycle-owned and deliberately NOT enrolled here.
        self._spawned_containers: list[str] = []
        self._atexit_registered = False
        # Monotonic stamp of the last reconcile() that PASSED the stuck-scan and entered the
        # re-detection path (binary re-resolve + `docker info` + start_all) — set even when the
        # daemon turns out still-down, so a down/absent daemon is probed at most once per
        # _RECONCILE_THROTTLE_S, not on every tool call. The cheap "nothing stuck" scan that
        # precedes it is never throttled (it early-returns before this stamp is read).
        self._last_reconcile_at: float = 0.0

    # ---- public API ---------------------------------------------------

    @property
    def autostart_enabled(self) -> bool:
        return self._autostart

    @property
    def uptime_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    def backend_names(self) -> list[str]:
        return list(self._runtimes.keys())

    def get_runtime(self, name: str) -> BackendRuntime:
        return self._runtimes[name]

    async def start_all(self) -> dict[str, Any]:
        """Bring up (or probe-only) every backend in parallel.

        Returns a dict snapshot like :meth:`status` for caller convenience.
        Idempotent: subsequent calls only re-probe — no double-spawn.
        """
        if self._started_at is None:
            self._started_at = time.monotonic()
        # Register the atexit hook on first call. We never unregister.
        if not self._atexit_registered:
            atexit.register(self._atexit_shutdown)
            self._atexit_registered = True

        # Launch per-backend bring-up in parallel. We use return_exceptions
        # so a buggy probe (which would be a programmer bug — probes are
        # supposed to catch their own exceptions) doesn't take down sibling
        # bring-ups.
        await asyncio.gather(
            *(self._bring_up(rt) for rt in self._runtimes.values()),
            return_exceptions=True,
        )
        self._start_all_done = True
        return await self.status()

    async def reload_backend(self, name: str) -> dict[str, Any]:
        """Re-establish ONE backend by name (probe → reuse-if-healthy → else spawn/restart) — the
        per-component seam the dashboard monitor calls to auto-recover a single failed backend.

        Unlike :meth:`reconcile` (docker-only, whole-roster, throttled), this targets ONE backend of
        ANY kind. It applies no backoff itself — the caller (the monitor) owns the retry cadence — so
        a tool/operator can force an immediate re-establish. Returns the backend's post-attempt
        snapshot (same shape as one entry of :meth:`status`'s ``backends``). Raises ``KeyError`` for
        an unknown name."""
        rt = self._runtimes[name]
        await self._bring_up(rt)
        with self._lock:
            return rt.snapshot()

    async def reconcile(self) -> dict[str, Any]:
        """Self-heal docker-dependent backends when Docker comes up AFTER startup.

        Docker is detected once at construction + once per backend during the initial
        ``start_all()``; if the user starts the daemon LATER, the docker backends
        (postgres/redis/minio → RHEA) would otherwise stay stuck forever. This is the
        seam tool calls invoke to re-attempt bring-up when Docker became available.

        Cheap on the happy path: the stuck-scan (a list-comp over the runtimes) runs FIRST
        and early-returns, so a healthy server pays nothing — no ``shutil.which``, no
        ``docker info``. Only when a docker backend is actually stuck does it pay the
        re-detection cost, and that whole path is throttled (``_RECONCILE_THROTTLE_S``) so a
        persistently-down/absent daemon isn't probed on every call. ``start_all`` is
        idempotent (re-probes healthy backends, re-attempts the stuck ones). Every backend
        here is a docker container (RHEA included), so a stuck RHEA triggers reconcile
        directly — this heals the Docker-came-up-late case.
        """
        # Cheap stuck-scan FIRST — a docker backend NOT in a healthy/in-flight state. The
        # happy path returns here, before any shutil.which / docker info / time call.
        healthy = {BackendState.READY, BackendState.REUSED, BackendState.STARTING}
        stuck = [
            rt
            for rt in self._runtimes.values()
            if rt.spec.kind == "docker_container" and rt.state not in healthy
        ]
        if not stuck:
            return {"reattempted": []}
        # Something is stuck — throttle the whole re-attempt (binary re-resolve + docker info +
        # start_all) so a down/absent daemon isn't probed every call.
        now = time.monotonic()
        if now - self._last_reconcile_at < _RECONCILE_THROTTLE_S:
            return {"reattempted": [], "throttled": True}
        self._last_reconcile_at = now
        if not self._docker:
            self._docker = shutil.which("docker")  # may have been installed since construction
        if not self._docker:
            return {"reattempted": []}
        # Is the daemon actually up now? (the one-shot startup check may have run while it was down)
        info = await asyncio.to_thread(
            subprocess.run, [self._docker, "info"], capture_output=True, timeout=10
        )
        if info.returncode != 0:
            return {"reattempted": []}  # daemon still down
        reattempting = [rt.spec.name for rt in stuck]
        log.info("InfraOrchestrator.reconcile: Docker now up; re-attempting %s", reattempting)
        await self.start_all()
        return {"reattempted": reattempting}

    async def status(self) -> dict[str, Any]:
        """Snapshot of every backend's current state.

        Re-probes every backend currently in READY / REUSED state with
        a short timeout. Backends still in STARTING are not re-probed
        (bring-up is in flight). Operator-prereq states
        (EXTERNAL_MISSING / EXTERNAL_UNCONFIGURED / EXTERNAL_SKIPPED)
        are NOT re-probed automatically — they need an operator action
        to change and constant re-probing would be pointless cost.
        """
        # Re-probe any backend whose state is one we re-probe on every
        # status call. We run these in parallel; mutation is guarded by
        # _lock per backend.
        coros = []
        for rt in self._runtimes.values():
            if rt.state in (BackendState.READY, BackendState.REUSED, BackendState.DEGRADED):
                coros.append(self._reprobe(rt))
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

        backends = [rt.snapshot() for rt in self._runtimes.values()]
        overall = self._compute_overall_state()
        actionable = self._actionable_messages()
        snapshot: dict[str, Any] = {
            "overall": overall,
            "autostart_enabled": self._autostart,
            "orchestrator_uptime_seconds": self.uptime_seconds,
            "start_all_completed": self._start_all_done,
            "backends": backends,
            "actionable": actionable,
        }
        if self._prewarm_report is not None:
            snapshot["rhea_tool_prewarm"] = self._prewarm_report.snapshot()
            # Lift any per-tool failure into the top-level actionable
            # list so a wedged pre-warm shows up alongside the backend
            # actionables. A pre-warm failure does NOT flip ``overall``
            # to ``down`` — the workflow's UNAVAILABLE marker is the
            # right surface for per-tool problems; ``overall`` is for
            # cross-cutting backend state.
            for tool_result in self._prewarm_report.tools:
                if tool_result.state == "failed":
                    snapshot["actionable"].append(
                        f"[prewarm:{tool_result.tool_name}] {tool_result.detail}"
                    )
        return snapshot

    async def ensure_catalog_seeded(self, *, timeout_s: float = 600.0) -> dict[str, Any]:
        """Auto-run the Rhea tool-catalog ingestion when the catalog is empty.

        The rhea-server container auto-builds + runs (zero-config), but its
        EXTERNAL postgres carries the tool catalog. On an unseeded machine that
        catalog is empty, so the server answers ``tools/list`` with ZERO tools —
        reachable but degraded — and every rhea tool is unavailable until an
        operator runs ``apecx-setup rhea`` ingestion. This method closes that
        gap: it detects the empty-catalog state via the rhea probe and, only
        then, runs the ingestion INSIDE the running container, so rhea works
        after nothing but ``uv install`` + ``apecx-setup``.

        Idempotent + safe to call repeatedly:
          * ``docker exec ... update_tools`` is an upsert (``session.merge``),
            so a re-run on an already-seeded catalog is a no-op.
          * We probe FIRST and return ``already_seeded`` without touching Docker
            when the catalog already has tools.

        Returns a small dict describing what happened (never raises for the
        no-rhea / no-docker / server-down cases — those are surfaced by the
        backend state machine, not by an exception here). It DOES raise on an
        ingestion FAILURE (non-zero exit / timeout) — that is a real, actionable
        problem the caller must not swallow silently.
        """
        rt = self._runtimes.get("rhea_mcp")
        if rt is None or rt.spec.container is None:
            return {"seeded": False, "action": "skipped", "reason": "no rhea backend in roster"}
        if self._docker is None:
            return {"seeded": False, "action": "skipped", "reason": "docker CLI not on PATH"}
        container_name = rt.spec.container.container_name

        # Detect seeded state via the postgres CATALOG ROW COUNT — NOT the MCP
        # tools/list count. The rhea MCP tools/list ALWAYS lists ``find_tools``
        # (the discovery meta-tool, count 1) at startup; catalog tool-wrappers are
        # added dynamically only AFTER find_tools runs — so tools/list cannot tell
        # an empty catalog from a seeded one (both read "1 tool"). The
        # ``galaxytools`` row count is the ground truth. (An integration test that
        # truncated the catalog caught the probe-based check wrongly reporting
        # "already_seeded" and skipping the ingest.)
        n_tools = await self._catalog_row_count()
        if n_tools is None:
            return {
                "seeded": False,
                "action": "skipped",
                "reason": "no postgres backend in roster / catalog count unavailable",
            }
        if n_tools > 0:
            return {
                "seeded": True,
                "action": "already_seeded",
                "detail": f"{n_tools} tool(s) in catalog",
            }

        # Reachable but empty → run the ingestion inside the container.
        ingest_only = os.environ.get(_RHEA_INGEST_ONLY_ENV, "muscle")
        cmd = [
            self._docker,
            "exec",
            "-e",
            f"RHEA_INGEST_ONLY={ingest_only}",
            container_name,
            "sh",
            "-lc",
            "cd /app && uv run python -m rhea.preprocess.update_tools",
        ]
        log.info(
            "InfraOrchestrator.ensure_catalog_seeded: rhea catalog empty — running ingestion "
            "(RHEA_INGEST_ONLY=%s) in %s ...",
            ingest_only,
            container_name,
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Rhea catalog ingestion timed out after {timeout_s}s "
                f"(RHEA_INGEST_ONLY={ingest_only}). A default muscle-only ingest is normally "
                "~10s; a timeout usually means a widened ingest set or a stalled dependency. "
                "Check: (1) Ollama + the mxbai-embed-large embedding model reachable from the "
                "container; (2) network to Galaxy ToolShed / GitHub for the tool source."
            ) from exc

        stdout = result.stdout.decode("utf-8", "replace") if result.stdout else ""
        stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
        # Log the ingestion output so a slow widen-ingest is observable, not a stall.
        for line in stdout.splitlines():
            log.info("[rhea-ingest] %s", line)

        if result.returncode != 0:
            raise RuntimeError(
                f"Rhea catalog ingestion FAILED (exit {result.returncode}, "
                f"RHEA_INGEST_ONLY={ingest_only}). Likely causes: (1) Ollama + the "
                "mxbai-embed-large embedding model reachable from the container? "
                "(2) network to Galaxy ToolShed / GitHub for the tool source? "
                f"(3) rhea source built into the image behind {container_name!r}? "
                f"stderr tail: {stderr[-500:]!r}"
            )

        # Re-count so the returned detail reflects the freshly-seeded catalog
        # (ground truth, same reason as the detection above — not the MCP probe).
        seeded_count = await self._catalog_row_count()
        return {
            "seeded": bool(seeded_count and seeded_count > 0),
            "action": "ingested",
            "ingest_only": ingest_only,
            "detail": f"{seeded_count} tool(s) in catalog after ingestion",
        }

    async def _catalog_row_count(self) -> int | None:
        """Row count of the rhea ``galaxytools`` catalog table — the seeded-state ground truth.

        Runs ``psql ... SELECT COUNT(*) FROM galaxytools`` inside the postgres container (the
        rhea catalog DB). Returns the count; 0 when the table is absent (fresh postgres, before
        the first ingest) or the query is otherwise non-numeric; None when there is no postgres
        backend or no docker CLI (caller treats None as "can't tell — skip").
        """
        pg_rt = self._runtimes.get("postgres")
        if pg_rt is None or pg_rt.spec.container is None or self._docker is None:
            return None
        # DB name matches _compose_rhea_container_env's DATABASE_URL (default "rhea").
        db_name = dict(pg_rt.spec.container.env or ()).get("POSTGRES_DB", "rhea")
        cmd = [
            self._docker,
            "exec",
            pg_rt.spec.container.container_name,
            "psql",
            "-U",
            "postgres",
            "-d",
            db_name,
            "-tAc",
            "SELECT COUNT(*) FROM galaxytools;",
        ]
        try:
            res = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            return None
        out = (res.stdout or b"").decode("utf-8", "replace").strip()
        if res.returncode == 0:
            try:
                return int(out)
            except ValueError:
                return 0
        # Non-zero exit: distinguish a genuinely-unseeded catalog (the table hasn't
        # been created yet — psql: relation "galaxytools" does not exist → 0, ingest)
        # from a "can't tell" error (connection refused / auth / postgres still
        # starting → None, skip — same as the TimeoutExpired path above; a spurious
        # ingest on a transient postgres error would be wasteful, not just harmless).
        stderr = (res.stderr or b"").decode("utf-8", "replace").lower()
        if "does not exist" in stderr:
            return 0
        return None

    async def prewarm_workflow_tools(self) -> None:
        """Pre-install every Rhea tool conda env declared by the catalog.

        Runs AFTER ``start_all()`` so Rhea is already up + reachable.
        Drives the nanobrain pre-warm workflow at
        ``infrastructure/prewarm_workflow/configs/prewarm_workflow.yml``:
        three steps wired by DirectLinks
        (collect_tools → install_tools → aggregate_report) emitting a
        :class:`PrewarmReport` on the workflow's output DU. Result is
        stashed in ``self._prewarm_report`` and surfaced by
        ``status()``.

        Safe to call when the catalog declares no pre-warm tools — the
        workflow emits an empty PrewarmReport in that case. Idempotent
        within a process (Redis cache hit on second call → reused).

        Why a workflow instead of the older imperative driver:

        * The pipeline (catalog walk → per-tool install → aggregate
          report) is now visible as a nanobrain DAG, not buried inside
          a single Python function. Operators reading
          ``prewarm_workflow.yml`` see the three stages by name.
        * Future extensions (parallel install via ``ParallelStep``,
          retries via ``LoopController``, per-tool gating via
          ``ConditionalLink``) are expressible with first-class
          nanobrain primitives without touching this driver.
        * The orchestrator now drives pre-warm the same way it drives
          every other apecx workflow — ``Workflow.from_config(...)`` +
          ``initialize()`` + ``Workflow.run(...)`` (the canonical
          G8/G124/G125 entry point that drains the cascade and collects
          workflow outputs in one call). One pattern, one mental model.

        The pipeline's actual install/Postgres/Redis logic still lives
        in :mod:`rhea_prewarm` — the workflow steps are thin nanobrain
        wrappers around those helpers, so the per-tool semantics + the
        unit tests for the helpers remain authoritative.
        """
        # Lazy imports so the orchestrator without prewarm doesn't pull
        # nanobrain.core.workflow + the prewarm step classes into the
        # import graph until the pipeline actually runs.
        from pathlib import Path

        from nanobrain.core.workflow import Workflow

        # Resolve the database_url from the postgres backend spec so
        # the pre-warm uses the orchestrator's view of the world (no
        # config drift between Rhea's DATABASE_URL and what we tell
        # the pre-warm).
        pg_runtime = self._runtimes.get("postgres")
        if pg_runtime is None or pg_runtime.spec.container is None:
            log.warning("rhea_prewarm: no postgres backend in roster; skipping.")
            return
        pg_container = pg_runtime.spec.container
        pg_env = dict(pg_container.env)
        host_port = pg_container.ports[0][0]
        database_url = (
            f"postgresql://postgres:"
            f"{pg_env.get('POSTGRES_PASSWORD', 'postgres')}"
            f"@localhost:{host_port}/{pg_env.get('POSTGRES_DB', 'rhea')}"
        )
        redis_runtime = self._runtimes.get("redis")
        if redis_runtime is None or redis_runtime.spec.container is None:
            log.warning("rhea_prewarm: no redis backend in roster; skipping.")
            return
        redis_host_port = redis_runtime.spec.container.ports[0][0]

        # The catalog path can come from the env var override (operator
        # custom catalog) OR fall back to the packaged default — but
        # we DON'T pre-load the catalog here. CollectToolsStep does
        # that, keeping the load-and-walk logic inside the workflow.
        catalog_path = os.environ.get("APECX_MCP_WORKFLOW_CATALOG")
        prewarm_request = {
            "catalog_path": catalog_path,
            "database_url": database_url,
            "redis_host": "localhost",
            "redis_port": redis_host_port,
            "rhea_python": os.environ.get("RHEA_PYTHON_PATH"),
        }

        workflow_yaml = (
            Path(__file__).resolve().parent
            / "prewarm_workflow"
            / "configs"
            / "prewarm_workflow.yml"
        )
        try:
            workflow = Workflow.from_config(str(workflow_yaml))
        except Exception as exc:  # noqa: BLE001
            log.error(
                "rhea_prewarm: could not load workflow YAML at %s: %s",
                workflow_yaml,
                exc,
            )
            return

        # Phase 3 — resolve + bind step triggers. Without an explicit
        # initialize() the trigger graph is half-wired and the cascade
        # never fires (workspace-known pattern; see rag_e2e workflow-
        # yaml test for the same dance).
        await workflow.initialize()

        # Drive via the canonical ``Workflow.run`` (G8/G124/G125): it
        # invokes process(), awaits the cascade until quiet, and collects
        # the workflow-level output data units in one call — closing the
        # cascade-drain race that the older manual
        # ``process() + wait_for_cascade()`` pair was prone to. ``run``
        # does NOT call ``initialize()`` itself, so the explicit call above
        # stays. Per-tool install can be 30-90s each; the 1800s timeout
        # bounds a worst-case catalog (the install step's own
        # execution_timeout also caps any single hung install).
        outputs = await workflow.run(
            {"prewarm_request": prewarm_request},
            timeout=1800.0,
            settle_ms=500,
            raise_on_cascade_timeout=False,
        )
        if not isinstance(outputs, dict) or outputs.get("status") == "cascade_timeout":
            log.error(
                "rhea_prewarm: workflow cascade did not drain within 1800s — "
                "either a step hung or a DirectLink failed to transfer. "
                "Check the per-step trigger state."
            )
            return

        report = outputs.get("prewarm_report")
        if report is None:
            log.error(
                "rhea_prewarm: workflow drained but prewarm_report DU is "
                "empty — likely the aggregate_report step did not run, or "
                "its return key did not match the declared output_data_unit "
                "name 'prewarm_report'."
            )
            return

        with self._lock:
            self._prewarm_report = report

    async def shutdown(self) -> None:
        """Tear down ONLY containers we spawned.

        Spawned containers get ``docker stop`` (10s grace) followed by
        ``docker rm -f`` if stop fails. Pre-existing containers and
        restart-policy (Docker-lifecycle-owned) containers are never
        touched.
        """
        # Spawned containers.
        if self._docker is not None:
            for container_name in list(self._spawned_containers):
                try:
                    subprocess.run(
                        [self._docker, "stop", "-t", "10", container_name],
                        capture_output=True,
                        timeout=15,
                    )
                except subprocess.TimeoutExpired:
                    log.warning(
                        "InfraOrchestrator: `docker stop %s` timed out; forcing rm",
                        container_name,
                    )
                    subprocess.run(
                        [self._docker, "rm", "-f", container_name],
                        capture_output=True,
                        timeout=10,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "InfraOrchestrator: error stopping container %s: %s",
                        container_name,
                        exc,
                    )
        self._spawned_containers.clear()

    # ---- internals ----------------------------------------------------

    def _atexit_shutdown(self) -> None:
        """atexit hook — sync-only wrapper around ``shutdown``."""
        if self._docker is not None:
            for container_name in list(self._spawned_containers):
                try:
                    subprocess.run(
                        [self._docker, "stop", "-t", "10", container_name],
                        capture_output=True,
                        timeout=15,
                    )
                except Exception:  # noqa: BLE001
                    with contextlib.suppress(Exception):
                        subprocess.run(
                            [self._docker, "rm", "-f", container_name],
                            capture_output=True,
                            timeout=10,
                        )
        self._spawned_containers.clear()

    async def _bring_up(self, rt: BackendRuntime) -> None:
        """Bring up one backend (probe → optional spawn → poll)."""
        spec = rt.spec

        # First: probe. If healthy, mark REUSED and stop.
        first = await spec.probe.run()
        with self._lock:
            rt.last_probe_at = time.time()
            rt.last_latency_ms = first.latency_ms
            rt.reachable = first.reachable
            if first.healthy:
                rt.state = BackendState.REUSED
                rt.detail = first.detail
                rt.error = None
                rt.spawned_by_us = False
                return

        # Not healthy. Decide whether to attempt autostart.
        if not self._autostart:
            with self._lock:
                rt.state = BackendState.EXTERNAL_SKIPPED
                rt.detail = (
                    f"{spec.display_name}: not running and "
                    f"{_AUTOSTART_ENV_VAR}=0 — orchestrator is in probe-only mode."
                )
                rt.error = first.error
            return

        if spec.kind == "external":
            with self._lock:
                rt.state = BackendState.EXTERNAL_MISSING
                rt.detail = spec.actionable_message
                rt.error = first.error
            return

        if spec.kind == "docker_container":
            await self._bring_up_container(rt)
            return

        with self._lock:
            rt.state = BackendState.ERROR_STARTING
            rt.detail = f"unknown backend kind: {spec.kind}"
            rt.error = f"BackendSpec.kind={spec.kind!r}"

    async def _bring_up_container(self, rt: BackendRuntime) -> None:
        spec = rt.spec
        container_spec: ContainerSpec = spec.container  # type: ignore[assignment]
        if self._docker is None:
            with self._lock:
                rt.state = BackendState.EXTERNAL_MISSING
                rt.detail = (
                    f"{spec.display_name}: cannot autostart — `docker` CLI "
                    "not found on PATH. Install Docker Desktop from "
                    "https://www.docker.com/products/docker-desktop/ and "
                    "ensure the daemon is running."
                )
                rt.error = "docker binary not on PATH"
            return

        # Check daemon reachability — `docker info` returns non-zero
        # when the daemon is down. We report the same actionable msg.
        info = await asyncio.to_thread(
            subprocess.run,
            [self._docker, "info"],
            capture_output=True,
            timeout=10,
        )
        if info.returncode != 0:
            with self._lock:
                rt.state = BackendState.EXTERNAL_MISSING
                rt.detail = (
                    f"{spec.display_name}: docker daemon unreachable "
                    "(`docker info` returned non-zero). Start Docker "
                    "Desktop and re-run."
                )
                rt.error = info.stderr.decode("utf-8", "replace")[:300]
            return

        with self._lock:
            rt.state = BackendState.STARTING
            rt.detail = f"spawning container {container_spec.container_name} ..."

        # If a container with that name already exists (stopped), `docker start`
        # is the right move — it preserves the operator's volume state. If not,
        # we `docker run`.
        existing = await asyncio.to_thread(
            subprocess.run,
            [self._docker, "ps", "-aq", "-f", f"name=^{container_spec.container_name}$"],
            capture_output=True,
            timeout=10,
        )
        existing_id = existing.stdout.decode("utf-8", "replace").strip()
        if existing_id:
            spawn = await asyncio.to_thread(
                subprocess.run,
                [self._docker, "start", container_spec.container_name],
                capture_output=True,
                timeout=30,
            )
            spawn_action = "docker start"
        else:
            # Ensure a LOCAL image exists before `docker run` (e.g. build rhea-server from
            # source). Idempotent — a no-op when the image is already present. Only on the
            # `docker run` path; a `docker start` reuses an existing container's image.
            # A build failure is FAIL-LOUD (gather swallows exceptions), so we translate it
            # into ERROR_STARTING rather than let the backend hang in STARTING.
            if container_spec.image_builder is not None:
                # Deliberately UNBOUNDED (no timeout): a first-time rhea-server build is a
                # multi-minute `docker build`; a short cap would wrongly kill it. The builder
                # (ensure_docker_image_built) owns its own lifecycle + build-lock; `on_progress`
                # streams each build line to the log so a slow build is observable, not a stall.
                try:
                    await container_spec.image_builder(on_progress=log.info)
                except Exception as exc:  # noqa: BLE001 — surface any build failure loudly
                    with self._lock:
                        rt.state = BackendState.ERROR_STARTING
                        rt.detail = (
                            f"{spec.display_name}: image build failed before "
                            f"`docker run`. {spec.actionable_message}"
                        )
                        rt.error = f"{type(exc).__name__}: {exc}"[:300]
                    return
            cmd = [self._docker] + container_run_args(container_spec)
            spawn = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                timeout=120,
            )
            spawn_action = "docker run"

        if spawn.returncode != 0:
            err = spawn.stderr.decode("utf-8", "replace")[:300]
            with self._lock:
                rt.state = BackendState.ERROR_STARTING
                rt.detail = (
                    f"{spec.display_name}: `{spawn_action} "
                    f"{container_spec.container_name}` exited with "
                    f"{spawn.returncode}. {spec.actionable_message}"
                )
                rt.error = err
            return

        # Record what we spawned so atexit cleans it up. We register
        # both newly-run and previously-stopped containers as "spawned
        # by us" — we want to stop them on shutdown only if WE
        # transitioned them from stopped to running. EXCEPTION: a
        # restart-policy container (``restart != "no"``) is Docker-
        # lifecycle-owned — a ``docker stop`` on exit would cancel its
        # restart policy and defeat OS-reboot survival, so it is NOT
        # enrolled for teardown (see the exemption below).
        with self._lock:
            rt.spawned_by_us = True
            rt.spawned_container = container_spec.container_name
            # Anti-silent-failure: if we just CREATED a container from
            # scratch (vs starting a previously-stopped one), surface
            # an actionable warning. With named volumes declared in
            # ContainerSpec the data persists across respawns; without
            # them, or if the operator destroyed the volume, the
            # container starts empty — a probe-green container that
            # silently lost the operator's prior data is exactly the
            # silent-failure shape we're guarding against.
            if spawn_action == "docker run":
                vol_note = (
                    f"named volume(s) {[v[0] for v in container_spec.volumes]} "
                    f"declared — data persists if the volume exists"
                    if container_spec.volumes
                    else "NO persistent volume declared — data will be lost when the container is removed"
                )
                rt.fresh_create_warning = (
                    f"container {container_spec.container_name!r} was freshly "
                    f"created (the operator's prior container, if any, is gone). "
                    f"{vol_note}. Verify your expected state is present "
                    f"(e.g. `docker volume ls`, application-level row counts) "
                    f"before relying on this backend."
                )
        # Teardown exemption: only enroll for atexit ``docker stop`` when the
        # container has NO restart policy. A restart-policy container must
        # outlive apecx-mcp (and survive an OS reboot) — stopping it on exit
        # would cancel the policy, so Docker owns its lifecycle instead.
        if container_spec.restart == "no":
            self._spawned_containers.append(container_spec.container_name)
        else:
            log.info(
                "InfraOrchestrator: %s is restart-policy-managed (restart=%s) — "
                "Docker owns its lifecycle; not tracked for atexit teardown.",
                container_spec.container_name,
                container_spec.restart,
            )

        # Poll the probe until healthy or the per-spec timeout fires.
        ok = await self._poll_until_healthy(rt, container_spec.ready_timeout_s)
        if ok:
            with self._lock:
                rt.state = BackendState.READY
            return
        # Poll never went healthy. Distinguish a container that is UP but not fully provisioned
        # (reachable — e.g. the apecx-ollama container serving with no model pulled yet) from one
        # that failed to come up (unreachable). The former is DEGRADED + actionable (provision it via
        # `apecx-setup llm`), NOT ERROR_STARTING — mislabelling a live-but-unprovisioned container as
        # a start failure is the kind of dishonest state this orchestrator exists to avoid. (#7)
        final = await spec.probe.run()
        with self._lock:
            rt.reachable = final.reachable
            if final.reachable:
                rt.state = BackendState.DEGRADED
                rt.detail = f"{spec.display_name}: container up but not ready — {final.detail}"
                rt.error = final.error
            else:
                rt.state = BackendState.ERROR_STARTING
                rt.detail = (
                    f"{spec.display_name}: container "
                    f"{container_spec.container_name} spawned but did not "
                    f"become reachable within {container_spec.ready_timeout_s}s."
                )
                rt.error = final.error

    async def _poll_until_healthy(
        self,
        rt: BackendRuntime,
        timeout_s: float,
        *,
        poll_interval_s: float = 0.5,
    ) -> bool:
        """Probe until healthy or timeout. Returns True on success."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = await rt.spec.probe.run()
            with self._lock:
                rt.last_probe_at = time.time()
                rt.last_latency_ms = result.latency_ms
                rt.reachable = (
                    result.reachable
                )  # keep the field's contract on the poll-success path
                if result.healthy:
                    rt.detail = result.detail
                    rt.error = None
                    return True
                rt.detail = result.detail
                rt.error = result.error
            await asyncio.sleep(poll_interval_s)
        return False

    async def _reprobe(self, rt: BackendRuntime) -> None:
        """Re-probe a backend and update its state in place.

        Only state transitions we make here:

        * READY/REUSED + probe-healthy → unchanged (latency refreshed)
        * READY/REUSED + probe-unhealthy → DEGRADED
        * DEGRADED + probe-healthy → READY (or REUSED if we didn't spawn it)
        * DEGRADED + probe-unhealthy → unchanged
        """
        result = await rt.spec.probe.run()
        with self._lock:
            rt.last_probe_at = time.time()
            rt.last_latency_ms = result.latency_ms
            rt.reachable = result.reachable
            if result.healthy:
                rt.detail = result.detail
                rt.error = None
                if rt.state == BackendState.DEGRADED:
                    rt.state = BackendState.READY if rt.spawned_by_us else BackendState.REUSED
            else:
                rt.detail = result.detail
                rt.error = result.error
                if rt.state in (BackendState.READY, BackendState.REUSED):
                    rt.state = BackendState.DEGRADED

    def _compute_overall_state(self) -> str:
        """Aggregate per-backend states into a single overall string."""
        if not self._autostart and not self._start_all_done:
            return "disabled"
        states = {rt.state for rt in self._runtimes.values()}
        # If any required backend is in a hard-failed state, overall is "down".
        required_failed = any(
            rt.spec.required and rt.state in (BackendState.DOWN, BackendState.ERROR_STARTING)
            for rt in self._runtimes.values()
        )
        if required_failed:
            return "down"
        if BackendState.STARTING in states or not self._start_all_done:
            return "starting"
        # Any non-ready required backend → degraded.
        required_not_ready = any(
            rt.spec.required and rt.state not in (BackendState.READY, BackendState.REUSED)
            for rt in self._runtimes.values()
        )
        if required_not_ready:
            return "degraded"
        return "ready"

    def _actionable_messages(self) -> list[str]:
        out = []
        for rt in self._runtimes.values():
            if rt.state in (
                BackendState.DEGRADED,
                BackendState.DOWN,
                BackendState.EXTERNAL_MISSING,
                BackendState.EXTERNAL_UNCONFIGURED,
                BackendState.ERROR_STARTING,
                BackendState.EXTERNAL_SKIPPED,
            ):
                # Prefer the live detail (which carries the latest
                # error context); fall back to the spec's static
                # message if detail wasn't populated.
                msg = rt.detail or rt.spec.actionable_message
                out.append(f"[{rt.spec.name}] {msg}")
            # Fresh-create warning surfaces independently of state — a
            # READY backend that we just freshly created may have lost
            # the operator's prior data, and they need to know.
            if rt.fresh_create_warning:
                out.append(f"[{rt.spec.name}] {rt.fresh_create_warning}")
        return out


# ---------------------------------------------------------------------------
# Process-singleton accessor
# ---------------------------------------------------------------------------


_SINGLETON: InfraOrchestrator | None = None
_SINGLETON_LOCK = threading.Lock()


def get_orchestrator() -> InfraOrchestrator:
    """Return the process-singleton orchestrator, constructing on first call.

    This is the accessor the MCP server + ``infrastructure_status``
    tool both call. Construction is lazy so a test that imports the
    module without intending to start the orchestrator does not pay
    the (small) container-spec construction cost.

    Thread-safe via a module-level ``threading.Lock`` — double-checked
    locking so the fast path (singleton already exists) avoids the
    lock cost.
    """
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = InfraOrchestrator()
    return _SINGLETON


# Background-drive handle. Stored so the drive thread can be stopped
# (cancelled + joined) on process exit or in test teardown — a daemon
# thread that keeps running ``prewarm_workflow_tools()`` after its owner
# is gone logs into a closed stream and, in production, risks being
# abrupt-killed mid-conda-build. See ``stop_orchestrator_in_background_thread``.
_BG_THREAD: threading.Thread | None = None
_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_TASK: asyncio.Future | None = None
_BG_HANDLE_LOCK = threading.Lock()
_BG_ATEXIT_REGISTERED = False


def _log_bringup_verdict(snapshot: dict[str, Any]) -> None:
    """Log the aggregate backend bring-up verdict LOUDLY so a required-backend
    failure is visible AT BOOT — not only on-demand via the ``infrastructure_status``
    tool. The background drive previously discarded ``start_all()``'s snapshot, so a
    ``down``/``degraded`` deployment served tools while a mandatory backend was absent.

    Reuses the ``overall`` (from ``_compute_overall_state``) + ``actionable`` (from
    ``_actionable_messages``) already present in the snapshot — no re-probe.
    """
    overall = snapshot.get("overall")
    actionable = snapshot.get("actionable") or []
    lines = "\n  ".join(actionable) if actionable else "(no actionable detail)"
    if overall == "down":
        log.error(
            "InfraOrchestrator: backend bring-up finished overall=%s — the deployment is "
            "NOT functional (a required backend is down). Actionable:\n  %s",
            overall,
            lines,
        )
    elif overall == "degraded":
        log.warning(
            "InfraOrchestrator: backend bring-up finished overall=%s — one or more backends "
            "are unavailable. Actionable:\n  %s",
            overall,
            lines,
        )
    else:
        log.info("InfraOrchestrator: backend bring-up finished overall=%s.", overall)


def start_orchestrator_in_background_thread() -> threading.Thread:
    """Kick off ``orchestrator.start_all()`` in a dedicated daemon thread.

    The orchestrator's per-backend bring-up is async-driven (probes
    are async). We run that drive inside a fresh asyncio loop owned
    by a daemon thread so the FastMCP server's startup is not blocked
    waiting for slow probes (Rhea MCP can take 5-10s if it's spawning).

    The status tool, which runs in FastMCP's loop, is also async and
    re-probes via its OWN fresh-loop coros — the per-backend mutation
    surface is guarded by a ``threading.Lock``, so the two loops can
    not race on field assignment.

    The drive (``start_all`` + optional pre-warm) runs as a single
    cancellable asyncio task; the thread/loop/task handles are stored
    module-side so ``stop_orchestrator_in_background_thread`` can stop
    it cleanly. A process-exit ``atexit`` hook is registered once so the
    drive is *cancelled* (its ``finally`` blocks run) rather than
    abrupt-killed when the interpreter tears the daemon thread down.

    Returns the thread so callers can ``.join`` it in tests; in
    production it's a daemon thread and will die with the process.
    """
    orch = get_orchestrator()
    loop_ready = threading.Event()
    holder: dict[str, Any] = {}

    async def _drive() -> None:
        snapshot = await orch.start_all()
        # Surface the aggregate bring-up verdict LOUDLY at boot — the drive
        # previously discarded this snapshot, so a required-backend failure
        # was silent until someone polled ``infrastructure_status``.
        _log_bringup_verdict(snapshot)
        # After backends probe/spawn, run the Rhea tool pre-warm phase
        # (build + Redis-cache the per-tool conda envs declared in the
        # catalog). Pre-warm builds conda envs on disk — a system-
        # touching action — so it is SKIPPED in probe-only mode
        # (``APECX_MCP_AUTOSTART_INFRA=0``), consistent with that mode's
        # no-spawn / hands-off contract. The pre-warm bypasses the
        # Academy actor (direct ``install_conda_env`` call) so install
        # failures don't wedge the actor for the rest of the session.
        if orch._autostart:
            # Seed the rhea tool catalog if it's empty (unseeded machine) BEFORE
            # the pre-warm — pre-warm installs the per-tool conda envs the catalog
            # declares, so it needs a seeded catalog. FAIL-LOUD but non-crashing:
            # ensure_catalog_seeded raises on an ingestion failure; we log it and
            # continue so one degraded backend doesn't take the whole drive down.
            try:
                seed_result = await orch.ensure_catalog_seeded()
                log.info("InfraOrchestrator: rhea catalog seed check → %s", seed_result)
            except Exception:  # noqa: BLE001
                log.exception(
                    "InfraOrchestrator: rhea catalog ingestion failed — rhea tools stay "
                    "unavailable until the catalog is seeded (run `apecx-setup rhea`). "
                    "Continuing startup."
                )
            await orch.prewarm_workflow_tools()
        else:
            log.info(
                "InfraOrchestrator: probe-only mode (autostart=0) — "
                "skipping workflow-tool pre-warm (no conda-env builds)."
            )

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        task = loop.create_task(_drive())
        holder["loop"] = loop
        holder["task"] = task
        loop_ready.set()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            log.info("InfraOrchestrator: background drive cancelled (shutdown).")
        except Exception:  # noqa: BLE001
            log.exception("InfraOrchestrator background drive raised")
        finally:
            loop.close()

    thread = threading.Thread(target=_runner, name="apecx-infra-orchestrator", daemon=True)
    thread.start()
    # Wait briefly for the loop+task to exist so a stop() that races a
    # just-started thread has handles to act on.
    loop_ready.wait(timeout=5.0)

    global _BG_THREAD, _BG_LOOP, _BG_TASK, _BG_ATEXIT_REGISTERED
    with _BG_HANDLE_LOCK:
        _BG_THREAD = thread
        _BG_LOOP = holder.get("loop")
        _BG_TASK = holder.get("task")
        if not _BG_ATEXIT_REGISTERED:
            atexit.register(stop_orchestrator_in_background_thread)
            _BG_ATEXIT_REGISTERED = True
    return thread


def stop_orchestrator_in_background_thread(timeout: float = 5.0) -> None:
    """Cancel the background drive task and join its thread.

    Idempotent and safe to call when no thread is running. Cancelling the
    task (rather than killing the thread) lets in-flight ``await`` points —
    e.g. a conda-env build inside pre-warm — unwind through their
    ``finally`` blocks. Registered as an ``atexit`` hook by
    ``start_orchestrator_in_background_thread``; also called by test
    teardown so the drive does not outlive the test that spawned it (a
    leaked drive logs into pytest's closed capture stream).
    """
    with _BG_HANDLE_LOCK:
        thread, loop, task = _BG_THREAD, _BG_LOOP, _BG_TASK
    if thread is None or not thread.is_alive():
        return
    if loop is not None and task is not None and not task.done():
        with contextlib.suppress(RuntimeError):
            # RuntimeError if the loop already closed between the check
            # and the call — benign (thread is on its way out).
            loop.call_soon_threadsafe(task.cancel)
    thread.join(timeout=timeout)


def reset_orchestrator_for_testing() -> None:
    """Clear the singleton. Tests use this between test functions.

    Production code MUST NOT call this — atexit hooks registered by
    the previous singleton are not unregistered, which would leak.
    Test fixtures call this in setup AND teardown.
    """
    global _SINGLETON
    _SINGLETON = None


__all__ = [
    "InfraOrchestrator",
    "get_orchestrator",
    "reset_orchestrator_for_testing",
    "start_orchestrator_in_background_thread",
    "stop_orchestrator_in_background_thread",
]
