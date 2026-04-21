"""ContainerRuntime protocol + detection.

The app needs to bring up a local Postgres on first start and reuse it
thereafter. Two runtimes are supported:

- **Docker** (preferred): scientist laptops, dev boxes, CI.
- **Apptainer** (fallback): HPC login/compute nodes where Docker isn't
  available (daemon needs root; HPC users rarely have it). Apptainer is
  the renamed/forked Singularity.

Both runtimes expose the small surface area we actually use:

    ensure_postgres_running(config) -> None
    is_postgres_running() -> bool
    teardown(remove_data: bool) -> None

Selection: ``detect_runtime()`` picks the first available. Docker is
preferred because its ``compose`` orchestration and built-in healthcheck
need no re-implementation. Apptainer loses some Docker-only conveniences
(named volumes, compose profiles) — documented in each method.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class RuntimeKind(Enum):
    DOCKER = "docker"
    APPTAINER = "apptainer"


@dataclass(frozen=True, kw_only=True)
class PostgresConfig:
    """Parameters the runtime needs to stand up a Postgres instance."""

    image: str = "postgres:16-alpine"
    port: int = 5433
    user: str = "apecx"
    password: str = "apecx"
    database: str = "apecx_cp"
    data_dir: str  # host-side path that will hold Postgres data


class ContainerRuntimeUnavailable(RuntimeError):
    """Raised when no supported container runtime is installed."""


@runtime_checkable
class ContainerRuntime(Protocol):
    kind: RuntimeKind

    def ensure_postgres_running(self, config: PostgresConfig) -> None:
        """Idempotent: start Postgres if not running, no-op if already up."""

    def is_postgres_running(self) -> bool: ...

    def teardown(self, config: PostgresConfig, *, remove_data: bool) -> None:
        """Stop the container. When ``remove_data=True`` also remove the
        volume / bind-mounted data directory. No-op if nothing is running.
        """


def _have_command(name: str) -> bool:
    return shutil.which(name) is not None


def _docker_daemon_is_up() -> bool:
    """Cheap liveness check for the Docker daemon.

    ``docker info --format '{{.ServerVersion}}'`` prints nothing and
    returns non-zero when the daemon is not reachable (even though the
    client is installed). The client-only case is what distinguishes
    "docker installed but daemon off" from "docker fully available".
    """
    if not _have_command("docker"):
        return False
    try:
        res = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return res.returncode == 0 and bool(res.stdout.strip())


def detect_runtime() -> ContainerRuntime:
    """Pick a runtime. Docker first if its daemon is up, Apptainer next.

    Raises :class:`ContainerRuntimeUnavailable` if neither is available.
    """
    # Late imports so Docker-only and Apptainer-only code paths don't
    # import each other's subprocess shims at module load time.
    if _docker_daemon_is_up():
        from apecx_integration.control_plane.infra.docker_runtime import DockerRuntime

        return DockerRuntime()
    if _have_command("apptainer") or _have_command("singularity"):
        from apecx_integration.control_plane.infra.apptainer_runtime import (
            ApptainerRuntime,
        )

        binary = "apptainer" if _have_command("apptainer") else "singularity"
        return ApptainerRuntime(binary=binary)

    raise ContainerRuntimeUnavailable(
        "Neither Docker (daemon-up) nor Apptainer/Singularity found on PATH. "
        "Install one, or set APECX_CP_DB_URL to a remote Postgres you manage."
    )
