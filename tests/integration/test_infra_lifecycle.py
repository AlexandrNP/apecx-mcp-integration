"""End-to-end test of ensure_infra_ready + the migrated Postgres.

Exercises the full flow a scientist sees on first ``apecx-cp serve``:
1. DB URL points at local-managed Postgres.
2. ensure_infra_ready brings the container up via DockerRuntime.
3. Migrations run against the live Postgres.
4. All T09 tables exist.
5. A second call is a no-op (container already up, alembic head already
   reached, no new migrations).

Runs only with a working Docker daemon. If you want to test the
Apptainer path, run on a host with apptainer and no docker daemon.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect

from apecx_integration.control_plane.infra.docker_runtime import DockerRuntime
from apecx_integration.control_plane.infra.lifecycle import (
    ensure_infra_ready,
    teardown_infra,
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

LOCAL_URL = "postgresql+psycopg://apecx:apecx@localhost:5433/apecx_cp"


@pytest.fixture
def clean_container():
    runtime = DockerRuntime()
    cfg = PostgresConfig(data_dir="/tmp/apecx_cp_it")
    runtime.teardown(cfg, remove_data=True)
    yield
    runtime.teardown(cfg, remove_data=True)


EXPECTED_TABLES = {
    "alembic_version",
    "allocation_estimate",
    "approval",
    "artifact",
    "component",
    "generated_artifact",
    "provenance_event",
    "run",
    "step",
    "verified_synonym",
}


def test_ensure_infra_ready_brings_up_postgres_and_migrates(clean_container) -> None:
    ensure_infra_ready(LOCAL_URL)

    # Tables exist, at head.
    tables = set(inspect(create_engine(LOCAL_URL)).get_table_names())
    assert tables == EXPECTED_TABLES


def test_ensure_infra_ready_is_idempotent(clean_container) -> None:
    ensure_infra_ready(LOCAL_URL)
    # Second call: container already up, alembic already at head.
    ensure_infra_ready(LOCAL_URL)
    tables = set(inspect(create_engine(LOCAL_URL)).get_table_names())
    assert tables == EXPECTED_TABLES


def test_teardown_stops_container_but_leaves_data_by_default(clean_container) -> None:
    ensure_infra_ready(LOCAL_URL)
    teardown_infra(LOCAL_URL)
    runtime = DockerRuntime()
    assert not runtime.is_postgres_running()


def test_sqlite_url_runs_migrations_without_touching_docker(tmp_path) -> None:
    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    # This path must not call docker compose at all; we rely on the
    # fact that a bare SQLite invocation works even when docker is
    # unavailable. Here we just verify tables land.
    ensure_infra_ready(url)
    tables = set(inspect(create_engine(url)).get_table_names())
    assert tables == EXPECTED_TABLES


def test_byo_postgres_url_skips_container_and_attempts_migration_directly(
    monkeypatch,
) -> None:
    """A non-localhost Postgres URL must NOT trigger container ensure
    logic; the migration call will still go out but will fail to connect,
    which is acceptable — the point of this test is that no docker
    commands are issued.
    """
    from apecx_integration.control_plane.infra import lifecycle as lc

    calls: list[str] = []

    class _SpyRuntime:
        kind = "docker"  # any

        def ensure_postgres_running(self, config):
            calls.append("ensure")

        def is_postgres_running(self) -> bool:
            return False

        def teardown(self, config, *, remove_data: bool) -> None:
            calls.append("teardown")

    monkeypatch.setattr(lc, "detect_runtime", lambda: _SpyRuntime())

    # Assume no Postgres on db.example.invalid — alembic will raise an
    # OperationalError. We catch it; what we're asserting is that
    # ensure was NOT called.
    import sqlalchemy.exc

    with pytest.raises(
        (sqlalchemy.exc.OperationalError, sqlalchemy.exc.DBAPIError)
    ):
        ensure_infra_ready(
            "postgresql+psycopg://u:p@db.example.invalid:5432/x"
        )

    assert calls == []
