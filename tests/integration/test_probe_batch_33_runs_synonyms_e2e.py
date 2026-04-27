"""Probe batch 33 — /runs/* + /verified_synonyms/* end-to-end
(probes 880-904). Closes the streak at 300/300 post-AQ.

Final batch in the campaign. Pins the remaining HTTP layer:

  - /runs/list, /runs/status, /runs/artifact (cluster AH FIFO
    ordering, pending_approval picker)
  - /verified_synonyms/lookup (cache-hot-path, scope semantics)
  - /verified_synonyms/ POST (uniqueness, race-safe 409, post-
    revoke-create)

Cluster AE (FIFO) and AH (step-ordering) regression coverage at
the wire level.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(tmp_path):
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    db = tmp_path / "rs.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db}")
    return create_app(engine=eng), eng


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    a, _ = app
    with TestClient(a) as c:
        yield c


def _insert_run(eng, *, user_id="u", status=None, created_at=None) -> uuid.UUID:
    from apecx_integration.control_plane.models.entities import Run
    from apecx_integration.control_plane.schemas.enums import RunStatus
    from apecx_integration.control_plane.db import make_session_factory
    sf = make_session_factory(eng)
    rid = uuid.uuid4()
    with sf() as session:
        session.add(Run(
            id=rid, user_id=user_id,
            status=status or RunStatus.PENDING,
            created_at=created_at or datetime.now(UTC),
        ))
        session.commit()
    return rid


# ---------------------------------------------------------------------------
# /runs/list — probes 880-883
# ---------------------------------------------------------------------------


def test_probe_880_list_returns_user_runs(client, app) -> None:
    _, eng = app
    rid = _insert_run(eng, user_id="alice")
    r = client.post("/runs/list", json={"user_id": "alice", "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert any(run["id"] == str(rid) for run in body["runs"])


def test_probe_881_list_filters_by_user(client, app) -> None:
    _, eng = app
    a_rid = _insert_run(eng, user_id="alice")
    b_rid = _insert_run(eng, user_id="bob")
    r = client.post("/runs/list", json={"user_id": "alice", "limit": 10})
    body = r.json()
    ids = [run["id"] for run in body["runs"]]
    assert str(a_rid) in ids
    assert str(b_rid) not in ids


def test_probe_882_list_empty_for_unknown_user(client) -> None:
    r = client.post("/runs/list", json={"user_id": "ghost", "limit": 10})
    assert r.status_code == 200
    assert r.json()["runs"] == []


def test_probe_883_list_orders_by_created_at_desc(client, app) -> None:
    """Newest first — operators expect the most recent run on top."""
    _, eng = app
    base = datetime.now(UTC)
    older = _insert_run(eng, user_id="u", created_at=base - timedelta(hours=1))
    newer = _insert_run(eng, user_id="u", created_at=base)
    r = client.post("/runs/list", json={"user_id": "u", "limit": 10})
    ids = [run["id"] for run in r.json()["runs"]]
    assert ids[0] == str(newer)
    assert ids[1] == str(older)


# ---------------------------------------------------------------------------
# /runs/status — probes 884-892
# ---------------------------------------------------------------------------


def test_probe_884_status_unknown_run_404(client) -> None:
    r = client.post("/runs/status", json={"run_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_probe_885_status_returns_run_and_steps(client, app) -> None:
    """Happy path: status returns run + sorted steps + None
    pending_approval."""
    from apecx_integration.control_plane.models.entities import Step
    from apecx_integration.control_plane.schemas.enums import (
        ExecutorKind, RunStatus, StepStatus,
    )
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid = _insert_run(eng, status=RunStatus.RUNNING)
    sf = make_session_factory(eng)
    with sf() as session:
        session.add(Step(
            id=uuid.uuid4(), run_id=rid,
            step_name="step_one",
            executor=ExecutorKind.LOCAL,
            status=StepStatus.RUNNING,
            created_at=datetime.now(UTC),
        ))
        session.commit()
    r = client.post("/runs/status", json={"run_id": str(rid)})
    assert r.status_code == 200
    body = r.json()
    assert body["run"]["id"] == str(rid)
    assert len(body["steps"]) == 1
    assert body["pending_approval"] is None


def test_probe_886_status_empty_steps(client, app) -> None:
    """A run with no Step rows must return steps=[], not 500."""
    _, eng = app
    rid = _insert_run(eng)
    r = client.post("/runs/status", json={"run_id": str(rid)})
    assert r.status_code == 200
    assert r.json()["steps"] == []


def test_probe_887_status_step_fifo_ordering(client, app) -> None:
    """Cluster AH — PENDING steps order by created_at, NOT by uuid
    lex order. Probe inserts 3 PENDING steps in known order."""
    from apecx_integration.control_plane.models.entities import Step
    from apecx_integration.control_plane.schemas.enums import (
        ExecutorKind, StepStatus,
    )
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid = _insert_run(eng)
    sf = make_session_factory(eng)
    base = datetime.now(UTC)
    sids = []
    with sf() as session:
        for i in range(3):
            sid = uuid.uuid4()
            sids.append(sid)
            session.add(Step(
                id=sid, run_id=rid,
                step_name=f"s{i}",
                executor=ExecutorKind.LOCAL,
                status=StepStatus.PENDING,
                created_at=base + timedelta(seconds=i),
            ))
        session.commit()
    r = client.post("/runs/status", json={"run_id": str(rid)})
    returned_ids = [s["id"] for s in r.json()["steps"]]
    expected = [str(s) for s in sids]
    assert returned_ids == expected, (
        f"PROBE 887: PENDING step FIFO broken — expected {expected}, "
        f"got {returned_ids}"
    )


def test_probe_888_status_pending_approval_picker(client, app) -> None:
    """When multiple PENDING approvals exist on a run, the
    OLDEST is surfaced (cluster AE-style picker for single-result)."""
    from apecx_integration.control_plane.models.entities import (
        Approval, Step,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ApprovalKind, ApprovalStatus, ExecutorKind, StepStatus,
    )
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid = _insert_run(eng)
    sf = make_session_factory(eng)
    base = datetime.now(UTC)
    older_approval_id = uuid.uuid4()
    newer_approval_id = uuid.uuid4()
    with sf() as session:
        for ai, when in (
            (older_approval_id, base),
            (newer_approval_id, base + timedelta(seconds=1)),
        ):
            sid = uuid.uuid4()
            session.add(Step(
                id=sid, run_id=rid,
                step_name="s",
                executor=ExecutorKind.LOCAL,
                status=StepStatus.PAUSED_FOR_APPROVAL,
                created_at=base,
            ))
            session.flush()
            session.add(Approval(
                id=ai, step_id=sid,
                kind=ApprovalKind.HARD,
                status=ApprovalStatus.PENDING,
                created_at=when,
            ))
        session.commit()
    r = client.post("/runs/status", json={"run_id": str(rid)})
    pending = r.json()["pending_approval"]
    assert pending is not None
    assert pending["id"] == str(older_approval_id), (
        "PROBE 888: pending_approval picker did not return oldest"
    )


def test_probe_889_status_with_no_pending_approval(client, app) -> None:
    """When all approvals are decided, pending_approval=None."""
    _, eng = app
    rid = _insert_run(eng)
    r = client.post("/runs/status", json={"run_id": str(rid)})
    assert r.json()["pending_approval"] is None


def test_probe_890_status_invalid_run_id_422(client) -> None:
    r = client.post("/runs/status", json={"run_id": "not-a-uuid"})
    assert r.status_code == 422


def test_probe_891_artifact_unknown_id_404(client) -> None:
    r = client.post("/runs/artifact", json={"artifact_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_probe_892_artifact_returns_metadata(client, app) -> None:
    """Happy path: get_artifact returns the row metadata."""
    from apecx_integration.control_plane.models.entities import Artifact, Run
    from apecx_integration.control_plane.schemas.enums import (
        ArtifactKind, RunStatus,
    )
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid = uuid.uuid4()
    aid = uuid.uuid4()
    sf = make_session_factory(eng)
    with sf() as session:
        session.add(Run(
            id=rid, user_id="u",
            status=RunStatus.PENDING,
            created_at=datetime.now(UTC),
        ))
        session.flush()
        session.add(Artifact(
            id=aid, run_id=rid,
            kind=ArtifactKind.OUTPUT,
            location="/tmp/somewhere",
            content_hash="0" * 64,
            size_bytes=100,
            mime_type="application/octet-stream",
            created_at=datetime.now(UTC),
        ))
        session.commit()
    r = client.post("/runs/artifact", json={"artifact_id": str(aid)})
    assert r.status_code == 200
    body = r.json()
    assert body["artifact"]["id"] == str(aid)
    assert body["artifact"]["content_hash"] == "0" * 64
    # T11 not landed → inline_bytes None with reason
    assert body["inline_bytes"] is None
    assert body["reason_inline_omitted"]


# ---------------------------------------------------------------------------
# /verified_synonyms/lookup — probes 893-897
# ---------------------------------------------------------------------------


def _create_synonym(client, **kwargs) -> uuid.UUID:
    """POST /verified_synonyms/ to create a row."""
    payload = {
        "source_vocabulary": "user_query",
        "query_term": "EEEV",
        "target_vocabulary": "violin.pathogen_id",
        "canonical_term": "VO_0000001",
        "verified_by": "alice",
        "confidence": 0.95,
        **kwargs,
    }
    r = client.post("/verified_synonyms/", json=payload)
    assert r.status_code == 200, r.text
    return uuid.UUID(r.json()["verified_synonym"]["id"])


def test_probe_893_lookup_returns_active_mapping(client) -> None:
    """A previously-created synonym must show up in lookup
    results."""
    _create_synonym(client, query_term="EEEV", canonical_term="VO_0000001")
    r = client.post("/verified_synonyms/lookup", json={
        "source_vocabulary": "user_query",
        "target_vocabulary": "violin.pathogen_id",
        "query_terms": ["EEEV"],
    })
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert len(matches) == 1
    assert matches[0]["query_term"] == "EEEV"
    assert matches[0]["result"]["canonical_term"] == "VO_0000001"


def test_probe_894_lookup_novel_term_null_result(client) -> None:
    """Terms with no active mapping must return result=null —
    the cache-miss signal that drives Step 3c (LLM proposals)."""
    r = client.post("/verified_synonyms/lookup", json={
        "source_vocabulary": "user_query",
        "target_vocabulary": "violin.pathogen_id",
        "query_terms": ["UNKNOWN_TERM_XYZ"],
    })
    assert r.status_code == 200
    matches = r.json()["matches"]
    assert len(matches) == 1
    assert matches[0]["result"] is None


def test_probe_895_lookup_empty_query_terms_422(client) -> None:
    """Pydantic min_length=1 enforced — empty list rejects."""
    r = client.post("/verified_synonyms/lookup", json={
        "source_vocabulary": "user_query",
        "target_vocabulary": "violin.pathogen_id",
        "query_terms": [],
    })
    assert r.status_code == 422


def test_probe_896_lookup_too_many_query_terms_422(client) -> None:
    """max_length=500 — a list of 501 terms rejects at the
    schema layer."""
    r = client.post("/verified_synonyms/lookup", json={
        "source_vocabulary": "user_query",
        "target_vocabulary": "violin.pathogen_id",
        "query_terms": [f"term_{i}" for i in range(501)],
    })
    assert r.status_code == 422


def test_probe_897_lookup_respects_scope_filter(client) -> None:
    """A scope-NULL mapping must NOT match a scope-non-NULL lookup
    (and vice versa). Scope is the per-corpus narrowing."""
    # Create a global (scope=None) mapping
    _create_synonym(
        client, query_term="GAMMA",
        canonical_term="GLOBAL_MAPPING",
        scope=None,
    )
    # Create a scoped mapping for the same term
    _create_synonym(
        client, query_term="GAMMA",
        canonical_term="ALPHAVIRIDAE_MAPPING",
        scope="alphaviridae",
    )
    # Lookup with no scope should match the global one
    r = client.post("/verified_synonyms/lookup", json={
        "source_vocabulary": "user_query",
        "target_vocabulary": "violin.pathogen_id",
        "query_terms": ["GAMMA"],
    })
    assert r.json()["matches"][0]["result"]["canonical_term"] == "GLOBAL_MAPPING"
    # Lookup with scope should match the scoped one
    r = client.post("/verified_synonyms/lookup", json={
        "source_vocabulary": "user_query",
        "target_vocabulary": "violin.pathogen_id",
        "query_terms": ["GAMMA"],
        "scope": "alphaviridae",
    })
    assert r.json()["matches"][0]["result"]["canonical_term"] == "ALPHAVIRIDAE_MAPPING"


# ---------------------------------------------------------------------------
# /verified_synonyms/ POST + cross-route — probes 898-904
# ---------------------------------------------------------------------------


def test_probe_898_create_happy_path(client) -> None:
    """Create returns the new synonym with is_active=True."""
    sid = _create_synonym(client, query_term="EEEV")
    # Re-read via lookup
    r = client.post("/verified_synonyms/lookup", json={
        "source_vocabulary": "user_query",
        "target_vocabulary": "violin.pathogen_id",
        "query_terms": ["EEEV"],
    })
    assert r.json()["matches"][0]["result"]["id"] == str(sid)
    assert r.json()["matches"][0]["result"]["is_active"] is True


def test_probe_899_create_duplicate_active_returns_409(client) -> None:
    """Migration 0003 unique-active-when-scope-NULL constraint —
    a second create for the same (source, query, target, scope=NULL,
    is_active=True) tuple → 409."""
    _create_synonym(client, query_term="DUPE", canonical_term="A")
    r = client.post("/verified_synonyms/", json={
        "source_vocabulary": "user_query",
        "query_term": "DUPE",
        "target_vocabulary": "violin.pathogen_id",
        "canonical_term": "B",  # different canonical, same tuple
        "verified_by": "bob",
        "confidence": 1.0,
    })
    assert r.status_code == 409


def test_probe_900_create_after_revoke_succeeds(client) -> None:
    """Once an active mapping is revoked (is_active=False), a new
    active mapping for the same tuple must be allowed — that's
    the "correct a previous mistake" workflow."""
    sid = _create_synonym(client, query_term="REPLACE_ME", canonical_term="OLD")
    # Revoke
    rev = client.patch(f"/verified_synonyms/{sid}", json={
        "revoked_by": "alice",
        "revocation_reason": "wrong canonical",
    })
    assert rev.status_code == 200
    # Now a fresh active mapping for the same tuple should work
    new_sid = _create_synonym(client, query_term="REPLACE_ME", canonical_term="NEW")
    assert new_sid != sid


def test_probe_901_create_confidence_out_of_range_422(client) -> None:
    """confidence is Field(ge=0.0, le=1.0) — values outside that
    range reject."""
    for bad in (-0.1, 1.5, 2.0):
        r = client.post("/verified_synonyms/", json={
            "source_vocabulary": "user_query",
            "query_term": "x",
            "target_vocabulary": "violin.pathogen_id",
            "canonical_term": "y",
            "verified_by": "alice",
            "confidence": bad,
        })
        assert r.status_code == 422, f"PROBE 901: confidence={bad} unexpectedly accepted"


def test_probe_902_create_empty_query_term_422(client) -> None:
    """query_term has Field(min_length=1) — empty rejects."""
    r = client.post("/verified_synonyms/", json={
        "source_vocabulary": "user_query",
        "query_term": "",
        "target_vocabulary": "violin.pathogen_id",
        "canonical_term": "y",
        "verified_by": "alice",
        "confidence": 1.0,
    })
    assert r.status_code == 422


def test_probe_903_create_persists_metadata(client, app) -> None:
    """Created row carries verified_by + verified_at + confidence."""
    from apecx_integration.control_plane.models.entities import VerifiedSynonym
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    sid = _create_synonym(
        client, query_term="META_TEST",
        verified_by="dr.smith@example", confidence=0.87,
    )
    sf = make_session_factory(eng)
    with sf() as session:
        row = session.get(VerifiedSynonym, sid)
        assert row.verified_by == "dr.smith@example"
        assert row.verified_at is not None
        assert row.confidence == 0.87
        assert row.is_active is True


def test_probe_904_cross_route_create_then_lookup_round_trip(client) -> None:
    """End-to-end smoke that closes the streak: create three
    synonyms, then a single batched lookup for all three returns
    them in the correct order with correct canonicals — the hot
    path for the workflow's Step 3a cache lookup."""
    expected = [
        ("EEEV", "VO_EASTERN_EQUINE"),
        ("VEEV", "VO_VENEZUELAN"),
        ("WEEV", "VO_WESTERN_EQUINE"),
    ]
    for term, canonical in expected:
        _create_synonym(client, query_term=term, canonical_term=canonical)
    r = client.post("/verified_synonyms/lookup", json={
        "source_vocabulary": "user_query",
        "target_vocabulary": "violin.pathogen_id",
        "query_terms": [term for term, _ in expected],
    })
    assert r.status_code == 200
    matches = r.json()["matches"]
    # Order must match input order; result must match created canonical
    for (term, canonical), match in zip(expected, matches):
        assert match["query_term"] == term
        assert match["result"]["canonical_term"] == canonical
