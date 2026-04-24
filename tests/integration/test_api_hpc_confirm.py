"""T07 follow-up — ``/hpc/confirm`` user-acknowledgement gate.

Integration tests hit the live FastAPI + migrated SQLite. No mocks;
real AllocationEstimate rows get written by /hpc/estimate and read
by /hpc/confirm.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


SAMPLE_YAML = """\
name: confirm_test_wf
description: "two-step workflow for /hpc/confirm tests"
version: "0.1"
steps:
  extract:
    class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
    config: "steps/entity_extraction.yml"
  read:
    class: "apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep"
    config: "steps/reader.yml"
links: {}
"""


def _insert_run(engine: Engine, *, workflow_config_id=None):
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, "
                "workflow_config_id) VALUES (:id, 'alex', 'PENDING', "
                ":ts, :wcid)"
            ),
            {
                "id": str(run_id),
                "ts": datetime.now(UTC).isoformat(),
                "wcid": str(workflow_config_id) if workflow_config_id else None,
            },
        )
    return run_id


def _insert_workflow_artifact(
    engine: Engine, run_id, *, yaml_text: str, on_disk_path: Path
) -> str:
    on_disk_path.write_text(yaml_text, encoding="utf-8")
    content_hash = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
    artifact_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', :loc, :h, "
                ":sz, 'application/yaml', :ts)"
            ),
            {
                "id": str(artifact_id),
                "rid": str(run_id),
                "loc": str(on_disk_path),
                "h": content_hash,
                "sz": len(yaml_text.encode("utf-8")),
                "ts": datetime.now(UTC).isoformat(),
            },
        )
        conn.execute(
            text("UPDATE run SET workflow_config_id = :aid WHERE id = :rid"),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
    return artifact_id


def _count_estimates(engine: Engine, run_id) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM allocation_estimate "
                "WHERE run_id = :rid"
            ),
            {"rid": str(run_id)},
        ).first()
    return int(row[0])


def test_estimate_persists_allocation_estimate_row(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    """Each /hpc/estimate call writes a new AllocationEstimate row
    — audit trail of every look the user took."""
    run_id = _insert_run(cp_engine)
    _insert_workflow_artifact(
        cp_engine, run_id, yaml_text=SAMPLE_YAML, on_disk_path=tmp_path / "wf.yml"
    )

    assert _count_estimates(cp_engine, run_id) == 0

    for _ in range(3):
        r = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
        assert r.status_code == 200, r.text

    assert _count_estimates(cp_engine, run_id) == 3


def test_confirm_happy_path_flips_user_confirmed(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    _insert_workflow_artifact(
        cp_engine, run_id, yaml_text=SAMPLE_YAML, on_disk_path=tmp_path / "wf.yml"
    )
    est = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
    assert est.status_code == 200
    estimated = est.json()["total_core_hours"]

    confirm = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(run_id), "confirmed_core_hours": estimated},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["confirmed"] is True

    # Row in DB must be flipped.
    with cp_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT user_confirmed, user_confirmed_at FROM "
                "allocation_estimate WHERE run_id = :rid"
            ),
            {"rid": str(run_id)},
        ).first()
    assert row is not None
    confirmed, confirmed_at = row
    assert bool(confirmed) is True
    assert confirmed_at is not None


def test_confirm_returns_404_for_unknown_run(cp_client: TestClient):
    r = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(uuid4()), "confirmed_core_hours": 1.0},
    )
    assert r.status_code == 404


def test_confirm_returns_422_when_no_estimate_exists(
    cp_client: TestClient, cp_engine: Engine
):
    """Run exists but no prior /hpc/estimate call — nothing to confirm."""
    run_id = _insert_run(cp_engine)
    r = cp_client.post(
        "/hpc/confirm",
        json={"run_id": str(run_id), "confirmed_core_hours": 1.0},
    )
    assert r.status_code == 422
    assert "no AllocationEstimate" in r.json()["detail"]


def test_confirm_returns_422_when_ceiling_below_estimate(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    """User tries to confirm at a ceiling lower than the estimate."""
    run_id = _insert_run(cp_engine)
    _insert_workflow_artifact(
        cp_engine, run_id, yaml_text=SAMPLE_YAML, on_disk_path=tmp_path / "wf.yml"
    )
    est = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
    assert est.status_code == 200
    estimated = est.json()["total_core_hours"]

    r = cp_client.post(
        "/hpc/confirm",
        json={
            "run_id": str(run_id),
            "confirmed_core_hours": estimated * 0.5,
        },
    )
    assert r.status_code == 422
    assert "ceiling" in r.json()["detail"]
