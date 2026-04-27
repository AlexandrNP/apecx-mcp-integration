"""Probe batch 31 — approval routes happy + edge paths end-to-end
(probes 805-829).

The /approvals/* routes are the human-in-the-loop boundary. Every
HTTP path here either flips an approval row's status or queries
its state. A bug on the approve/reject/correct happy path means
scientists can't actually decide approvals; a bug on the edge
paths (already-decided, missing approval, mismatched run/step)
means the audit trail loses meaning.

This batch exercises every /approvals/* route end-to-end via
TestClient against a real migrated SQLite DB. Cluster V1 (atomic
conditional UPDATE) and AE (FIFO ordering) regressions get
explicit pinning probes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
    db = tmp_path / "appr.db"
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


def _seed_run_and_step(eng) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Run + Step and return (run_id, step_id) for use
    by /approvals/ routes."""
    from apecx_integration.control_plane.models.entities import Run, Step
    from apecx_integration.control_plane.schemas.enums import (
        ExecutorKind, RunStatus, StepStatus,
    )
    from apecx_integration.control_plane.db import make_session_factory
    sf = make_session_factory(eng)
    rid = uuid.uuid4()
    sid = uuid.uuid4()
    with sf() as session:
        session.add(Run(
            id=rid, user_id="u",
            status=RunStatus.RUNNING,
            created_at=datetime.now(UTC),
        ))
        session.flush()
        session.add(Step(
            id=sid, run_id=rid,
            step_name="approval_gate",
            executor=ExecutorKind.LOCAL,
            status=StepStatus.PAUSED_FOR_APPROVAL,
            created_at=datetime.now(UTC),
        ))
        session.commit()
    return rid, sid


def _create_approval(client, run_id, step_id, kind="hard") -> uuid.UUID:
    """POST /approvals/ to create a pending approval, return its id."""
    r = client.post("/approvals/", json={
        "run_id": str(run_id),
        "step_id": str(step_id),
        "kind": kind,
        "summary": "Test approval gate",
        "artifact_ids": [],
        "policy": {},
    })
    assert r.status_code == 200, r.text
    return uuid.UUID(r.json()["approval"]["id"])


# ---------------------------------------------------------------------------
# /approvals/ create — probes 805-810
# ---------------------------------------------------------------------------


