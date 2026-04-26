"""Probe batch 5 — remaining surface areas.

Probes 126-150. Each test is one distinct adversarial probe.
Categories: status enum boundaries, route HTTP method exclusivity,
ORM round-trip integrity, MCP client wrappers, edge cases in
config loading.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from apecx_integration.control_plane.db import make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import ProvenanceEventType
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


def _seed_run(engine: Engine, *, status_value: str = "PENDING") -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', :st, :ts)"
            ),
            {"id": str(run_id), "st": status_value, "ts": now},
        )
    return run_id


# --- Probe 126: GET on a POST-only route returns 405 ---


def test_probe_126_method_not_allowed(cp_client: TestClient) -> None:
    r = cp_client.get("/workflows/start")
    assert r.status_code == 405


# --- Probe 127: POST on /healthz fails 405 ---


def test_probe_127_healthz_post_405(cp_client: TestClient) -> None:
    r = cp_client.post("/healthz")
    assert r.status_code == 405


# --- Probe 128: PUT on /verified_synonyms/{id} 405 ---


def test_probe_128_verified_synonyms_put_405(cp_client: TestClient) -> None:
    r = cp_client.put(f"/verified_synonyms/{uuid4()}")
    assert r.status_code == 405


# --- Probe 129: DELETE on /verified_synonyms/{id} 405 ---


def test_probe_129_verified_synonyms_delete_405(cp_client: TestClient) -> None:
    r = cp_client.delete(f"/verified_synonyms/{uuid4()}")
    assert r.status_code == 405


# --- Probe 130: /openapi.json is reachable ---


def test_probe_130_openapi_json(cp_client: TestClient) -> None:
    r = cp_client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "/healthz" in spec["paths"]


# --- Probe 131: /docs is reachable ---


def test_probe_131_docs(cp_client: TestClient) -> None:
    r = cp_client.get("/docs")
    assert r.status_code == 200


# --- Probe 132: /verified_synonyms PATCH with null body 422 ---


def test_probe_132_revoke_null_body(cp_client: TestClient) -> None:
    r = cp_client.patch(f"/verified_synonyms/{uuid4()}")
    # No body → Pydantic 422
    assert r.status_code == 422


# --- Probe 133: All ProvenanceEventType values can be recorded ---


def test_probe_133_all_event_types_recordable(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    for event_type in ProvenanceEventType:
        # Skip RUN_STARTED which has a unique partial index
        if event_type is ProvenanceEventType.RUN_STARTED:
            continue
        run_id = _seed_run(cp_engine)
        evt = recorder.record(
            run_id=run_id,
            event_type=event_type,
            actor="probe",
            payload={"t": event_type.value},
        )
        assert evt.event_type is event_type


# --- Probe 134: ProvenanceEvent payload empty dict round-trips ---


def test_probe_134_empty_payload(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    evt = recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.STEP_STARTED,
        actor="probe",
        payload={},
    )
    assert evt.payload == {}


# --- Probe 135: Step ORM round-trip with all fields ---


def test_probe_135_step_orm_full_round_trip(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import (
        Step as StepORM,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ExecutorKind,
        StepStatus,
    )

    run_id = _seed_run(cp_engine)
    step_id = uuid4()
    factory = make_session_factory(cp_engine)
    with factory() as session:
        s = StepORM(
            id=step_id,
            run_id=run_id,
            step_name="t",
            executor=ExecutorKind.LOCAL,
            status=StepStatus.PENDING,
            input_artifact_ids=[],
            output_artifact_ids=[],
            created_at=datetime.now(UTC),
        )
        session.add(s)
        session.commit()
    with factory() as session:
        s2 = session.get(StepORM, step_id)
        assert s2 is not None
        assert s2.step_name == "t"
        assert s2.status is StepStatus.PENDING


# --- Probe 136: Approval ORM with policy=dict round-trips ---


def test_probe_136_approval_policy_round_trip(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import (
        Approval as ApprovalORM,
        Step as StepORM,
    )
    from apecx_integration.control_plane.schemas.enums import (
        ApprovalKind,
        ApprovalStatus,
        ExecutorKind,
        StepStatus,
    )

    run_id = _seed_run(cp_engine)
    step_id = uuid4()
    aid = uuid4()
    factory = make_session_factory(cp_engine)
    with factory() as session:
        session.add(StepORM(
            id=step_id,
            run_id=run_id,
            step_name="t",
            executor=ExecutorKind.LOCAL,
            status=StepStatus.PENDING,
            input_artifact_ids=[],
            output_artifact_ids=[],
            created_at=datetime.now(UTC),
        ))
        session.add(ApprovalORM(
            id=aid,
            step_id=step_id,
            kind=ApprovalKind.HARD,
            status=ApprovalStatus.PENDING,
            policy={"a": 1, "b": [1, 2, 3], "c": {"nested": True}},
            created_at=datetime.now(UTC),
        ))
        session.commit()
    with factory() as session:
        a = session.get(ApprovalORM, aid)
        assert a.policy == {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}


# --- Probe 137: ProvenanceEvent.payload is queryable as JSON ---


def test_probe_137_payload_is_dict_after_read(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    payload = {"k": [1, 2, "three"], "n": None}
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.RUN_STARTED,
        actor="probe",
        payload=payload,
    )
    from apecx_integration.control_plane.models.entities import (
        ProvenanceEvent,
    )
    with factory() as session:
        rows = session.query(ProvenanceEvent).filter_by(run_id=run_id).all()
        assert len(rows) == 1
        # JSON column round-trips structure exactly.
        assert rows[0].payload == payload


# --- Probe 138: VerifiedSynonym ORM stores scope=None correctly ---


def test_probe_138_verified_synonym_null_scope(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import (
        VerifiedSynonym as VSORM,
    )

    factory = make_session_factory(cp_engine)
    sid = uuid4()
    with factory() as session:
        session.add(VSORM(
            id=sid,
            source_vocabulary="v",
            query_term="t",
            target_vocabulary="b",
            canonical_term="X",
            scope=None,
            verified_by="alex",
            verified_at=datetime.now(UTC),
            confidence=1.0,
        ))
        session.commit()
    with factory() as session:
        v = session.get(VSORM, sid)
        assert v.scope is None


# --- Probe 139: AllocationEstimate ORM with NaN-on-write — Pydantic schema doesn't apply at ORM layer ---


def test_probe_139_allocation_estimate_orm_accepts_finite(cp_engine: Engine) -> None:
    """ORM doesn't enforce Pydantic constraints; verify finite values
    round-trip clean."""
    from apecx_integration.control_plane.models.entities import (
        AllocationEstimate as AEORM,
    )

    factory = make_session_factory(cp_engine)
    run_id = _seed_run(cp_engine)
    aid = uuid4()
    with factory() as session:
        session.add(AEORM(
            id=aid,
            run_id=run_id,
            estimated_core_hours=42.5,
            estimated_wall_time_seconds=15300.0,
            endpoint="polaris",
            user_confirmed=False,
            created_at=datetime.now(UTC),
        ))
        session.commit()
    with factory() as session:
        a = session.get(AEORM, aid)
        assert a.estimated_core_hours == 42.5


# --- Probe 140: Run.parent_run_id can be set; FK enforced ---


def test_probe_140_run_parent_fk_enforced(cp_engine: Engine) -> None:
    """parent_run_id pointing at non-existent run should raise FK
    error. Cluster X-style data-integrity check."""
    fake_parent = uuid4()
    with cp_engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO run (id, user_id, status, created_at, "
                    "parent_run_id) VALUES (:id, 'alex', 'PENDING', :ts, :p)"
                ),
                {
                    "id": str(uuid4()),
                    "ts": datetime.now(UTC).isoformat(),
                    "p": str(fake_parent),
                },
            )


# --- Probe 141: Run.parent_run_id pointing at real parent works ---


def test_probe_141_run_parent_real(cp_engine: Engine) -> None:
    parent_id = _seed_run(cp_engine)
    child_id = uuid4()
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, "
                "parent_run_id) VALUES (:id, 'alex', 'PENDING', :ts, :p)"
            ),
            {
                "id": str(child_id),
                "ts": datetime.now(UTC).isoformat(),
                "p": str(parent_id),
            },
        )
    with cp_engine.connect() as conn:
        row = conn.execute(
            text("SELECT parent_run_id FROM run WHERE id = :id"),
            {"id": str(child_id)},
        ).scalar_one()
        assert UUID(row) == parent_id


# --- Probe 142: Approval.step_id FK enforced ---


def test_probe_142_approval_step_fk_enforced(cp_engine: Engine) -> None:
    fake_step = uuid4()
    with cp_engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO approval (id, step_id, kind, status, "
                    "policy, created_at) VALUES "
                    "(:id, :sid, 'HARD', 'PENDING', '{}', :ts)"
                ),
                {
                    "id": str(uuid4()),
                    "sid": str(fake_step),
                    "ts": datetime.now(UTC).isoformat(),
                },
            )


# --- Probe 143: VerifiedSynonym.superseded_by FK enforced ---


def test_probe_143_verified_synonym_superseded_fk(cp_engine: Engine) -> None:
    fake = uuid4()
    with cp_engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO verified_synonym (id, source_vocabulary, "
                    "query_term, target_vocabulary, canonical_term, "
                    "verified_by, verified_at, confidence, is_active, "
                    "superseded_by) VALUES (:id, 'v', 't', 'b', 'X', 'a', "
                    ":ts, 1.0, 0, :s)"
                ),
                {
                    "id": str(uuid4()),
                    "ts": datetime.now(UTC).isoformat(),
                    "s": str(fake),
                },
            )


# --- Probe 144: VerifiedSynonym.is_active default ---


def test_probe_144_verified_synonym_active_default(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import (
        VerifiedSynonym as VSORM,
    )

    factory = make_session_factory(cp_engine)
    sid = uuid4()
    with factory() as session:
        session.add(VSORM(
            id=sid,
            source_vocabulary="v",
            query_term="t",
            target_vocabulary="b",
            canonical_term="X",
            verified_by="alex",
            verified_at=datetime.now(UTC),
            confidence=1.0,
        ))
        session.commit()
    with factory() as session:
        v = session.get(VSORM, sid)
        assert v.is_active is True


# --- Probe 145: Component ORM with string id ---


def test_probe_145_component_string_id(cp_engine: Engine) -> None:
    from apecx_integration.control_plane.models.entities import (
        Component,
    )

    factory = make_session_factory(cp_engine)
    with factory() as session:
        session.add(Component(
            id="my.unique.id",
            name="My Comp",
            version="1.0.0",
            description="desc",
            implementation_path="apecx_integration/comp.py",
        ))
        session.commit()
    with factory() as session:
        c = session.get(Component, "my.unique.id")
        assert c.name == "My Comp"


# --- Probe 146: Recorder records all events to same run produces single chain ---


def test_probe_146_recorder_single_chain_per_run(cp_engine: Engine) -> None:
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    for i in range(10):
        recorder.record(
            run_id=run_id,
            event_type=ProvenanceEventType.STEP_COMPLETED,
            actor="probe",
            payload={"i": i},
        )
    with cp_engine.connect() as conn:
        cnt_null = conn.execute(
            text(
                "SELECT COUNT(*) FROM provenance_event WHERE run_id = :r "
                "AND prev_event_hash IS NULL"
            ),
            {"r": str(run_id)},
        ).scalar_one()
        cnt_total = conn.execute(
            text(
                "SELECT COUNT(*) FROM provenance_event WHERE run_id = :r"
            ),
            {"r": str(run_id)},
        ).scalar_one()
    assert cnt_null == 1
    assert cnt_total == 10


# --- Probe 147: /verified_synonyms/lookup with non-matching scope returns null ---


def test_probe_147_lookup_scope_nonmatch(cp_client: TestClient) -> None:
    cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "v",
            "query_term": "scope-test",
            "target_vocabulary": "b",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": 1.0,
            "scope": "scope-A",
        },
    )
    r = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "v",
            "target_vocabulary": "b",
            "query_terms": ["scope-test"],
            "scope": "scope-B",  # different scope
        },
    )
    matches = r.json()["matches"]
    assert matches[0]["result"] is None


# --- Probe 148: /metrics/approvals window includes >= since ---


def test_probe_148_metrics_window_inclusive(cp_engine: Engine, cp_client: TestClient) -> None:
    """Events at exactly `since` time should be included."""
    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    run_id = _seed_run(cp_engine)
    fixed = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
    aid = str(uuid4())
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.APPROVAL_REQUESTED,
        actor="probe",
        payload={"approval_id": aid},
        now=fixed,
    )
    recorder.record(
        run_id=run_id,
        event_type=ProvenanceEventType.APPROVAL_DECIDED,
        actor="probe",
        payload={"approval_id": aid},
        now=fixed,
    )
    # URL encoding: +00:00 in the path → '+' is space when decoded;
    # use Z form or pass via params= for proper urlencoding.
    r = cp_client.get(
        "/metrics/approvals",
        params={"since": fixed.isoformat()},
    )
    assert r.status_code == 200
    # Probe asserts the route counts paired REQUESTED/DECIDED events;
    # we wrote one pair so count == 1. The Approval row isn't
    # required (route counts events).
    body = r.json()
    assert body["count"] == 1


# --- Probe 149: ConfigBase from_yaml round-trip via Composer.from_config ---


def test_probe_149_composer_config_from_yaml(tmp_path) -> None:
    """ComposerConfig loads cleanly from a minimal YAML."""
    from apecx_integration.composition.composer_schemas import ComposerConfig

    yaml_text = """
library_version: "0.1.0"
prompt_dir: "/tmp/prompts"
"""
    cfg_path = tmp_path / "c.yml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    import yaml as _yaml
    raw = _yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg = ComposerConfig.model_validate(raw)
    assert cfg.library_version == "0.1.0"


# --- Probe 150: ComposerConfig rejects negative max_tokens ---


def test_probe_150_composer_config_invalid_max_tokens() -> None:
    from apecx_integration.composition.composer_schemas import ComposerConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ComposerConfig(
            library_version="0.1.0",
            prompt_dir="/tmp/p",
            max_tokens=-100,
        )
