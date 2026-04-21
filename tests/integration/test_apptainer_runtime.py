"""Integration test for ApptainerRuntime.

This machine (the one running Claude Code authoring this code) is macOS
and does not have Apptainer. The test is therefore **skipped by default**
and only activates on a host that actually has ``apptainer`` (or the
older ``singularity``) on ``$PATH`` — typically an HPC login node.

The unit suite (``tests/unit/test_apptainer_commands.py``) pins the
exact argv we issue. This file fills the end-to-end side of the
mock/integration parity rule: on any machine where Apptainer actually
exists, this test brings up a real Postgres instance via Apptainer and
round-trips a query.

To run on an HPC node:
    pytest tests/integration/test_apptainer_runtime.py -v

To deliberately skip even if Apptainer is present (e.g., to run the
faster Docker-only test pass):
    APECX_SKIP_APPTAINER_IT=1 pytest ...
"""

from __future__ import annotations

import os
import shutil

import pytest
from apecx_integration.control_plane.infra.apptainer_runtime import ApptainerRuntime
from apecx_integration.control_plane.infra.runtime import PostgresConfig


def _apptainer_available() -> bool:
    if os.environ.get("APECX_SKIP_APPTAINER_IT"):
        return False
    return bool(shutil.which("apptainer") or shutil.which("singularity"))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _apptainer_available(),
        reason=(
            "Apptainer/Singularity not on $PATH; this test runs only on "
            "hosts where the runtime is actually installed (typically an "
            "HPC login node)."
        ),
    ),
]


@pytest.fixture
def apptainer_runtime(tmp_path):
    binary = "apptainer" if shutil.which("apptainer") else "singularity"
    runtime = ApptainerRuntime(binary=binary)
    cfg = PostgresConfig(data_dir=str(tmp_path / "pgdata"))
    runtime.teardown(cfg, remove_data=False)
    yield runtime, cfg
    runtime.teardown(cfg, remove_data=True)


def test_ensure_brings_up_postgres(apptainer_runtime) -> None:
    runtime, cfg = apptainer_runtime
    runtime.ensure_postgres_running(cfg)
    assert runtime.is_postgres_running()


def test_ensure_is_idempotent(apptainer_runtime) -> None:
    runtime, cfg = apptainer_runtime
    runtime.ensure_postgres_running(cfg)
    runtime.ensure_postgres_running(cfg)
    assert runtime.is_postgres_running()


def test_postgres_actually_accepts_connections(apptainer_runtime) -> None:
    """Probe via psycopg that a client can connect + query."""
    import psycopg

    runtime, cfg = apptainer_runtime
    runtime.ensure_postgres_running(cfg)
    dsn = (
        f"host=127.0.0.1 port={cfg.port} user={cfg.user} "
        f"password={cfg.password} dbname={cfg.database}"
    )
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT 1").fetchone()
    assert row == (1,)
