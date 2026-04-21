"""Integration test for ApptainerRuntime.

## Running modes

- **Direct Apptainer on PATH** — typical HPC login node. Uses the
  default ``_SubprocessRunner``.
- **Apptainer-via-Lima on macOS** — the official macOS install path
  per https://apptainer.org/docs/admin/main/installation.html#mac.
  We wrap commands through ``limactl shell apptainer --``. Lima
  reverse-mounts ``$HOME`` **read-only**, so tests use a VM-local
  scratch under ``/tmp/`` inside the VM. Lima auto-forwards any
  port bound on the VM's ``127.0.0.1`` back to the macOS host.

Opt-out via ``APECX_SKIP_APPTAINER_IT=1``.

## What this suite actually covers

The suite verifies the **Apptainer runtime plumbing**: ``instance start
/ stop / list`` drive correctly, bind-mount paths land correctly
(subdir convention), mkdir goes through the runner. It uses a
lightweight ``docker://alpine`` image for the instance-lifecycle tests
because:

- It's ~5MB and pulls / converts to SIF in seconds.
- ``alpine`` starts an ``appinit`` that keeps the instance alive,
  which is all we need to verify the lifecycle.

## What this suite deliberately does NOT cover

**Running Postgres under Apptainer via ``docker://postgres:16-alpine``
does not work.** Reason: ``apptainer instance start`` spawns Apptainer's
``appinit`` instead of the image's ENTRYPOINT, so the Postgres
``docker-entrypoint.sh`` never runs. Getting a working Postgres under
Apptainer requires a **custom SIF image** built for non-root execution
(user-namespace-friendly PGDATA, no ``gosu`` dependency on a root-owned
postgres user, explicit ``%runscript`` that invokes ``postgres``). That
work is tracked in ``docs/future_work.md`` under "Apptainer Postgres
runtime image" — it is not a blocker for the Docker path on scientist
laptops, which is the priority deployment.

A test that tries to probe Postgres through Apptainer is included
below as ``xfail`` with a pointer at the future-work ticket, so the
moment a custom SIF lands this test starts passing and CI flags it.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from apecx_integration.control_plane.infra.apptainer_runtime import (
    ApptainerRuntime,
    _SubprocessRunner,
)
from apecx_integration.control_plane.infra.runtime import PostgresConfig

LIMA_VM_NAME = "apptainer"
# Small, fast-pulling image that runs an "appinit"-compatible no-op so
# we can exercise start/stop/list without the Postgres-specific
# complication.
SMOKE_IMAGE = "docker://alpine:3.20"


def _direct_apptainer_on_path() -> str | None:
    for name in ("apptainer", "singularity"):
        if shutil.which(name):
            return name
    return None


def _lima_apptainer_running() -> bool:
    if not shutil.which("limactl"):
        return False
    try:
        res = subprocess.run(
            ["limactl", "list", "--format", "{{.Name}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if res.returncode != 0:
        return False
    for line in res.stdout.splitlines():
        name, _, status = line.partition("\t")
        if name.strip() == LIMA_VM_NAME and status.strip().lower() == "running":
            return True
    return False


def _apptainer_available() -> bool:
    if os.environ.get("APECX_SKIP_APPTAINER_IT"):
        return False
    return _direct_apptainer_on_path() is not None or _lima_apptainer_running()


def _is_lima_mode() -> bool:
    return _direct_apptainer_on_path() is None and _lima_apptainer_running()


class _LimaShellRunner(_SubprocessRunner):
    """Routes commands through ``limactl shell <vm> --`` so macOS dev
    boxes running Apptainer inside Lima can exercise the real runtime.
    """

    def __init__(self, *, vm_name: str = LIMA_VM_NAME) -> None:
        super().__init__()
        self._vm_name = vm_name

    def run(self, cmd, *, check=True, timeout=300.0):
        return super().run(
            ["limactl", "shell", self._vm_name, "--", *cmd],
            check=check,
            timeout=timeout,
        )


def _make_runtime_for_this_host() -> ApptainerRuntime:
    direct = _direct_apptainer_on_path()
    if direct is not None:
        return ApptainerRuntime(binary=direct)
    return ApptainerRuntime(binary="apptainer", runner=_LimaShellRunner())


def _pick_data_parent(tmp_path) -> str:
    """Direct-Apptainer: pytest ``tmp_path``. Lima: VM-local /tmp path
    tagged with pid so parallel runs don't collide.
    """
    if _is_lima_mode():
        return f"/tmp/apecx_apptainer_it_{os.getpid()}"
    return str(tmp_path / "pgparent")


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _apptainer_available(),
        reason=(
            "Apptainer is neither on $PATH nor reachable via a running "
            "Lima VM named 'apptainer'. Install via the apptainer docs, "
            "or set APECX_SKIP_APPTAINER_IT=1 to silence this skip."
        ),
    ),
]


# ---- Lifecycle smoke: proves the runtime plumbing works -----------------


@pytest.fixture
def smoke_runtime(tmp_path):
    """Runtime pointed at a small smoke image. Proves the runtime's
    command pipeline works end-to-end without depending on Postgres
    starting correctly under Apptainer.
    """
    runtime = _make_runtime_for_this_host()
    cfg = PostgresConfig(data_dir=_pick_data_parent(tmp_path), image="alpine:3.20")
    # Smoke image uri is docker://alpine:3.20; PostgresConfig.image is
    # passed to build_ensure_commands which prepends docker://. We
    # verify the build path, then execute.
    runtime.teardown(cfg, remove_data=True)
    yield runtime, cfg
    runtime.teardown(cfg, remove_data=True)


def test_instance_start_stop_list_via_alpine_smoke(smoke_runtime) -> None:
    """Bring up an instance with a lightweight image, confirm
    is_postgres_running() reports it, tear it down, confirm it's gone.

    ``ensure_postgres_running`` also triggers the psycopg probe, which
    will fail (alpine isn't a Postgres server). We call the smaller
    building blocks directly and skip the healthcheck so this smoke
    genuinely tests the runtime plumbing without demanding Postgres.
    """
    runtime, cfg = smoke_runtime
    # Drive the runtime's command pipeline without the Postgres probe.
    runtime._runner.run(["mkdir", "-p", cfg.data_dir + "/apecx_cp_postgres"])
    for cmd in runtime.build_ensure_commands(cfg):
        runtime._runner.run(cmd)
    assert runtime.is_postgres_running()

    for cmd in runtime.build_teardown_commands(cfg, remove_data=False):
        runtime._runner.run(cmd, check=False)
    assert not runtime.is_postgres_running()


def test_teardown_bounded_to_managed_subdir(smoke_runtime) -> None:
    """Prove that ``--remove-data`` rm -rf's only the managed subdir,
    never the parent ``data_dir``. We create a sentinel file outside
    the managed subdir under ``data_dir`` and verify it survives.
    """
    runtime, cfg = smoke_runtime
    managed = cfg.data_dir + "/apecx_cp_postgres"
    sibling = cfg.data_dir + "/SHOULD_NOT_BE_DELETED"
    runtime._runner.run(["mkdir", "-p", managed])
    runtime._runner.run(["bash", "-c", f"echo canary > {sibling}"])
    runtime._runner.run(["bash", "-c", f"echo data > {managed}/marker"])

    # Now teardown with remove_data=True.
    for cmd in runtime.build_teardown_commands(cfg, remove_data=True):
        runtime._runner.run(cmd, check=False)

    # managed dir should be gone; the sibling canary must survive.
    marker_res = runtime._runner.run(
        ["bash", "-c", f"test -f {managed}/marker && echo PRESENT || echo GONE"],
        check=False,
    )
    assert "GONE" in marker_res.stdout
    canary_res = runtime._runner.run(
        ["bash", "-c", f"test -f {sibling} && echo PRESENT || echo GONE"],
        check=False,
    )
    assert "PRESENT" in canary_res.stdout


# ---- Postgres-through-Apptainer: xfail with a clear pointer --------------


def _port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
        except OSError:
            return False
    return True


@pytest.mark.xfail(
    strict=True,
    reason=(
        "docker://postgres:16-alpine under `apptainer instance start` "
        "does not run the image's ENTRYPOINT (appinit takes PID 1). "
        "A custom Postgres SIF is required; see "
        "docs/future_work.md -> 'Apptainer Postgres runtime image'."
    ),
)
def test_postgres_accepts_connections_under_apptainer(tmp_path) -> None:
    import psycopg

    # If port 5433 is already held (typically by a running Docker
    # Postgres from other tests), we can't distinguish "Apptainer
    # brought up Postgres" from "the probe connected to something
    # else". Skip in that case — the test is only meaningful when the
    # target port is owned by whatever Apptainer does or doesn't start.
    if _port_in_use("127.0.0.1", 5433):
        pytest.skip(
            "localhost:5433 already held by a non-Apptainer listener "
            "(probably the Docker Postgres started for other tests). "
            "This xfail canary only means something when the port is "
            "free before ensure_postgres_running runs."
        )

    runtime = _make_runtime_for_this_host()
    cfg = PostgresConfig(data_dir=_pick_data_parent(tmp_path))
    runtime.teardown(cfg, remove_data=True)
    try:
        runtime.ensure_postgres_running(cfg)
        dsn = (
            f"host=127.0.0.1 port={cfg.port} user={cfg.user} "
            f"password={cfg.password} dbname={cfg.database}"
        )
        with psycopg.connect(dsn) as conn:
            row = conn.execute("SELECT 1").fetchone()
        assert row == (1,)
    finally:
        runtime.teardown(cfg, remove_data=True)
