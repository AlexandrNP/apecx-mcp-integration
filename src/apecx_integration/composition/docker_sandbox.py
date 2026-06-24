"""T13b — runtime Docker sandbox (scaffold).

Phase-2 runtime isolation backstop for T13's static import-whitelist
scanner (``sandbox.py``). See ``docs/t13b_sandbox_design.md`` for the
threat model + flag rationale.

**This module is a scaffold.** The ``build_docker_sandbox_command``
argv construction is complete and unit-tested; the runtime
``DockerSandboxRunner.run`` exists but is gated behind the
``APECX_T13B_SANDBOX_EXECUTE`` env var to prevent accidental Docker
invocation in CI. Composer wiring to actually USE this runner is
Phase-3 work.

API:

    from apecx_integration.composition.docker_sandbox import (
        DockerSandboxRunner,
        SandboxConfig,
        SandboxResult,
        SandboxNotAvailableError,
        build_docker_sandbox_command,
    )
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from apecx_integration.composition.runtime.container_admission import acquire_container_slot

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SandboxConfig:
    """Tunable isolation parameters.

    Defaults are the threat-model-justified values from
    ``docs/t13b_sandbox_design.md``. Override judiciously — weakening
    any field requires a matching update to the threat-model table.
    """

    image: str = "python:3.12-slim"
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 256
    user: str = "65534:65534"  # nobody:nogroup
    network: str = "none"
    tmpfs_size: str = "256m"
    workdir: str = "/work"
    timeout_seconds: float = 60.0
    # Audit §1.7: kill_timeout was hardcoded at 5.0 inside the kill
    # fallback. Slow Docker daemons cascade timeouts; making this
    # tunable lets operators on stressed CI hosts grant the kill
    # more time before declaring the container abandoned.
    kill_timeout_seconds: float = 5.0
    extra_run_args: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, kw_only=True)
class SandboxResult:
    """Outcome of a single sandboxed execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    killed_by_timeout: bool
    # Audit §1.8: pre-fix the docker-kill fallback was a bare
    # ``except Exception: pass`` — kill failure (slow daemon,
    # permission issue, container already gone) returned
    # ``killed_by_timeout=True`` even when the kill itself errored
    # and the container might still be alive. ``kill_succeeded``
    # surfaces that distinction so the caller knows whether
    # post-timeout state is clean.
    kill_succeeded: bool = True
    argv: tuple[str, ...]  # the exact docker run invocation (for audit)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SandboxError(Exception):
    """Base class for sandbox errors."""


class SandboxNotAvailableError(SandboxError):
    """Raised when the sandbox cannot be invoked.

    Two known causes:
    - ``docker`` binary is not on ``$PATH``.
    - ``APECX_T13B_SANDBOX_EXECUTE`` env var is not set. The scaffold
      requires this explicit opt-in so that CI runs of the test suite
      cannot accidentally invoke Docker on a developer's host.
    """


# ---------------------------------------------------------------------------
# Command construction — pure, no I/O, fully unit-testable
# ---------------------------------------------------------------------------


def build_docker_sandbox_command(
    command: Sequence[str],
    *,
    input_host_path: Path | None,
    container_name: str | None = None,
    config: SandboxConfig | None = None,
) -> list[str]:
    """Construct the ``docker run`` argv for a hardened sandbox invocation.

    Every flag corresponds to a row in the threat-model table in
    ``docs/t13b_sandbox_design.md``. If you remove or weaken a flag
    here, update that table and the unit tests in lockstep.

    Args:
        command: The command to run inside the container, e.g.
            ``["python", "script.py"]``. Interpreted relative to
            ``config.workdir``.
        input_host_path: Host directory to bind-mount **read-only**
            at ``config.workdir`` inside the container. ``None``
            means no input mount (for smoke-testing the container
            itself).
        container_name: Optional explicit container name. If None,
            Docker will auto-generate one. Passing a name is useful
            when the caller wants to ``docker kill <name>`` on
            cancellation; Phase-3 will wire that path.
        config: Isolation parameters. ``None`` uses the defaults.

    Returns:
        The argv (``list[str]``) ready to pass to ``subprocess.run``.
    """
    cfg = config or SandboxConfig()

    argv: list[str] = [
        "docker",
        "run",
        "--rm",
        "--network",
        cfg.network,
        "--read-only",
        "--tmpfs",
        f"/tmp:size={cfg.tmpfs_size},mode=1777",
        "--memory",
        f"{cfg.memory_mb}m",
        # --memory-swap equal to --memory disables swap. Without this,
        # the memory cap is unenforced whenever swap is available.
        "--memory-swap",
        f"{cfg.memory_mb}m",
        "--cpus",
        str(cfg.cpus),
        "--pids-limit",
        str(cfg.pids_limit),
        "--user",
        cfg.user,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        # Seccomp: Docker's built-in default profile applies automatically
        # when no `--security-opt seccomp=...` flag is passed. The literal
        # value `seccomp=default` is NOT a Docker keyword — Docker Desktop
        # on Mac treats it as a file path and the container fails to
        # start ("opening seccomp profile (default) failed: open default:
        # no such file"). To DISABLE seccomp, pass `seccomp=unconfined`.
        # We deliberately do not pass the flag here so the default
        # profile (~60 syscalls blocked: ptrace, mount, unshare, reboot,
        # keyctl, etc.) applies. The test_no_seccomp_unconfined test
        # below verifies we never accidentally use `unconfined`.
        "--workdir",
        cfg.workdir,
    ]

    if input_host_path is not None:
        # resolve() + readonly. Path must be absolute on the host; Docker
        # rejects relative bind sources.
        src = input_host_path.resolve()
        argv += [
            "--mount",
            f"type=bind,source={src},target={cfg.workdir},readonly",
        ]

    if container_name is not None:
        argv += ["--name", container_name]

    argv += list(cfg.extra_run_args)
    argv.append(cfg.image)
    argv += list(command)
    return argv


