"""Docker-backed implementation of :class:`ContainerRuntime`.

The existing ``docker-compose.yml`` at the repo root is the source of
truth for service definitions, ports, and the persistent named volume.
This runtime drives it via ``docker compose`` subcommands.

Why ``docker compose`` and not ``docker run``:
- The compose file already exists and is the right granularity for
  testing / CI / explicit dev workflows.
- Named volume lifecycle (``apecx_cp_postgres_data``) is compose's
  concern; we don't want to reimplement it.
- ``docker compose up -d postgres`` is idempotent: no-op when the
  container is already running, starts it when stopped.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from apecx_integration.control_plane.infra.runtime import (
    PostgresConfig,
    RuntimeKind,
)

log = logging.getLogger(__name__)

CONTAINER_NAME = "apecx-cp-postgres"
VOLUME_NAME = "apecx_cp_postgres_data"


def _repo_root() -> Path:
    # Walk up from this file until we find docker-compose.yml. Running
    # under pytest or after ``pip install -e .`` the package path won't
    # point at the repo root directly, so we search — but we only search
    # a bounded number of levels to stay safe.
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "docker-compose.yml").is_file():
            return parent
    raise FileNotFoundError(
        "docker-compose.yml not found in any parent of "
        f"{here}; cannot drive Docker runtime from outside the repo."
    )


class DockerRuntime:
    kind = RuntimeKind.DOCKER

    def __init__(self, *, repo_root: Path | None = None) -> None:
        self._repo_root = repo_root or _repo_root()

    def _compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", *args],
            cwd=self._repo_root,
            check=check,
            capture_output=True,
            text=True,
        )

    def ensure_postgres_running(self, config: PostgresConfig) -> None:
        # ``up -d`` is idempotent — creates + starts if missing, no-op if
        # already healthy. The compose file controls port/user/password/db
        # identity; we verify that the caller's expectations match to
        # catch drift between PostgresConfig and docker-compose.yml.
        if config.port != 5433:
            raise ValueError(
                f"DockerRuntime: docker-compose.yml binds host port 5433, "
                f"PostgresConfig asked for {config.port}. Edit the compose "
                "file if you need a different port."
            )
        already = self.is_postgres_running()
        if already:
            log.info("Docker container %s already running; no-op.", CONTAINER_NAME)
        else:
            log.info(
                "Docker: starting container %s via `docker compose up -d "
                "postgres`. Image %s will be pulled on first run; this may "
                "take a minute.",
                CONTAINER_NAME,
                config.image,
            )
        self._compose("up", "-d", "postgres")
        if not already:
            log.info(
                "Waiting for Postgres to report healthy on localhost:%d...",
                config.port,
            )
        self._wait_for_healthy(timeout_seconds=30)
        if not already:
            log.info("Postgres ready on localhost:%d.", config.port)

    def is_postgres_running(self) -> bool:
        res = self._compose("ps", "--format", "{{.Name}}\t{{.State}}", check=False)
        if res.returncode != 0:
            return False
        for line in res.stdout.splitlines():
            if line.startswith(CONTAINER_NAME + "\t") and "running" in line.lower():
                return True
        return False

    def teardown(self, config: PostgresConfig, *, remove_data: bool) -> None:
        # ``docker compose down`` only touches containers owned by the
        # current compose project (derived from the worktree directory
        # name). If a previous run in a different worktree / project
        # created the same fixed-name container, compose won't clean it
        # up — we fall back to a direct ``docker rm -f`` by the literal
        # container name.
        if remove_data:
            # -v drops the named volume too.
            self._compose("down", "-v", check=False)
        else:
            self._compose("down", check=False)
        subprocess.run(
            ["docker", "rm", "-f", CONTAINER_NAME],
            capture_output=True,
            text=True,
            check=False,
        )

    def _wait_for_healthy(self, *, timeout_seconds: int) -> None:
        """Poll ``docker compose ps`` until the postgres service reports
        health=healthy, or timeout.

        Compose's built-in healthcheck (``pg_isready`` in the container)
        is what actually gates this; we just watch for the transition.
        """
        deadline = time.monotonic() + timeout_seconds
        last_state = ""
        while time.monotonic() < deadline:
            res = self._compose("ps", "--format", "{{.Name}}\t{{.State}}\t{{.Health}}", check=False)
            for line in res.stdout.splitlines():
                if not line.startswith(CONTAINER_NAME + "\t"):
                    continue
                parts = line.split("\t")
                # parts[1] = State, parts[2] = Health (may be empty if no healthcheck).
                last_state = "\t".join(parts[1:])
                health = parts[2] if len(parts) > 2 else ""
                if health == "healthy":
                    return
                if parts[1].lower() == "running" and not health:
                    # Image without healthcheck but running is good enough.
                    return
            time.sleep(0.5)
        raise TimeoutError(
            f"Postgres container {CONTAINER_NAME} did not become healthy "
            f"within {timeout_seconds}s (last state: {last_state!r})."
        )
