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
  ``PostgresConfig.data_dir`` must be a writable host path; we
  create it on first start. The CI-ephemeral tmpfs override that
  docker-compose.ci.yml gives us has no Apptainer equivalent; if you
  need ephemerality, set ``data_dir`` to a path under ``/tmp`` and
  remove it yourself.
- **No healthcheck built in.** We wait on ``pg_isready`` ourselves by
  ``apptainer exec``-ing into the running instance, or by a
  ``psycopg.connect`` probe from Python.
- **Docker images are pulled via ``docker://``.** First start pays the
  conversion cost (image pulled + converted to SIF). Subsequent starts
  reuse the cached SIF.
- **Networking.** Apptainer shares the host network by default. The
  Postgres port (5433) must be free on the host — same assumption as
  Docker, so no practical difference.

This runtime is built but **not end-to-end tested here** — this dev
machine is macOS and does not have Apptainer. The command-construction
logic is unit-tested; a skipped integration test in
``tests/integration/test_apptainer_runtime.py`` activates when
``apptainer`` is on ``$PATH`` (e.g., on an HPC login node).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from apecx_integration.control_plane.infra.runtime import (
    PostgresConfig,
    RuntimeKind,
)

INSTANCE_NAME = "apecx-cp-postgres"


@dataclass(frozen=True, kw_only=True)
class _CommandResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass
class _SubprocessRunner:
    """Indirection so command construction is unit-testable without
    actually invoking apptainer.
    """

    captured_calls: list[list[str]] = field(default_factory=list)

    def run(
        self, cmd: list[str], *, check: bool = True, timeout: float | None = 120.0
    ) -> _CommandResult:
        self.captured_calls.append(list(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)
        return _CommandResult(
            cmd=cmd, returncode=res.returncode, stdout=res.stdout, stderr=res.stderr
        )


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
        for unit tests and is the natural shape for the "dry run" MCP tool
        we may add later. The actual ``ensure_postgres_running`` method
        executes each command in sequence with real subprocess calls.
        """
        image_uri = f"docker://{config.image}"
        # Bind-mount host data_dir at the container's Postgres data path.
        bind_arg = f"{config.data_dir}:/var/lib/postgresql/data"
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
            # Apptainer has no "volume rm". The app owns the bind-mounted
            # directory, so we rm -rf it. This is the only destructive op
            # in the runtime; caller asks for it explicitly.
            cmds.append(["rm", "-rf", config.data_dir])
        return cmds

    def build_is_running_command(self) -> list[str]:
        return [self._binary, "instance", "list", "--json"]

    # ---- Execution ----------------------------------------------------

    def ensure_postgres_running(self, config: PostgresConfig) -> None:
        Path(config.data_dir).mkdir(parents=True, exist_ok=True)
        if self.is_postgres_running():
            return
        for cmd in self.build_ensure_commands(config):
            self._runner.run(cmd)
        self._wait_for_healthy(config, timeout_seconds=60)

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

    def _wait_for_healthy(self, config: PostgresConfig, *, timeout_seconds: int) -> None:
        """Poll a ``psycopg.connect`` probe until the instance accepts
        connections, or timeout.

        Apptainer has no built-in healthcheck, so we check from outside.
        ``pg_isready`` inside the container would work too but requires an
        extra ``apptainer exec`` roundtrip per probe — the psycopg probe
        is cheaper and catches auth + networking failures in one shot.
        """
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError(
                "ApptainerRuntime healthcheck needs psycopg; install "
                "apecx-integration with the `[dev]` extras or add "
                "`psycopg[binary]` to your production deps."
            ) from e

        dsn = (
            f"host=127.0.0.1 port={config.port} user={config.user} "
            f"password={config.password} dbname={config.database}"
        )
        deadline = time.monotonic() + timeout_seconds
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as conn:
                    conn.execute("SELECT 1").fetchone()
                return
            except psycopg.Error as e:
                last_err = e
                time.sleep(1.0)
        raise TimeoutError(
            f"Apptainer-managed Postgres did not accept connections on "
            f"localhost:{config.port} within {timeout_seconds}s "
            f"(last error: {last_err!r})."
        )
