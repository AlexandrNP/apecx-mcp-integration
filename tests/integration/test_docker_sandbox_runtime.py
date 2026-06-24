"""T13b Docker-sandbox runtime integration tests.

Two groups:

1. **Guard tests** — verify the scaffold refuses to run when Docker
   isn't on ``$PATH`` or the execute-gate env var isn't set. These run
   on any machine, including CI. They do NOT invoke Docker.

2. **Live-sandbox test** — opt-in via ``APECX_T13B_SANDBOX_EXECUTE=1``
   AND Docker daemon reachable. Runs a trivial Python command inside
   the hardened container and asserts exit-code + captured stdout.
   Skipped automatically when either condition is absent.

**Brutal truth:** a "the container actually isolates" test would
attempt to open a TCP socket, open ``/etc/passwd``, and assert both
fail. That test is high-value but needs to live alongside a
controlled Docker environment (specific daemon version, specific
seccomp profile); adding it here without that controlled environment
risks false-greens. Phase-3 concern.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest

from apecx_integration.composition.docker_sandbox import (
    DockerSandboxRunner,
    SandboxConfig,
    SandboxNotAvailableError,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Guard tests — safe on any machine
# ---------------------------------------------------------------------------


def test_runner_raises_without_execute_env_var(monkeypatch):
    """Without APECX_T13B_SANDBOX_EXECUTE=1, run() must refuse —
    even when docker IS on PATH. Prevents accidental Docker
    invocation from a bare ``pytest`` run.
    """
    monkeypatch.delenv("APECX_T13B_SANDBOX_EXECUTE", raising=False)
    runner = DockerSandboxRunner()

    # Pretend docker is available so we can reach the env-var check
    # regardless of the test host.
    monkeypatch.setattr(
        "apecx_integration.composition.docker_sandbox._docker_on_path",
        lambda: True,
    )

    with pytest.raises(SandboxNotAvailableError, match="APECX_T13B_SANDBOX_EXECUTE"):
        asyncio.run(runner.run(["true"]))


def test_runner_raises_when_docker_not_on_path(monkeypatch):
    """Whether or not the execute-gate env var is set, absence of
    the docker binary must surface as SandboxNotAvailableError —
    not some unrelated FileNotFoundError from subprocess_exec."""
    monkeypatch.setenv("APECX_T13B_SANDBOX_EXECUTE", "1")
    monkeypatch.setattr(
        "apecx_integration.composition.docker_sandbox._docker_on_path",
        lambda: False,
    )
    runner = DockerSandboxRunner()
    with pytest.raises(SandboxNotAvailableError, match="docker binary not found"):
        asyncio.run(runner.run(["true"]))


# ---------------------------------------------------------------------------
# Live-sandbox test — opt-in
# ---------------------------------------------------------------------------


def _docker_daemon_reachable() -> bool:
    """Return True when ``docker info`` returns 0 within 5s.

    `docker` on $PATH is necessary but not sufficient — Docker Desktop
    may be installed but not running, leaving a working CLI that fails
    every command.
    """
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5.0,
            check=False,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


_OPT_IN_REASON = (
    "T13b live sandbox test is opt-in: set APECX_T13B_SANDBOX_EXECUTE=1 "
    "AND have a reachable Docker daemon (e.g. Docker Desktop running). "
    "This test pulls a ~40MB image on first run."
)


@pytest.mark.skipif(
    os.environ.get("APECX_T13B_SANDBOX_EXECUTE") != "1" or not _docker_daemon_reachable(),
    reason=_OPT_IN_REASON,
)
def test_sandbox_runs_trivial_python_command():
    """Full round-trip: build argv, invoke Docker, capture stdout."""
    runner = DockerSandboxRunner(
        SandboxConfig(
            image="python:3.12-slim",
            timeout_seconds=120.0,  # first run pulls the image
        ),
    )
    result = asyncio.run(
        runner.run(
            ["python", "-c", "print('t13b-ok')"],
        )
    )
    assert result.exit_code == 0, (
        f"sandbox exited {result.exit_code}\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "t13b-ok" in result.stdout
    assert not result.killed_by_timeout
    assert result.duration_seconds > 0


@pytest.mark.skipif(
    os.environ.get("APECX_T13B_SANDBOX_EXECUTE") != "1" or not _docker_daemon_reachable(),
    reason=_OPT_IN_REASON,
)
def test_sandbox_kills_runaway_via_timeout():
    """A command that sleeps past the timeout must be killed and the
    result must report ``killed_by_timeout=True``. This is the
    first line of defense against resource-exhaustion (threat-model
    row 5) — without a functioning timeout + kill, a runaway
    container could sit burning CPU until the operator notices.
    """
    runner = DockerSandboxRunner(
        SandboxConfig(
            image="python:3.12-slim",
            timeout_seconds=2.0,
        ),
    )
    result = asyncio.run(
        runner.run(
            ["python", "-c", "import time; time.sleep(30)"],
        )
    )
    assert result.killed_by_timeout, (
        f"timeout did not fire; exit_code={result.exit_code}, "
        f"duration={result.duration_seconds:.2f}s"
    )
    # exit_code of a killed docker-run client is implementation-specific;
    # the contract we pin is just that duration stays close to the
    # timeout — not drifting into "slept all 30 seconds".
    assert result.duration_seconds < 10.0


@pytest.mark.skipif(
    os.environ.get("APECX_T13B_SANDBOX_EXECUTE") != "1" or not _docker_daemon_reachable(),
    reason=_OPT_IN_REASON,
)
def test_admission_cap_serializes_concurrent_spawns(monkeypatch):
    """Real-spawn parity for the host-side admission cap (deployment-hardening Task A).

    4 concurrent sandbox containers under ``APECX_MAX_CONCURRENT_DOCKER_RUNS=2`` must run
    in ~2 waves, not one — proving the semaphore is correctly wired around the real
    ``run()`` spawn path. Without the cap all 4 would run at once (~1 wave). The unit
    test ``test_container_admission.test_caps_concurrency_from_env`` pins the semaphore
    itself; this pins that the spawn path actually holds a slot.
    """
    import time as _time

    from apecx_integration.composition.runtime import container_admission as ca

    monkeypatch.setenv(ca.ENV_VAR, "2")
    ca._reset_for_test()
    try:
        runner = DockerSandboxRunner(
            SandboxConfig(image="python:3.12-slim", timeout_seconds=120.0),
        )

        async def _sleep_two():
            return await runner.run(["python", "-c", "import time; time.sleep(2)"])

        # Warm the image cache once so the timed batch is not skewed by a ~40MB pull.
        asyncio.run(runner.run(["python", "-c", "print('warm')"]))

        async def _batch():
            return await asyncio.gather(*[_sleep_two() for _ in range(4)])

        start = _time.monotonic()
        results = asyncio.run(_batch())
        wall = _time.monotonic() - start

        assert all(r.exit_code == 0 for r in results), [r.exit_code for r in results]
        # 2 waves of ~2s sleeps (+per-container docker-run overhead) clears 4s; a single
        # all-concurrent wave would finish near ~3s. Generous margin for timing noise.
        assert wall >= 4.0, f"expected ~2 serialized waves, got {wall:.1f}s (cap not holding?)"
    finally:
        ca._reset_for_test()


async def _spawn_raises_oserror(*_args, **_kwargs):
    # Simulates create_subprocess_exec failing — e.g. OSError when fork() cannot
    # allocate memory under host pressure (exactly the exhaustion case).
    raise OSError("simulated fork() allocation failure")


def test_spawn_failure_releases_admission_slot(monkeypatch):
    """If the container spawn itself raises, the admission slot must be released, not
    leaked. A leak would permanently drain the fixed-size pool and deadlock every later
    code-exec call — a worse failure than the exhaustion this guard mitigates.

    Regression for the review-gate FAIL where the sandbox spawn sat OUTSIDE the
    try/finally. Runs anywhere: the spawn is mocked, no real Docker.
    """
    from apecx_integration.composition.runtime import container_admission as ca

    monkeypatch.setenv("APECX_T13B_SANDBOX_EXECUTE", "1")
    monkeypatch.setenv(ca.ENV_VAR, "1")  # pool of 1 -> a single leak drains it
    ca._reset_for_test()
    monkeypatch.setattr(
        "apecx_integration.composition.docker_sandbox._docker_on_path", lambda: True
    )
    monkeypatch.setattr(
        "apecx_integration.composition.docker_sandbox.asyncio.create_subprocess_exec",
        _spawn_raises_oserror,
    )
    runner = DockerSandboxRunner(SandboxConfig(image="python:3.12-slim"))

    async def _drive():
        with pytest.raises(OSError):
            await runner.run(["python", "-c", "print(1)"])

        # The pool (size 1) must be free again. If the slot leaked, acquiring blocks
        # forever and asyncio.wait_for raises TimeoutError -> the test fails.
        async def _acquire_once():
            async with ca.acquire_container_slot():
                pass

        await asyncio.wait_for(_acquire_once(), timeout=2.0)

    try:
        asyncio.run(_drive())
    finally:
        ca._reset_for_test()