# ---------------------------------------------------------------------------
# Runtime wrapper — gated, opt-in
# ---------------------------------------------------------------------------


_EXECUTE_ENV_VAR = "APECX_T13B_SANDBOX_EXECUTE"


def _docker_on_path() -> bool:
    return shutil.which("docker") is not None


class DockerSandboxRunner:
    """Runs commands inside a hardened Docker sandbox.

    Scaffold caveats — this class is deliberately under-featured:
    - No cancellation path (ask the container to die via
      ``docker kill``). Phase-3.
    - No ``/out`` writable-mount convention. Phase-3.
    - No image-pin-by-digest enforcement. Phase-3.

    The intent today is to lock in the argv shape (via
    ``build_docker_sandbox_command``) and make it cheap for Phase-3
    to add the missing pieces without reshaping the API.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    @property
    def config(self) -> SandboxConfig:
        return self._config

    def _check_available(self) -> None:
        if not _docker_on_path():
            raise SandboxNotAvailableError(
                "docker binary not found on $PATH; install Docker Desktop "
                "or the Docker CLI before invoking DockerSandboxRunner."
            )
        if os.environ.get(_EXECUTE_ENV_VAR) != "1":
            raise SandboxNotAvailableError(
                f"{_EXECUTE_ENV_VAR}=1 is required to execute the sandbox. "
                "This explicit opt-in exists so CI runs of the test suite "
                "do not accidentally invoke Docker. Unit tests of argv "
                "construction call build_docker_sandbox_command directly "
                "and do not trip this guard."
            )

    async def run(
        self,
        command: Sequence[str],
        *,
        input_host_path: Path | None = None,
        container_name: str | None = None,
    ) -> SandboxResult:
        """Execute ``command`` inside the sandbox.

        Refuses to run unless ``APECX_T13B_SANDBOX_EXECUTE=1`` is set.
        """
        self._check_available()
        argv = build_docker_sandbox_command(
            command,
            input_host_path=input_host_path,
            container_name=container_name,
            config=self._config,
        )

        start = time.monotonic()
        # Bound simultaneous code-exec containers process-wide (open-endpoint
        # exhaustion guard); released in the ``finally`` below after the container is
        # fully reaped on every path. The spawn is INSIDE the ``try`` so a failed
        # spawn (e.g. OSError when fork() can't allocate memory — exactly the
        # exhaustion case) still releases the slot rather than leaking it.
        slot = acquire_container_slot()
        await slot.acquire()
        killed = False
        kill_succeeded = True  # only meaningful when killed=True
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._config.timeout_seconds,
            )
        except TimeoutError:
            killed = True
            kill_succeeded = False
            # Ask the container to die. ``--rm`` takes care of removal.
            if container_name is not None:
                # Audit §1.7+§1.8: the kill timeout is now configurable
                # (was hardcoded 5.0) and the exception handling is no
                # longer a bare ``except Exception: pass``. We catch
                # specific exceptions, log them, and surface the
                # outcome via ``kill_succeeded`` on the result so the
                # caller knows whether the container is definitely
                # dead.
                try:
                    subprocess.run(
                        ["docker", "kill", container_name],
                        check=False,
                        timeout=self._config.kill_timeout_seconds,
                        capture_output=True,
                    )
                    kill_succeeded = True
                except subprocess.TimeoutExpired:
                    log.warning(
                        "docker kill %s timed out after %.1fs; container may still be running.",
                        container_name,
                        self._config.kill_timeout_seconds,
                    )
                except OSError as exc:
                    log.warning(
                        "docker kill %s failed with OSError (%s); container may still be running.",
                        container_name,
                        exc,
                    )
                # Other exceptions (e.g., FileNotFoundError if docker
                # binary disappeared mid-run) propagate — those are
                # not "container left running"; they're environment
                # corruption worth surfacing.
            # Kill the docker-run client process itself, whatever state
            # it's in. Without this, communicate() below can still hang.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            stdout_b, stderr_b = await proc.communicate()
        finally:
            slot.release()

        duration = time.monotonic() - start
        returncode = proc.returncode if proc.returncode is not None else -1

        return SandboxResult(
            exit_code=returncode,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_seconds=duration,
            killed_by_timeout=killed,
            kill_succeeded=kill_succeeded,
            argv=tuple(argv),
        )


__all__ = [
    "DockerSandboxRunner",
    "SandboxConfig",
    "SandboxError",
    "SandboxNotAvailableError",
    "SandboxResult",
    "build_docker_sandbox_command",
]
