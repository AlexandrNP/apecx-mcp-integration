"""T09 AC7: the SQLAlchemy models and migrations work against real Postgres.

Reads ``APECX_CP_POSTGRES_URL`` from the environment. If unset, the test
is skipped with a message pointing the developer at ``docker compose up
-d postgres``. On CI/local-dev the URL is expected to be:

    postgresql+psycopg://apecx:apecx@localhost:5433/apecx_cp

This is a real integration test against a live Postgres (docker-compose
service). No mocks.

Scope (deliberately focused, not the full suite):
- alembic upgrade head + downgrade -1 + upgrade head on Postgres.
- All 10 tables created; circular FK run.workflow_config_id -> artifact.id
  present and enforced.
- ProvenanceRecorder hash-chain roundtrip on Postgres (same behavior as
  SQLite).

Full-suite parity is a future commit beyond T09; this is "the schema and
core component work on Postgres," which is what AC7 requires.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
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

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres_url() -> str:
    url = os.environ.get("APECX_CP_POSTGRES_URL")
    if not url:
        pytest.skip(
            "APECX_CP_POSTGRES_URL not set — run "
            "`docker compose up -d postgres` and set "
            "APECX_CP_POSTGRES_URL=postgresql+psycopg://apecx:apecx@localhost:5433/apecx_cp"
        )
    # The env var can be set while the container is down (e.g., after a
    # lifecycle test tore it down). Skip in that case instead of raising
    # so the rest of the suite stays green.
    from sqlalchemy.exc import OperationalError

    try:
        probe = create_engine(url, future=True, connect_args={"connect_timeout": 2})
        with probe.connect():
            pass
        probe.dispose()
    except OperationalError as e:
        pytest.skip(f"Postgres at {url} is not reachable: {e}")
    return url


@pytest.fixture
def clean_postgres(postgres_url: str) -> str:
    """Drop and recreate the public schema so each test starts from empty."""
    admin_engine = create_engine(postgres_url, future=True, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    admin_engine.dispose()
    return postgres_url


def _alembic_cfg(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def test_alembic_roundtrip_on_postgres(clean_postgres: str) -> None:
    url = clean_postgres
    cfg = _alembic_cfg(url)

    command.upgrade(cfg, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert tables == EXPECTED_TABLES

    # Migrations 0002+ landed during the 2026-04-26 adversarial-async
    # hunt; ``-1`` only reverts ONE step now. Use ``base`` to fully
    # downgrade so the post-condition (only alembic_version remains)
    # holds for any future migration count.
    command.downgrade(cfg, "base")
    tables_after = set(inspect(create_engine(url)).get_table_names())
    assert tables_after == {"alembic_version"}, tables_after

    command.upgrade(cfg, "head")
    tables_again = set(inspect(create_engine(url)).get_table_names())
    assert tables_again == EXPECTED_TABLES


def test_circular_fk_enforced_on_postgres(clean_postgres: str) -> None:
    """Postgres enforces FKs by default (unlike SQLite which needs PRAGMA).
    Inserting a run with a bogus workflow_config_id must fail.
    """
    url = clean_postgres
    command.upgrade(_alembic_cfg(url), "head")

    engine = make_engine(url)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, workflow_config_id, status, created_at) "
                "VALUES (:id, :uid, :wid, 'PENDING', :ts)"
            ),
            {
                "id": str(uuid4()),
                "uid": "tester",
                "wid": str(uuid4()),  # non-existent artifact id
                "ts": datetime.now(UTC).isoformat(),
            },
        )


def test_provenance_chain_on_postgres(clean_postgres: str) -> None:
    url = clean_postgres
    command.upgrade(_alembic_cfg(url), "head")

    engine = make_engine(url)
    recorder = ProvenanceRecorder(make_session_factory(engine))

    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'tester', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )

    e1 = recorder.record(run_id, ProvenanceEventType.RUN_STARTED, "system", {"i": 1})
    e2 = recorder.record(run_id, ProvenanceEventType.STEP_STARTED, "system", {"i": 2})
    e3 = recorder.record(run_id, ProvenanceEventType.STEP_COMPLETED, "system", {"i": 3})

    assert e1.prev_event_hash is None
    assert e2.prev_event_hash == e1.event_hash
    assert e3.prev_event_hash == e2.event_hash

    # Validation passes on clean chain (Postgres preserves tzinfo, so this
    # also implicitly checks that the _canonical_timestamp normalization
    # gives identical hashes for aware datetimes).
    recorder.validate(run_id)
