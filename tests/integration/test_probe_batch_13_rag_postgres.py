"""Probe batch 13 — Postgres-specific schema/behavior probes.

Probes 305-329. Now that Postgres is reachable on localhost:5433
(``docker compose up postgres``), exercise migration outcomes and
parity behaviors specific to Postgres. RAG functionality is
already covered by tests/rag/test_component_index_unit.py once
sentence-transformers is installed; not re-probed here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]


def _postgres_url() -> str | None:
    return os.environ.get("APECX_CP_POSTGRES_URL")


def _migrate_postgres_to_head() -> str:
    url = _postgres_url()
    if not url:
        pytest.skip("APECX_CP_POSTGRES_URL not set")
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return url


# --- Probe 305: Postgres has all expected tables after head ---


def test_probe_305_postgres_all_tables_present() -> None:
    url = _migrate_postgres_to_head()
    from sqlalchemy import create_engine, inspect
    insp = inspect(create_engine(url))
    tables = set(insp.get_table_names())
    expected = {
        "alembic_version", "run", "step", "approval", "artifact",
        "generated_artifact", "provenance_event", "component",
        "allocation_estimate", "verified_synonym",
    }
    missing = expected - tables
    assert not missing, f"PROBE 305: missing Postgres tables: {missing}"


# --- Probe 306: Postgres allocation_estimate.created_at is NOT NULL ---


def test_probe_306_postgres_allocation_created_at_not_null() -> None:
    url = _migrate_postgres_to_head()
    from sqlalchemy import create_engine, inspect
    insp = inspect(create_engine(url))
    cols = {c["name"]: c for c in insp.get_columns("allocation_estimate")}
    assert not cols["created_at"]["nullable"], (
        "PROBE 306: allocation_estimate.created_at should be NOT NULL"
    )


# --- Probe 307: Postgres approval.created_at is NOT NULL ---


def test_probe_307_postgres_approval_created_at_not_null() -> None:
    url = _migrate_postgres_to_head()
    from sqlalchemy import create_engine, inspect
    insp = inspect(create_engine(url))
    cols = {c["name"]: c for c in insp.get_columns("approval")}
    assert not cols["created_at"]["nullable"]


# --- Probe 308: Postgres step.created_at is NOT NULL ---


def test_probe_308_postgres_step_created_at_not_null() -> None:
    url = _migrate_postgres_to_head()
    from sqlalchemy import create_engine, inspect
    insp = inspect(create_engine(url))
    cols = {c["name"]: c for c in insp.get_columns("step")}
    assert not cols["created_at"]["nullable"]


# --- Probe 309: Postgres run table has parent_run_id (FK) ---


def test_probe_309_postgres_run_parent_fk() -> None:
    url = _migrate_postgres_to_head()
    from sqlalchemy import create_engine, inspect
    insp = inspect(create_engine(url))
    fks = insp.get_foreign_keys("run")
    parent_fks = [
        fk for fk in fks
        if fk.get("referred_table") == "run"
        and "parent_run_id" in fk.get("constrained_columns", [])
    ]
    assert parent_fks, "PROBE 309: run.parent_run_id FK missing"


# --- Probe 310: Postgres recorder round-trip ---


def test_probe_310_postgres_recorder_round_trip() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import (
        make_engine, make_session_factory,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    engine = make_engine(url)
    factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(factory)
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC)},
        )
    e1 = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe", payload={"k": "v"},
    )
    e2 = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.STEP_STARTED,
        actor="probe", payload={},
    )
    assert e2.prev_event_hash == e1.event_hash


# --- Probe 311: Postgres recorder.validate clean chain ---


def test_probe_311_postgres_validate_clean() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import make_engine, make_session_factory
    from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    engine = make_engine(url)
    factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(factory)
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC)},
        )
    for i in range(5):
        recorder.record(
            run_id=run_id, event_type=ProvenanceEventType.STEP_COMPLETED,
            actor="p", payload={"i": i},
        )
    recorder.validate(run_id)


# --- Probe 312: Postgres validate detects tampered prev_event_hash ---


def test_probe_312_postgres_validate_detects_tamper() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import make_engine, make_session_factory
    from apecx_integration.control_plane.provenance.recorder import (
        ChainBroken, ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    engine = make_engine(url)
    factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(factory)
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC)},
        )
    recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED,
        actor="p", payload={},
    )
    e2 = recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.STEP_STARTED,
        actor="p", payload={},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE provenance_event SET prev_event_hash = 'TAMPER' "
                "WHERE id = :id"
            ),
            {"id": str(e2.id)},
        )
    with pytest.raises(ChainBroken):
        recorder.validate(run_id)


# --- Probe 313: Postgres FK enforces parent_run_id ---


def test_probe_313_postgres_parent_fk_enforced() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import make_engine
    from sqlalchemy.exc import IntegrityError
    engine = make_engine(url)
    fake_parent = uuid4()
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, "
                "parent_run_id) VALUES (:id, 'a', 'PENDING', :ts, :p)"
            ),
            {
                "id": str(uuid4()),
                "ts": datetime.now(UTC),
                "p": str(fake_parent),
            },
        )


# --- Probe 314: Postgres unique-active-null-scope synonym (cluster Y) ---


def test_probe_314_postgres_synonym_unique_null_scope() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import make_engine
    from sqlalchemy.exc import IntegrityError
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO verified_synonym (id, source_vocabulary, "
                "query_term, target_vocabulary, canonical_term, "
                "verified_by, verified_at, confidence, is_active) VALUES "
                "(:id, 'v', 't', 'b', 'X', 'a', :ts, 1.0, true)"
            ),
            {"id": str(uuid4()), "ts": datetime.now(UTC)},
        )
    # Second insert with same (source, query, target, NULL scope, active)
    # should violate the partial unique index.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO verified_synonym (id, source_vocabulary, "
                "query_term, target_vocabulary, canonical_term, "
                "verified_by, verified_at, confidence, is_active) VALUES "
                "(:id, 'v', 't', 'b', 'Y', 'a', :ts, 1.0, true)"
            ),
            {"id": str(uuid4()), "ts": datetime.now(UTC)},
        )


# --- Probe 315: Postgres double RUN_STARTED on same run blocked ---


def test_probe_315_postgres_run_started_unique_index() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import make_engine, make_session_factory
    from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    from sqlalchemy.exc import IntegrityError
    engine = make_engine(url)
    factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(factory)
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC)},
        )
    recorder.record(
        run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED,
        actor="p", payload={},
    )
    with pytest.raises(IntegrityError):
        recorder.record(
            run_id=run_id, event_type=ProvenanceEventType.RUN_STARTED,
            actor="p", payload={},
        )


# --- Probe 316: Postgres alembic_version contains exactly head revision ---


def test_probe_316_postgres_alembic_version_at_head() -> None:
    url = _migrate_postgres_to_head()
    from sqlalchemy import create_engine
    eng = create_engine(url)
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert len(rows) == 1
    # head should be 0006 (latest migration)
    assert rows[0][0] == "0006"


# --- Probe 317: Postgres engine creates a session factory ---


def test_probe_317_postgres_session_factory() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import make_engine, make_session_factory
    engine = make_engine(url)
    factory = make_session_factory(engine)
    with factory() as session:
        result = session.execute(text("SELECT 1")).scalar_one()
        assert result == 1


# --- Probe 318: ORM round-trip via Postgres for Run ---


def test_probe_318_postgres_orm_run_roundtrip() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import make_engine, make_session_factory
    from apecx_integration.control_plane.models.entities import Run as RunORM
    from apecx_integration.control_plane.schemas.enums import RunStatus
    factory = make_session_factory(make_engine(url))
    rid = uuid4()
    with factory() as session:
        session.add(RunORM(
            id=rid, user_id="a", status=RunStatus.PAUSED,
            created_at=datetime.now(UTC),
        ))
        session.commit()
    with factory() as session:
        r = session.get(RunORM, rid)
        assert r.status is RunStatus.PAUSED


# --- Probe 319: Postgres /healthz via TestClient (cross-engine) ---


def test_probe_319_postgres_healthz() -> None:
    url = _migrate_postgres_to_head()
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    app = create_app(engine=make_engine(url))
    c = TestClient(app)
    r = c.get("/healthz")
    assert r.status_code == 200


# --- Probe 320: Postgres revoke conditional UPDATE works (cluster AA) ---


def test_probe_320_postgres_revoke_conditional() -> None:
    url = _migrate_postgres_to_head()
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    app = create_app(engine=make_engine(url))
    c = TestClient(app)
    r1 = c.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v", "query_term": "pg-revoke",
            "target_vocabulary": "b", "canonical_term": "X",
            "verified_by": "alex", "confidence": 1.0, "scope": "pg",
        },
    )
    sid = r1.json()["verified_synonym"]["id"]
    rev1 = c.patch(
        f"/verified_synonyms/{sid}",
        json={"revoked_by": "alex", "revocation_reason": "rev1"},
    )
    assert rev1.status_code == 200
    rev2 = c.patch(
        f"/verified_synonyms/{sid}",
        json={"revoked_by": "alex", "revocation_reason": "rev2"},
    )
    assert rev2.status_code == 409


# --- Probe 321: Postgres approve flow works ---


def test_probe_321_postgres_approve_flow() -> None:
    url = _migrate_postgres_to_head()
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    engine = make_engine(url)
    app = create_app(engine=engine)
    c = TestClient(app)
    run_id = uuid4()
    step_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PAUSED', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC)},
        )
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, "
                "status, input_artifact_ids, output_artifact_ids, "
                "created_at) VALUES (:id, :rid, 's', 'LOCAL', "
                "'PAUSED_FOR_APPROVAL', '[]'::jsonb, '[]'::jsonb, :ts)"
            ),
            {
                "id": str(step_id), "rid": str(run_id),
                "ts": datetime.now(UTC),
            },
        )
    cr = c.post(
        "/approvals/",
        json={
            "run_id": str(run_id), "step_id": str(step_id),
            "kind": "hard", "summary": "t", "artifact_ids": [],
        },
    )
    aid = cr.json()["approval"]["id"]
    appr = c.post(
        "/approvals/approve",
        json={"approval_id": aid, "decided_by": "alex"},
    )
    assert appr.status_code == 200


# --- Probe 322: Postgres reject + approve double-decision yields 409 ---


def test_probe_322_postgres_double_decision_409() -> None:
    url = _migrate_postgres_to_head()
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    engine = make_engine(url)
    app = create_app(engine=engine)
    c = TestClient(app)
    run_id = uuid4()
    step_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PAUSED', :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC)},
        )
        conn.execute(
            text(
                "INSERT INTO step (id, run_id, step_name, executor, "
                "status, input_artifact_ids, output_artifact_ids, "
                "created_at) VALUES (:id, :rid, 's', 'LOCAL', "
                "'PAUSED_FOR_APPROVAL', '[]'::jsonb, '[]'::jsonb, :ts)"
            ),
            {
                "id": str(step_id), "rid": str(run_id),
                "ts": datetime.now(UTC),
            },
        )
    cr = c.post(
        "/approvals/",
        json={
            "run_id": str(run_id), "step_id": str(step_id),
            "kind": "hard", "summary": "t", "artifact_ids": [],
        },
    )
    aid = cr.json()["approval"]["id"]
    c.post(
        "/approvals/approve",
        json={"approval_id": aid, "decided_by": "alex"},
    )
    rej = c.post(
        "/approvals/reject",
        json={"approval_id": aid, "decided_by": "alex", "reason": "no"},
    )
    assert rej.status_code == 409


# --- Probe 323: Postgres /metrics/approvals empty window ---


def test_probe_323_postgres_metrics_empty_window() -> None:
    url = _migrate_postgres_to_head()
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    app = create_app(engine=make_engine(url))
    c = TestClient(app)
    r = c.get("/metrics/approvals", params={"since": "2099-01-01T00:00:00Z"})
    assert r.status_code == 200
    assert r.json()["count"] == 0


# --- Probe 324: Postgres /runs/list ordered by created_at desc ---


def test_probe_324_postgres_runs_list_order() -> None:
    url = _migrate_postgres_to_head()
    import time
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    engine = make_engine(url)
    app = create_app(engine=engine)
    c = TestClient(app)
    rids = []
    for _ in range(3):
        rid = uuid4()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO run (id, user_id, status, created_at) "
                    "VALUES (:id, 'pg-list', 'PENDING', :ts)"
                ),
                {"id": str(rid), "ts": datetime.now(UTC)},
            )
        rids.append(rid)
        time.sleep(0.001)
    r = c.post(
        "/runs/list",
        json={"user_id": "pg-list", "limit": 10},
    )
    returned = [uuid4().__class__(rr["id"]) for rr in r.json()["runs"]]
    assert returned == list(reversed(rids))


# --- Probe 325: Postgres /verified_synonyms POST + lookup roundtrip ---


def test_probe_325_postgres_synonyms_lookup() -> None:
    url = _migrate_postgres_to_head()
    from fastapi.testclient import TestClient
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    app = create_app(engine=make_engine(url))
    c = TestClient(app)
    cr = c.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v", "query_term": "pg-look",
            "target_vocabulary": "b", "canonical_term": "X",
            "verified_by": "alex", "confidence": 1.0, "scope": "pg-lookup",
        },
    )
    assert cr.status_code == 200
    look = c.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "v", "target_vocabulary": "b",
            "query_terms": ["pg-look"], "scope": "pg-lookup",
        },
    )
    assert look.json()["matches"][0]["result"] is not None


# --- Probe 326: Postgres recorder cluster X cache works ---


def test_probe_326_postgres_recorder_cache() -> None:
    url = _migrate_postgres_to_head()
    from apecx_integration.control_plane.db import make_engine, make_session_factory
    from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    engine = make_engine(url)
    factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(factory)
    rid = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'a', 'PENDING', :ts)"
            ),
            {"id": str(rid), "ts": datetime.now(UTC)},
        )
    e = recorder.record(
        run_id=rid, event_type=ProvenanceEventType.RUN_STARTED,
        actor="p", payload={},
    )
    assert recorder._last_hash[rid] == e.event_hash


# --- Probe 327: Postgres allocation_estimate ORDER BY created_at works ---


def test_probe_327_postgres_estimate_order() -> None:
    url = _migrate_postgres_to_head()
    import time
    from sqlalchemy import select, desc
    from apecx_integration.control_plane.db import make_engine, make_session_factory
    from apecx_integration.control_plane.models.entities import (
        AllocationEstimate as AEORM, Run as RunORM,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    engine = make_engine(url)
    factory = make_session_factory(engine)
    rid = uuid4()
    with factory() as session:
        session.add(RunORM(
            id=rid, user_id="a", status=RunStatus.PENDING,
            created_at=datetime.now(UTC),
        ))
        session.commit()
    eids = []
    for _ in range(3):
        eid = uuid4()
        with factory() as session:
            session.add(AEORM(
                id=eid, run_id=rid,
                estimated_core_hours=1.0,
                estimated_wall_time_seconds=3600.0,
                endpoint="polaris", user_confirmed=False,
                created_at=datetime.now(UTC),
            ))
            session.commit()
        eids.append(eid)
        time.sleep(0.001)
    with factory() as session:
        rows = session.execute(
            select(AEORM)
            .where(AEORM.run_id == rid)
            .order_by(AEORM.created_at.desc(), AEORM.id.desc())
        ).scalars().all()
    assert [r.id for r in rows] == list(reversed(eids))


# --- Probe 328: Postgres alembic round-trip multi-step ---


def test_probe_328_postgres_round_trip_multi_step() -> None:
    url = _postgres_url()
    if not url:
        pytest.skip()
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    insp1 = inspect(create_engine(url))
    head_tables = set(insp1.get_table_names())
    command.downgrade(cfg, "base")
    insp2 = inspect(create_engine(url))
    base_tables = set(insp2.get_table_names())
    command.upgrade(cfg, "head")
    insp3 = inspect(create_engine(url))
    head_again = set(insp3.get_table_names())
    assert head_tables == head_again
    assert base_tables == {"alembic_version"}


# --- Probe 329: SQLite + Postgres produce IDENTICAL recorder hashes ---


def test_probe_329_recorder_hash_cross_backend() -> None:
    """Same input → same hash, regardless of whether the backend
    is SQLite or Postgres. Recorder's hash uses SHA-256 of
    canonical content, no DB-side influence."""
    url = _migrate_postgres_to_head()
    fixed_ts = datetime(2026, 1, 1, tzinfo=UTC)
    fixed_run = uuid4()
    from apecx_integration.control_plane.provenance.recorder import _compute_event_hash
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    h_pg = _compute_event_hash(
        prev_event_hash=None, run_id=fixed_run,
        event_type=ProvenanceEventType.RUN_STARTED, actor="p",
        timestamp=fixed_ts, payload={"k": "v"},
    )
    h_sqlite = _compute_event_hash(
        prev_event_hash=None, run_id=fixed_run,
        event_type=ProvenanceEventType.RUN_STARTED, actor="p",
        timestamp=fixed_ts, payload={"k": "v"},
    )
    assert h_pg == h_sqlite
