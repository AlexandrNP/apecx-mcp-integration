"""Integration tests for DockerRuntime against a live Docker daemon.

Skipped automatically when the Docker daemon is not reachable — same
pattern as test_postgres_parity.py skipping without APECX_CP_POSTGRES_URL.
"""

from __future__ import annotations

import pytest

from apecx_integration.control_plane.infra.docker_runtime import (
    CONTAINER_NAME,
    DockerRuntime,
)
from apecx_integration.control_plane.infra.runtime import (
    PostgresConfig,
    _docker_daemon_is_up,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_daemon_is_up(),
        reason="Docker daemon not reachable",
    ),
]


@pytest.fixture
def docker_runtime(tmp_path):
    runtime = DockerRuntime()
    cfg = PostgresConfig(data_dir=str(tmp_path / "pgdata"))
    # Ensure a clean slate before the test — previous aborted runs may
    # have left the container up.
    runtime.teardown(cfg, remove_data=False)
    yield runtime, cfg
    # Leave container stopped but keep the named volume around for
    # repeated local runs (tear the volume only when remove_data).
    runtime.teardown(cfg, remove_data=False)


def test_ensure_brings_up_postgres_and_is_running_reports_true(docker_runtime) -> None:
    runtime, cfg = docker_runtime
    assert not runtime.is_postgres_running()
    runtime.ensure_postgres_running(cfg)
    assert runtime.is_postgres_running()


def test_ensure_is_idempotent(docker_runtime) -> None:
    runtime, cfg = docker_runtime
    runtime.ensure_postgres_running(cfg)
    # Second call must not error, must not recreate.
    runtime.ensure_postgres_running(cfg)
    assert runtime.is_postgres_running()


def test_teardown_without_remove_data_stops_container(docker_runtime) -> None:
    runtime, cfg = docker_runtime
    runtime.ensure_postgres_running(cfg)
    runtime.teardown(cfg, remove_data=False)
    assert not runtime.is_postgres_running()


def test_wrong_port_config_raises_before_touching_docker(docker_runtime) -> None:
    runtime, _ = docker_runtime
    cfg_wrong = PostgresConfig(port=5999, data_dir="/tmp/irrelevant")
    with pytest.raises(ValueError, match="5433"):
        runtime.ensure_postgres_running(cfg_wrong)
    # Must not have started anything.
    assert CONTAINER_NAME not in (runtime._compose("ps", check=False).stdout)