def test_probe_805_create_approval_returns_approval(client, app) -> None:
    """POST /approvals/ creates a row and returns it with status=pending."""
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    r = client.post("/approvals/", json={
        "run_id": str(rid),
        "step_id": str(sid),
        "kind": "hard",
        "summary": "Need reviewer eyes",
        "artifact_ids": [],
        "policy": {},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["approval"]["status"] == "pending"
    assert body["approval"]["step_id"] == str(sid)


def test_probe_806_create_approval_persists_summary_in_policy(client, app) -> None:
    """The summary string is stored on the policy JSON blob — probe
    locks the field name so a future rename doesn't lose the
    reviewer-facing text silently."""
    from apecx_integration.control_plane.models.entities import Approval
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    sf = make_session_factory(eng)
    with sf() as session:
        appr = session.get(Approval, aid)
        assert appr.policy.get("summary") == "Test approval gate"


def test_probe_807_create_approval_emits_approval_requested(client, app) -> None:
    from apecx_integration.control_plane.models.entities import ProvenanceEvent
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    from apecx_integration.control_plane.db import make_session_factory
    from sqlalchemy import select
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    sf = make_session_factory(eng)
    with sf() as session:
        events = session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.run_id == rid,
                ProvenanceEvent.event_type == ProvenanceEventType.APPROVAL_REQUESTED,
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload.get("approval_id") == str(aid)


def test_probe_808_create_approval_run_step_mismatch_400(client, app) -> None:
    """If the run_id in the request doesn't match the step's
    actual run_id, fail-fast with 400. Silently accepting would
    let an attacker cross-link approvals to steps in unrelated
    runs."""
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    bogus_rid = uuid.uuid4()
    r = client.post("/approvals/", json={
        "run_id": str(bogus_rid),  # wrong run id
        "step_id": str(sid),
        "kind": "hard",
        "summary": "x",
        "artifact_ids": [],
        "policy": {},
    })
    assert r.status_code == 400
    assert "step" in r.json()["detail"]


def test_probe_809_create_approval_unknown_step_404(client, app) -> None:
    """An approval for a step that doesn't exist must 404, not 500.
    Pre-condition: only after a Step row exists can an Approval be
    created for it."""
    _, eng = app
    rid = uuid.uuid4()
    sid = uuid.uuid4()  # never inserted
    r = client.post("/approvals/", json={
        "run_id": str(rid),
        "step_id": str(sid),
        "kind": "hard",
        "summary": "x",
        "artifact_ids": [],
        "policy": {},
    })
    assert r.status_code in (404, 400)


def test_probe_810_create_approval_invalid_kind_422(client, app) -> None:
    """ApprovalKind enum is locked to {hard, soft, silent,
    allocation}. An invalid value must reject at the schema layer."""
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    r = client.post("/approvals/", json={
        "run_id": str(rid),
        "step_id": str(sid),
        "kind": "totally_made_up",
        "summary": "x",
        "artifact_ids": [],
        "policy": {},
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /approvals/approve — probes 811-815
# ---------------------------------------------------------------------------


def test_probe_811_approve_pending_returns_approved(client, app) -> None:
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    r = client.post("/approvals/approve", json={
        "approval_id": str(aid),
        "decided_by": "alice",
        "comment": "LGTM",
    })
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "approved"
    assert r.json()["approval"]["decided_by"] == "alice"


def test_probe_812_double_approve_returns_409(client, app) -> None:
    """Cluster V1 — the conditional UPDATE WHERE status=PENDING
    means the second approve loses the race. Surface 409 to the
    loser, NOT a silent overwrite."""
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    r1 = client.post("/approvals/approve", json={
        "approval_id": str(aid), "decided_by": "alice",
    })
    assert r1.status_code == 200
    r2 = client.post("/approvals/approve", json={
        "approval_id": str(aid), "decided_by": "bob",
    })
    assert r2.status_code == 409


def test_probe_813_approve_unknown_id_returns_404(client, app) -> None:
    r = client.post("/approvals/approve", json={
        "approval_id": str(uuid.uuid4()),
        "decided_by": "alice",
    })
    assert r.status_code == 404


def test_probe_814_approve_emits_approval_decided(client, app) -> None:
    from apecx_integration.control_plane.models.entities import ProvenanceEvent
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    from apecx_integration.control_plane.db import make_session_factory
    from sqlalchemy import select
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    client.post("/approvals/approve", json={
        "approval_id": str(aid), "decided_by": "alice",
    })
    sf = make_session_factory(eng)
    with sf() as session:
        events = session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.run_id == rid,
                ProvenanceEvent.event_type == ProvenanceEventType.APPROVAL_DECIDED,
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["status"] == "approved"
    assert events[0].payload["approval_id"] == str(aid)


def test_probe_815_approve_persists_decided_at(client, app) -> None:
    """decided_at must be populated after approve — it's the
    metric input for the rubber-stamping detector."""
    from apecx_integration.control_plane.models.entities import Approval
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    client.post("/approvals/approve", json={
        "approval_id": str(aid), "decided_by": "alice",
    })
    sf = make_session_factory(eng)
    with sf() as session:
        appr = session.get(Approval, aid)
        assert appr.decided_at is not None


# ---------------------------------------------------------------------------
# /approvals/reject — probes 816-819
# ---------------------------------------------------------------------------


def test_probe_816_reject_with_reason_persists(client, app) -> None:
    """Cluster AO — reject requires a non-empty reason. Successful
    reject persists status + comment from reason."""
    from apecx_integration.control_plane.models.entities import Approval
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    r = client.post("/approvals/reject", json={
        "approval_id": str(aid),
        "decided_by": "alice",
        "reason": "synonym mappings look wrong",
    })
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "rejected"
    sf = make_session_factory(eng)
    with sf() as session:
        appr = session.get(Approval, aid)
        # The route stores the reason as the comment field
        assert appr.comment == "synonym mappings look wrong"


def test_probe_817_reject_empty_reason_422(client, app) -> None:
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    r = client.post("/approvals/reject", json={
        "approval_id": str(aid),
        "decided_by": "alice",
        "reason": "",  # min_length=1
    })
    assert r.status_code == 422


def test_probe_818_double_reject_returns_409(client, app) -> None:
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    r1 = client.post("/approvals/reject", json={
        "approval_id": str(aid), "decided_by": "alice", "reason": "first",
    })
    assert r1.status_code == 200
    r2 = client.post("/approvals/reject", json={
        "approval_id": str(aid), "decided_by": "bob", "reason": "second",
    })
    assert r2.status_code == 409


def test_probe_819_approve_then_reject_returns_409(client, app) -> None:
    """Cross-decision race: a run that's already approved cannot
    be rejected after the fact. The conditional UPDATE WHERE
    status=PENDING enforces this."""
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    client.post("/approvals/approve", json={
        "approval_id": str(aid), "decided_by": "alice",
    })
    r = client.post("/approvals/reject", json={
        "approval_id": str(aid), "decided_by": "bob", "reason": "wait!",
    })
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# /approvals/correct — probes 820-822
# ---------------------------------------------------------------------------


def test_probe_820_correct_persists_modifications(client, app) -> None:
    """Cluster AP — correct must persist modifications dict on the
    policy field (or wherever it actually lives)."""
    from apecx_integration.control_plane.models.entities import Approval
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    r = client.post("/approvals/correct", json={
        "approval_id": str(aid),
        "decided_by": "alice",
        "modifications": {"replaced_synonyms": ["EEEV", "VEEV"]},
    })
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "approved_with_modifications"
    sf = make_session_factory(eng)
    with sf() as session:
        appr = session.get(Approval, aid)
        # The route stores modifications under policy.modifications
        # (extra_policy in _decide)
        assert appr.policy.get("modifications") == {
            "replaced_synonyms": ["EEEV", "VEEV"]
        }


def test_probe_821_correct_emits_approval_decided(client, app) -> None:
    from apecx_integration.control_plane.models.entities import ProvenanceEvent
    from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
    from apecx_integration.control_plane.db import make_session_factory
    from sqlalchemy import select
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    client.post("/approvals/correct", json={
        "approval_id": str(aid), "decided_by": "alice",
        "modifications": {"k": "v"},
    })
    sf = make_session_factory(eng)
    with sf() as session:
        ev = session.execute(
            select(ProvenanceEvent).where(
                ProvenanceEvent.run_id == rid,
                ProvenanceEvent.event_type == ProvenanceEventType.APPROVAL_DECIDED,
            )
        ).scalar_one()
    assert ev.payload["status"] == "approved_with_modifications"


def test_probe_822_correct_double_returns_409(client, app) -> None:
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    r1 = client.post("/approvals/correct", json={
        "approval_id": str(aid), "decided_by": "alice",
        "modifications": {"a": 1},
    })
    assert r1.status_code == 200
    r2 = client.post("/approvals/correct", json={
        "approval_id": str(aid), "decided_by": "bob",
        "modifications": {"a": 2},
    })
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# /approvals/pending FIFO ordering (cluster AE) — probes 823-826
# ---------------------------------------------------------------------------


def test_probe_823_pending_returns_only_pending(client, app) -> None:
    """The pending list must only include approvals with
    status=PENDING. Already-decided rows must NOT leak."""
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    a1 = _create_approval(client, rid, sid)
    # Approve a1; it must NOT show up in pending
    client.post("/approvals/approve", json={
        "approval_id": str(a1), "decided_by": "alice",
    })
    # Insert a second step for a fresh approval
    from apecx_integration.control_plane.models.entities import Step
    from apecx_integration.control_plane.schemas.enums import (
        ExecutorKind, StepStatus,
    )
    from apecx_integration.control_plane.db import make_session_factory
    sf = make_session_factory(eng)
    s2_id = uuid.uuid4()
    with sf() as session:
        session.add(Step(
            id=s2_id, run_id=rid,
            step_name="step2",
            executor=ExecutorKind.LOCAL,
            status=StepStatus.PAUSED_FOR_APPROVAL,
            created_at=datetime.now(UTC),
        ))
        session.commit()
    a2 = _create_approval(client, rid, s2_id)
    r = client.post("/approvals/pending", json={"user_id": "u"})
    assert r.status_code == 200
    pending_ids = {a["id"] for a in r.json()["approvals"]}
    assert str(a1) not in pending_ids
    assert str(a2) in pending_ids


def test_probe_824_pending_orders_by_created_at(client, app) -> None:
    """Cluster AE — created_at is the ORDER BY column. Insert
    approvals in known order and verify response respects it."""
    from apecx_integration.control_plane.models.entities import Step
    from apecx_integration.control_plane.schemas.enums import (
        ExecutorKind, StepStatus,
    )
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    rid, _sid = _seed_run_and_step(eng)
    sf = make_session_factory(eng)
    # Insert 3 steps + 3 approvals
    aids = []
    for i in range(3):
        s_id = uuid.uuid4()
        with sf() as session:
            session.add(Step(
                id=s_id, run_id=rid,
                step_name=f"s{i}",
                executor=ExecutorKind.LOCAL,
                status=StepStatus.PAUSED_FOR_APPROVAL,
                created_at=datetime.now(UTC),
            ))
            session.commit()
        aids.append(_create_approval(client, rid, s_id))
    r = client.post("/approvals/pending", json={"user_id": "u"})
    pending = r.json()["approvals"]
    pending_ids_in_order = [a["id"] for a in pending]
    # First-created should appear earlier than later-created
    # (FIFO is the operator's mental model — cluster AE pre-fix
    # used random uuid4 ordering, scrambling the backlog)
    expected_order = [str(a) for a in aids]
    assert pending_ids_in_order == expected_order, (
        f"PROBE 824: pending FIFO order broken — expected "
        f"{expected_order}, got {pending_ids_in_order}"
    )


def test_probe_825_pending_filters_by_user(client, app) -> None:
    """The pending list takes a user_id; only approvals belonging
    to runs owned by that user must appear."""
    from apecx_integration.control_plane.models.entities import Run, Step
    from apecx_integration.control_plane.schemas.enums import (
        ExecutorKind, RunStatus, StepStatus,
    )
    from apecx_integration.control_plane.db import make_session_factory
    _, eng = app
    sf = make_session_factory(eng)
    # Two runs by different users
    rid_alice, sid_alice = uuid.uuid4(), uuid.uuid4()
    rid_bob, sid_bob = uuid.uuid4(), uuid.uuid4()
    with sf() as session:
        session.add(Run(id=rid_alice, user_id="alice", status=RunStatus.RUNNING,
                        created_at=datetime.now(UTC)))
        session.add(Run(id=rid_bob, user_id="bob", status=RunStatus.RUNNING,
                        created_at=datetime.now(UTC)))
        session.flush()
        session.add(Step(id=sid_alice, run_id=rid_alice,
                         step_name="s", executor=ExecutorKind.LOCAL,
                         status=StepStatus.PAUSED_FOR_APPROVAL,
                         created_at=datetime.now(UTC)))
        session.add(Step(id=sid_bob, run_id=rid_bob,
                         step_name="s", executor=ExecutorKind.LOCAL,
                         status=StepStatus.PAUSED_FOR_APPROVAL,
                         created_at=datetime.now(UTC)))
        session.commit()
    a_alice = _create_approval(client, rid_alice, sid_alice)
    a_bob = _create_approval(client, rid_bob, sid_bob)
    r = client.post("/approvals/pending", json={"user_id": "alice"})
    pending_ids = {a["id"] for a in r.json()["approvals"]}
    assert str(a_alice) in pending_ids
    assert str(a_bob) not in pending_ids


def test_probe_826_pending_empty_for_unknown_user(client) -> None:
    r = client.post("/approvals/pending", json={"user_id": "no-such-user"})
    assert r.status_code == 200
    assert r.json()["approvals"] == []


# ---------------------------------------------------------------------------
# GET /approvals/{id} + miscellany — probes 827-829
# ---------------------------------------------------------------------------


def test_probe_827_get_approval_returns_current_state(client, app) -> None:
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    aid = _create_approval(client, rid, sid)
    r = client.get(f"/approvals/{aid}")
    assert r.status_code == 200
    assert r.json()["approval"]["status"] == "pending"
    # After approve, GET must return the new state
    client.post("/approvals/approve", json={
        "approval_id": str(aid), "decided_by": "alice",
    })
    r = client.get(f"/approvals/{aid}")
    assert r.json()["approval"]["status"] == "approved"


def test_probe_828_get_approval_unknown_id_returns_404(client) -> None:
    r = client.get(f"/approvals/{uuid.uuid4()}")
    assert r.status_code == 404


def test_probe_829_create_then_pending_returns_one(client, app) -> None:
    """End-to-end smoke: create an approval, then list pending —
    the count should be 1."""
    _, eng = app
    rid, sid = _seed_run_and_step(eng)
    _create_approval(client, rid, sid)
    r = client.post("/approvals/pending", json={"user_id": "u"})
    assert r.status_code == 200
    assert len(r.json()["approvals"]) == 1
