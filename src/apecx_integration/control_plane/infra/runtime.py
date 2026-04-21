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


_INSTALL_HINT = """\
Neither Docker (daemon-up) nor Apptainer/Singularity is available. Options:

  macOS:
    - Docker Desktop:  https://www.docker.com/products/docker-desktop
    - Apptainer (via Lima VM):
        https://apptainer.org/docs/admin/main/installation.html#mac

  Linux (laptop / workstation):
    - Docker Engine:   apt install docker.io   (or equivalent)
                       then add your user to the 'docker' group.
    - Apptainer:       https://apptainer.org/docs/admin/main/installation.html

  HPC (no root):
    - Apptainer is typically pre-installed; try `module load apptainer`
      or `module load singularity` for the legacy binary.
    - Docker is rarely available on HPC; don't expect it.

Escape hatches that need no container:
    - export APECX_CP_DB_URL='sqlite:///./apecx_cp.db'       # zero infra
    - export APECX_CP_DB_URL='postgresql+psycopg://...'      # BYO remote
"""


def detect_runtime() -> ContainerRuntime:
    """Pick a runtime. Docker first if its daemon is up, Apptainer next.

    Raises :class:`ContainerRuntimeUnavailable` with OS-specific install
    hints and BYO-escape-hatch env-var examples if neither is available.
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

    raise ContainerRuntimeUnavailable(_INSTALL_HINT)
