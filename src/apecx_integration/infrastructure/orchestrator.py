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
   is set, we attempt to bring it up — ``docker run`` for containers,
   ``Popen`` for host processes — then poll the probe until healthy
   or the timeout fires.
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

from apecx_integration.infrastructure.backends import (
    BackendRuntime,
    BackendSpec,
    BackendState,
    ContainerSpec,
    HostProcessSpec,
    Probe,
    ProbeResult,
)
from apecx_integration.infrastructure.containers import (
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

log = logging.getLogger(__name__)


_AUTOSTART_ENV_VAR = "APECX_MCP_AUTOSTART_INFRA"
_RHEA_REPO_PATH = "RHEA_REPO_PATH"
_RHEA_PYTHON_PATH = "RHEA_PYTHON_PATH"
# Optional. Extra path prepended to the spawned Rhea process's PATH so
# its downstream conda subprocesses (tool agents that run e.g. MUSCLE
# via `conda run`) can find the `conda` binary. apecx-mcp is started by
# Claude Desktop via Popen with NO shell — the operator's interactive
# PATH is NOT inherited; the operator must declare PATH (or this var)
# in claude_desktop_config.json's env block.
_RHEA_CONDA_BIN_ENV = "RHEA_CONDA_BIN"
# Optional embedding-model name for Rhea (used by the ToolShed catalog
# embedding step). 1024-dim default matches the Ollama mxbai-embed-large
# model bundled by apecx-setup; Rhea's bare default is for an HF
# embedding image that nobody on macOS actually runs.
_RHEA_EMBEDDING_MODEL_ENV = "RHEA_EMBEDDING_MODEL"
_OLLAMA_BASE_URL_ENV = "APECX_LLM_BASE_URL"
_RHEA_MCP_URL_ENV = "RHEA_MCP_URL"
# Selects how the orchestrator brings up rhea-server:
#   "container" (DEFAULT) — run the rhea-server Docker IMAGE. Tool execution +
#                per-tool conda envs build INSIDE the container, independent of
#                the host conda (the canonical Apple-Silicon broken-conda
#                failure). Verified host-conda-independent + memory-flat across a
#                4-virus viral_epitope_analysis multi-probe. Requires Docker; the
#                orchestrator only `docker run`s — `apecx-setup rhea` builds the
#                image; a missing image / no Docker surfaces a LOUD actionable
#                error and RHEA-backed tools degrade-loud (the rest still runs).
#   "host"      — spawn it as a host PROCESS using RHEA_PYTHON_PATH. No Docker
#                needed, but tool execution uses the HOST conda (fragile if that
#                conda is broken/missing). Set APECX_RHEA_BACKEND=host to opt in.
_RHEA_BACKEND_ENV = "APECX_RHEA_BACKEND"
# Image tag the container backend runs. Default matches what `apecx-setup rhea`
# builds from $RHEA_REPO_PATH/Dockerfile.
_RHEA_IMAGE_ENV = "APECX_RHEA_IMAGE"
_RHEA_IMAGE_DEFAULT = "apecx-rhea-server:local"


def _autostart_enabled() -> bool:
    return os.environ.get(_AUTOSTART_ENV_VAR, "1") != "0"


def _terminate_process_group(pid: int, *, grace_seconds: float) -> None:
    """SIGTERM the whole process group, then SIGKILL after grace.

    The orchestrator Popen's host processes with
    ``start_new_session=True`` so each spawned tree is its own session
    leader. Killing JUST the parent pid leaves the parent's children
    (uvicorn workers, parsl interchanges, ...) running and bound to
    their ports — which makes the next orchestrator's probe see
    ``reused`` against a "shutdown" backend. Group-kill closes that.
    """
    import signal as _signal

    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, _signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)  # signal 0 = "is anyone in the group still alive?"
        except (ProcessLookupError, OSError):
            return  # group is gone — clean exit
        time.sleep(0.1)
    # Grace expired; SIGKILL the group.
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        log.warning(
            "InfraOrchestrator: process group %s did not exit on SIGTERM; sending SIGKILL",
            pgid,
        )
        os.killpg(pgid, _signal.SIGKILL)


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
        return await ollama_probe(base_url=base_url)

    return BackendSpec(
        name="ollama",
        display_name="Ollama (host process)",
        kind="external",
        required=True,
        probe=Probe(name="ollama", fn=_probe),
        actionable_message=(
            f"Ollama not found at {base_url}. Install Ollama from "
            "https://ollama.com/download and run `ollama serve` (or "
            "`brew services start ollama` on macOS). The orchestrator "
            "cannot install or autostart Ollama — it is an operator "
            "prerequisite."
        ),
        tags=("llm", "operator-prereq"),
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
    return {
        # Server bind (Rhea's own MCP host:port — not the upstream MCP URL).
        "HOST": "localhost",
        "PORT": "3001",
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


def _verify_rhea_python_can_import(python_exec: str) -> tuple[bool, str]:
    """Sanity-check the configured Python BEFORE spawning rhea-server.

    Without this, a wrong RHEA_PYTHON_PATH (e.g. plain miniconda
    instead of the rhea uv-venv) leads to a 60-second
    "did not become healthy" wait followed by an obscure ImportError
    buried in the child log. With this check, we FAIL-LOUD upfront
    with an actionable message.
    """
    try:
        result = subprocess.run(
            [python_exec, "-c", "import rhea; print(rhea.__file__ or 'ns-pkg')"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[:400]
        return False, f"`{python_exec} -c 'import rhea'` exited {result.returncode}: {stderr}"
    return True, result.stdout.decode("utf-8", "replace").strip()


def _make_rhea_mcp_spec(
    *,
    postgres_container: ContainerSpec,
    redis_container: ContainerSpec,
    minio_container: ContainerSpec,
    ollama_base_url: str,
) -> BackendSpec:
    mcp_url = os.environ.get(_RHEA_MCP_URL_ENV, "http://localhost:3001/mcp/")

    async def _probe() -> ProbeResult:
        return await rhea_mcp_probe(mcp_url=mcp_url)

    def _command_factory(env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        rhea_python_bin = env[_RHEA_PYTHON_PATH]
        rhea_repo = env[_RHEA_REPO_PATH]
        python_exec = (
            f"{rhea_python_bin.rstrip('/')}/python"
            if not rhea_python_bin.endswith("/python")
            else rhea_python_bin
        )

        # Pre-spawn import check. FAIL-LOUD here turns a 60-second
        # "did not become healthy" wait into an immediate actionable
        # error naming the exact Python that couldn't import rhea.
        ok, detail = _verify_rhea_python_can_import(python_exec)
        if not ok:
            raise RuntimeError(
                f"the configured ${_RHEA_PYTHON_PATH}={rhea_python_bin!r} "
                f"cannot import `rhea`. Most common cause: pointing at a "
                f"bare miniconda bin/ dir; Rhea is installed in its uv "
                f"venv. Try ${_RHEA_PYTHON_PATH}={rhea_repo}/.venv/bin. "
                f"Underlying check: {detail}"
            )

        # Compose Rhea env from our other backend specs (single source
        # of truth — no port/host drift between apecx-mcp's view and
        # Rhea's Settings).
        rhea_env = _compose_rhea_env(
            postgres=postgres_container,
            redis_c=redis_container,
            minio=minio_container,
            ollama_base_url=ollama_base_url,
        )
        # Extend PATH so:
        #  1. the spawned rhea process resolves its own python
        #     (RHEA_PYTHON_PATH bin first).
        #  2. its downstream conda subprocesses can find `conda`
        #     (optional RHEA_CONDA_BIN second; if unset, the
        #     operator's existing PATH is preserved verbatim).
        path_segments = [rhea_python_bin.rstrip("/")]
        conda_bin = env.get(_RHEA_CONDA_BIN_ENV)
        if conda_bin:
            path_segments.append(conda_bin.rstrip("/"))
        path_segments.append(env.get("PATH", ""))
        # Hand Rhea an explicit conda binary via CONDA_EXE (conda's own
        # canonical env var). Without this, Rhea's subprocess invocations
        # resolve `conda` via PATH — and a stale Anaconda install at
        # /opt/anaconda3/bin/conda can win even when miniconda is first,
        # because some PATH-lookup contexts (parsl workers spawned from
        # uvicorn) re-resolve through the wider operator env. Setting
        # CONDA_EXE explicitly is the standard conda-shell convention.
        conda_exe_env: dict[str, str] = {}
        if conda_bin:
            conda_exe_env["CONDA_EXE"] = f"{conda_bin.rstrip('/')}/conda"
        env_additions: dict[str, str] = {
            "PATH": ":".join(seg for seg in path_segments if seg),
            "PYTHONUNBUFFERED": "1",
            **conda_exe_env,
            # Composed Rhea env (overrides any pre-existing values for
            # determinism — operator who wants a different DB URL can
            # set DATABASE_URL in claude_desktop_config.json's env
            # block, but then they should NOT use apecx's postgres at
            # all).
            **rhea_env,
            # Forward known RHEA_* env vars in case the operator wants
            # to override anything composed above.
            **{
                k: env[k]
                for k in env
                if k.startswith("RHEA_")
                and k
                not in (
                    _RHEA_REPO_PATH,
                    _RHEA_PYTHON_PATH,
                    _RHEA_CONDA_BIN_ENV,
                    _RHEA_EMBEDDING_MODEL_ENV,
                )
            },
        }
        argv = [
            python_exec,
            "-m",
            "rhea.server.mcp_server",
            "--transport",
            "streamable-http",
        ]
        # Run from the Rhea repo so any relative configs resolve.
        env_additions["__CWD__"] = rhea_repo
        return argv, env_additions

    process_spec = HostProcessSpec(
        prereq_env_vars=(_RHEA_REPO_PATH, _RHEA_PYTHON_PATH),
        command_factory=_command_factory,
        ready_timeout_s=60.0,
    )

    return BackendSpec(
        name="rhea_mcp",
        display_name="Rhea MCP (host process)",
        kind="host_process",
        required=True,
        probe=Probe(name="rhea_mcp", fn=_probe),
        actionable_message=(
            f"Rhea MCP is unreachable at {mcp_url}. To enable autostart, "
            f"set ${_RHEA_REPO_PATH} (path to the Rhea checkout) and "
            f"${_RHEA_PYTHON_PATH} (path to Rhea's uv venv bin/ — "
            "typically $RHEA_REPO_PATH/.venv/bin; NOT a bare miniconda "
            "bin/ unless miniconda is itself the rhea project venv). "
            f"Optionally set ${_RHEA_CONDA_BIN_ENV} (miniconda bin/ "
            "needed for the conda subprocesses Rhea spawns to run "
            "Galaxy tools like MUSCLE). Alternatively, start it "
            "manually: cd $RHEA_REPO_PATH && uv run -m "
            "rhea.server.mcp_server --transport streamable-http. "
            "Without Rhea MCP, the Rhea-backed catalog tools return "
            "UNAVAILABLE."
        ),
        process=process_spec,
        tags=("mcp", "rhea"),
    )


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

    The orchestrator only ``docker run``s — it does NOT build the image. A
    missing image surfaces a LOUD actionable message (build via ``apecx-setup
    rhea`` / ``docker build``). Reaching the host-published infra ports uses
    ``--add-host=host.docker.internal:host-gateway`` (needed on Linux; a no-op
    but harmless on Docker Desktop).
    """
    mcp_url = os.environ.get(_RHEA_MCP_URL_ENV, "http://localhost:3001/mcp/")
    image = os.environ.get(_RHEA_IMAGE_ENV, _RHEA_IMAGE_DEFAULT)

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
        extra_run_args=("--add-host=host.docker.internal:host-gateway",),
        # The server boots (probe = :3001 health) in ~10s; the slow per-tool
        # conda build happens later, on the first tool CALL, not at startup.
        ready_timeout_s=120.0,
    )

    return BackendSpec(
        name="rhea_mcp",
        display_name="Rhea MCP (container)",
        kind="docker_container",
        required=True,
        probe=Probe(name="rhea_mcp", fn=_probe),
        actionable_message=(
            f"Rhea MCP container is unreachable at {mcp_url}. The orchestrator "
            f"runs the image {image!r} but does NOT build it. Build it once with "
            f"`apecx-setup rhea` (or `docker build -t {image} -f "
            f"$RHEA_REPO_PATH/Dockerfile $RHEA_REPO_PATH`), make sure Docker is "
            f"running, then retry. To use the host-process backend instead, set "
            f"${_RHEA_BACKEND_ENV}=host."
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
    # Backend selection: default "host" (unchanged behavior). "container" runs
    # the rhea-server image so tool execution is independent of the host conda.
    rhea_backend = os.environ.get(_RHEA_BACKEND_ENV, "container").strip().lower()
    if rhea_backend == "container":
        rhea = _make_rhea_container_spec(
            postgres_container=pg.container,  # type: ignore[arg-type]
            redis_container=redis_s.container,  # type: ignore[arg-type]
            minio_container=minio_s.container,  # type: ignore[arg-type]
            ollama_base_url=ollama_base_url,
        )
    else:
        rhea = _make_rhea_mcp_spec(
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
        # Track spawned children for atexit cleanup. We hold direct
        # references so atexit can still reach them even if all other
        # references go out of scope.
        self._spawned_processes: list[subprocess.Popen[bytes]] = []
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
        idempotent (re-probes healthy backends, re-attempts the stuck ones). NOTE: only a
        stuck *docker container* triggers this; RHEA (a host_process) is re-attempted only as
        a side effect of ``start_all`` firing for a stuck container — if ONLY RHEA is stuck,
        reconcile is a no-op (out of scope: this heals the Docker-came-up-late case).
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
        """Tear down ONLY backends we spawned.

        Spawned host processes get SIGTERM with a 5s grace, then
        SIGKILL. Spawned containers get ``docker stop`` (10s grace)
        followed by ``docker rm -f`` if stop fails. Pre-existing
        containers + processes are never touched.
        """
        # Spawned host processes. We signal the entire PROCESS GROUP
        # (not just the leader pid) because rhea-server's uvicorn
        # parent forks worker children — SIGTERM to the parent alone
        # leaves the workers serving on the bound port, and the next
        # orchestrator's probe sees `reused` against a "shutdown" rhea.
        # We Popen'd with start_new_session=True so each spawned tree
        # is in its own session/group.
        for proc in list(self._spawned_processes):
            if proc.poll() is not None:
                continue
            try:
                _terminate_process_group(proc.pid, grace_seconds=5.0)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "InfraOrchestrator: error terminating child pid=%s: %s",
                    proc.pid,
                    exc,
                )
        self._spawned_processes.clear()

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
        # Spawned host processes — terminate without awaiting.
        for proc in list(self._spawned_processes):
            try:
                if proc.poll() is None:
                    # Kill the whole process group so uvicorn workers
                    # die along with the rhea-server parent — see the
                    # same fix in shutdown() above.
                    _terminate_process_group(proc.pid, grace_seconds=5.0)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        self._spawned_processes.clear()

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

        if spec.kind == "host_process":
            await self._bring_up_host_process(rt)
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
        # transitioned them from stopped to running.
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
        self._spawned_containers.append(container_spec.container_name)

        # Poll the probe until healthy or the per-spec timeout fires.
        ok = await self._poll_until_healthy(rt, container_spec.ready_timeout_s)
        if ok:
            with self._lock:
                rt.state = BackendState.READY
        else:
            with self._lock:
                rt.state = BackendState.ERROR_STARTING
                rt.detail = (
                    f"{spec.display_name}: container "
                    f"{container_spec.container_name} spawned but did not "
                    f"become healthy within {container_spec.ready_timeout_s}s."
                )

    async def _bring_up_host_process(self, rt: BackendRuntime) -> None:
        spec = rt.spec
        process_spec: HostProcessSpec = spec.process  # type: ignore[assignment]

        # Check that every prereq env var is set. If not, mark
        # EXTERNAL_UNCONFIGURED — no spawn attempt.
        missing = [var for var in process_spec.prereq_env_vars if not os.environ.get(var)]
        if missing:
            with self._lock:
                rt.state = BackendState.EXTERNAL_UNCONFIGURED
                rt.detail = (
                    f"{spec.display_name}: missing prereq env var(s) "
                    f"{missing}. {spec.actionable_message}"
                )
                rt.error = f"unset env vars: {missing}"
            return

        with self._lock:
            rt.state = BackendState.STARTING
            rt.detail = f"spawning host process for {spec.display_name} ..."

        try:
            argv, env_additions = process_spec.command_factory(dict(os.environ))
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                rt.state = BackendState.ERROR_STARTING
                rt.detail = (
                    f"{spec.display_name}: command_factory raised "
                    f"{type(exc).__name__}: {exc}. {spec.actionable_message}"
                )
                rt.error = f"{type(exc).__name__}: {exc}"
            return

        cwd = env_additions.pop("__CWD__", None)
        spawn_env = dict(os.environ)
        spawn_env.update(env_additions)

        # Tee stdout/stderr to a log file the operator can inspect.
        try:
            log_fh = open(process_spec.log_path, "ab")  # noqa: SIM115 — handle lives with the process
        except OSError as exc:
            with self._lock:
                rt.state = BackendState.ERROR_STARTING
                rt.detail = (
                    f"{spec.display_name}: could not open log file "
                    f"{process_spec.log_path}: {exc}. {spec.actionable_message}"
                )
                rt.error = str(exc)
            return

        try:
            proc = subprocess.Popen(  # noqa: S603 — argv built from operator config
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                env=spawn_env,
                cwd=cwd,
                start_new_session=True,
            )
        except OSError as exc:
            log_fh.close()
            with self._lock:
                rt.state = BackendState.ERROR_STARTING
                rt.detail = (
                    f"{spec.display_name}: Popen failed with {type(exc).__name__}: "
                    f"{exc}. {spec.actionable_message}"
                )
                rt.error = str(exc)
            return

        with self._lock:
            rt.spawned_by_us = True
            rt.spawned_pid = proc.pid
        self._spawned_processes.append(proc)

        # Poll the probe.
        ok = await self._poll_until_healthy(rt, process_spec.ready_timeout_s)
        if ok:
            with self._lock:
                rt.state = BackendState.READY
            return

        # Child failed to come up. Mark error state but keep the proc
        # in _spawned_processes so atexit still cleans it up if it
        # spawned a partial daemon.
        with self._lock:
            rt.state = BackendState.ERROR_STARTING
            rt.detail = (
                f"{spec.display_name}: process pid={proc.pid} spawned but "
                f"did not become healthy within {process_spec.ready_timeout_s}s. "
                f"Check {process_spec.log_path} for the child's stderr. "
                f"{spec.actionable_message}"
            )

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
        await orch.start_all()
        # After backends probe/spawn, run the Rhea tool pre-warm phase
        # (build + Redis-cache the per-tool conda envs declared in the
        # catalog). Pre-warm builds conda envs on disk — a system-
        # touching action — so it is SKIPPED in probe-only mode
        # (``APECX_MCP_AUTOSTART_INFRA=0``), consistent with that mode's
        # no-spawn / hands-off contract. The pre-warm bypasses the
        # Academy actor (direct ``install_conda_env`` call) so install
        # failures don't wedge the actor for the rest of the session.
        if orch._autostart:
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
