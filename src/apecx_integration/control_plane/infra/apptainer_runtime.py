"""Apptainer-backed implementation of :class:`ContainerRuntime`.

Apptainer (renamed from Singularity) is the container runtime of choice
on HPC login and compute nodes — it needs no daemon, no root, and
respects user quotas. This runtime is the fallback when Docker isn't
available.

Parity caveats vs. DockerRuntime (things Apptainer can't do, or does
differently):

- **No compose orchestration.** We issue ``apptainer instance`` commands
  directly for one service at a time. Multi-service compose is out of
  scope.
- **No named volumes.** Apptainer bind-mounts host directories.
  ``PostgresConfig.data_dir`` is a user-provided parent; the runtime
  creates and manages a subdirectory named ``DATA_SUBDIR`` under it
  (``apecx_cp_postgres/``). All data lives in that subdirectory; all
  destructive ``rm -rf`` calls are bounded to it. If the caller passes
  ``data_dir="/home/alex"``, only ``/home/alex/apecx_cp_postgres`` is
  ever touched — never the parent.
- **No healthcheck built in.** The runtime probes from outside: first
  via a ``psycopg.connect(...).execute("SELECT 1")`` if psycopg is
  installed (cheapest, catches auth + networking in one shot), falling
  back to ``apptainer exec <instance> pg_isready`` if psycopg is not
  available. The fallback keeps the Apptainer path working on minimal
  installs that don't carry the Postgres Python driver.
- **Docker images are pulled via ``docker://``.** First start pays the
  conversion cost (image pulled + converted to SIF). The runtime logs
  ``INFO pulling+converting image, this may take a minute on first
  run`` before the blocking call so the user sees progress.
- **Networking.** Apptainer shares the host network by default. The
  Postgres port (5433) must be free on the host — same assumption as
  Docker, so no practical difference.

End-to-end testability:

- Command construction is pinned by unit tests
  (``tests/unit/test_apptainer_commands.py``).
- ``tests/integration/test_apptainer_runtime.py`` runs end-to-end when
  ``apptainer`` or ``singularity`` is on ``$PATH``. On macOS,
  installing Apptainer via Lima (see ``apptainer.org/docs/admin``)
  makes this possible through ``limactl shell apptainer --``; the
  integration test file explains the opt-in and documents the
  trade-offs.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from apecx_integration.control_plane.infra.runtime import (
    PostgresConfig,
    RuntimeKind,
)

log = logging.getLogger(__name__)

INSTANCE_NAME = "apecx-cp-postgres"
# Apptainer operates by bind-mounting a host path into the container's
# Postgres data directory. To make the destructive ``--remove-data``
# teardown safe, the runtime never touches the user-supplied
# ``data_dir`` itself — it creates and manages this subdirectory under
# it, and only that subdirectory is ever ``rm -rf``'d.
DATA_SUBDIR = "apecx_cp_postgres"


@dataclass(frozen=True, kw_only=True)
class _CommandResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class _SubprocessRunner:
    """Indirection so command construction is unit-testable without
    actually invoking apptainer, and so that tests running on macOS
    can swap in a Lima-wrapped runner that routes commands through
    ``limactl shell``.
    """

    captured_calls: list[list[str]] = field(default_factory=list)

    def run(
        self, cmd: list[str], *, check: bool = True, timeout: float | None = 300.0
    ) -> _CommandResult:
        self.captured_calls.append(list(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)
        return _CommandResult(
            cmd=cmd, returncode=res.returncode, stdout=res.stdout, stderr=res.stderr
        )


def _managed_data_path(config: PostgresConfig) -> str:
    """Return the actual on-disk path the runtime manages.

    Always a subdirectory named :data:`DATA_SUBDIR` under
    ``config.data_dir`` — keeps the destructive teardown bounded.
    """
    return str(Path(config.data_dir) / DATA_SUBDIR)


class ApptainerRuntime:
    kind = RuntimeKind.APPTAINER

    def __init__(
        self,
        *,
        binary: str = "apptainer",
        runner: _SubprocessRunner | None = None,
    ) -> None:
        self._binary = binary
        self._runner = runner or _SubprocessRunner()

    # ---- Command construction (unit-testable) -------------------------

    def build_ensure_commands(self, config: PostgresConfig) -> list[list[str]]:
        """The exact commands we would invoke to bring Postgres up.

        Returning the list instead of executing it makes this deterministic
        for unit tests and is the natural shape for a "dry run" MCP tool
        we may add later. The actual ``ensure_postgres_running`` method
        executes each command in sequence with real subprocess calls.
        """
        image_uri = f"docker://{config.image}"
        managed = _managed_data_path(config)
        # Bind-mount the managed subdir at the container's Postgres data path.
        bind_arg = f"{managed}:/var/lib/postgresql/data"
        env_args = [
            "--env",
            f"POSTGRES_USER={config.user}",
            "--env",
            f"POSTGRES_PASSWORD={config.password}",
            "--env",
            f"POSTGRES_DB={config.database}",
            "--env",
            # Postgres 16 Alpine: PGDATA must be under the bind-mounted path.
            "PGDATA=/var/lib/postgresql/data/pgdata",
        ]
        return [
            [
                self._binary,
                "instance",
                "start",
                "--bind",
                bind_arg,
                *env_args,
                image_uri,
                INSTANCE_NAME,
            ],
        ]

    def build_teardown_commands(
        self, config: PostgresConfig, *, remove_data: bool
    ) -> list[list[str]]:
        cmds: list[list[str]] = [[self._binary, "instance", "stop", INSTANCE_NAME]]
        if remove_data:
            # Apptainer has no "volume rm". The app owns the managed
            # subdirectory; we rm -rf THAT, never the user's data_dir.
            cmds.append(["rm", "-rf", _managed_data_path(config)])
        return cmds

    def build_is_running_command(self) -> list[str]:
        return [self._binary, "instance", "list", "--json"]

    # ---- Execution ----------------------------------------------------

    def ensure_postgres_running(self, config: PostgresConfig) -> None:
        managed = _managed_data_path(config)
        # Route mkdir through the runner so Lima-wrapped test runners
        # create the directory inside the VM where Apptainer actually
        # sees it. A raw ``Path(managed).mkdir`` would create it on the
        # host instead, and the bind-mount would point at a path that
        # does not exist inside the container.
        self._runner.run(["mkdir", "-p", managed], check=True)
        if self.is_postgres_running():
            log.info("Apptainer instance %s already running; no-op.", INSTANCE_NAME)
            return
        log.info(
            "Apptainer: starting instance %s (bind=%s). Image %s will be "
            "pulled and converted to SIF on first run; this may take a "
            "minute or two.",
            INSTANCE_NAME,
            managed,
            config.image,
        )
        for cmd in self.build_ensure_commands(config):
            self._runner.run(cmd)
        log.info(
            "Apptainer instance %s started; waiting for Postgres to accept "
            "connections on localhost:%d.",
            INSTANCE_NAME,
            config.port,
        )
        self._wait_for_healthy(config, timeout_seconds=60)
        log.info("Apptainer-managed Postgres ready on localhost:%d.", config.port)

    def is_postgres_running(self) -> bool:
        try:
            res = self._runner.run(self.build_is_running_command(), check=False, timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if res.returncode != 0:
            return False
        # ``apptainer instance list --json`` emits {"instances":[{"instance":"...","pid":...,...}]}
        # We deliberately do a substring check rather than JSON-parse here:
        # (a) older Apptainer versions differ in the JSON envelope shape;
        # (b) the instance name is unique and distinctive, so the risk of
        # a false positive is effectively zero.
        return INSTANCE_NAME in res.stdout

    def teardown(self, config: PostgresConfig, *, remove_data: bool) -> None:
        for cmd in self.build_teardown_commands(config, remove_data=remove_data):
            self._runner.run(cmd, check=False)

    # ---- Healthcheck --------------------------------------------------

    def _wait_for_healthy(self, config: PostgresConfig, *, timeout_seconds: int) -> None:
        """Poll Postgres until it accepts connections, or timeout.

        Primary path: ``psycopg.connect + SELECT 1`` (cheap, catches auth
        + networking in one shot). Fallback: ``apptainer exec <instance>
        pg_isready`` when psycopg is not importable — keeps the Apptainer
        path working on minimal Python installs that don't carry a
        Postgres driver. pg_isready is shipped in the postgres image so
        it's always available inside the running container.
        """
        try:
            import psycopg  # type: ignore[import-not-found]

            probe_fn = lambda: self._psycopg_probe(config, psycopg)  # noqa: E731
        except ImportError:
            log.info(
                "psycopg not installed; Apptainer healthcheck will use "
                "'apptainer exec pg_isready' fallback."
            )
            probe_fn = lambda: self._pg_isready_probe(config)  # noqa: E731

        deadline = time.monotonic() + timeout_seconds
        last_err: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                probe_fn()
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.0)
        raise TimeoutError(
            f"Apptainer-managed Postgres did not accept connections on "
            f"localhost:{config.port} within {timeout_seconds}s "
            f"(last probe error: {last_err!r})."
        )

    def _psycopg_probe(self, config: PostgresConfig, psycopg_module) -> None:
        dsn = (
            f"host=127.0.0.1 port={config.port} user={config.user} "
            f"password={config.password} dbname={config.database}"
        )
        with psycopg_module.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1").fetchone()

    def _pg_isready_probe(self, config: PostgresConfig) -> None:
        """Shell out to ``apptainer exec <instance> pg_isready`` as a
        fallback when psycopg is not installed.
        """
        res = self._runner.run(
            [
                self._binary,
                "exec",
                f"instance://{INSTANCE_NAME}",
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-p",
                str(config.port),
                "-U",
                config.user,
                "-d",
                config.database,
            ],
            check=False,
            timeout=5.0,
        )
        if res.returncode != 0:
            raise RuntimeError(f"pg_isready exit={res.returncode}: {res.stderr.strip()}")
