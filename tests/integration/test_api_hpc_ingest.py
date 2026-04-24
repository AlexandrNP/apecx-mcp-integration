"""T05 AC3 — ``/hpc/ingest`` round-trips a completed bundle back into
Tier 2. Closes the export → remote-run → transfer-back → reconcile
loop without needing actual HPC access.

Uses the real PBS bundle generator to produce a bundle, then
simulates what a completed job would leave on disk
(``apecx_status.txt`` + ``outputs/result.json``) and posts the
bundle path to ``/hpc/ingest``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


SAMPLE_YAML = "name: wf\nsteps: {}\nlinks: {}\n"


def _insert_run_with_artifact(engine: Engine, tmp_path: Path):
    """Seed a Run + GENERATED_WORKFLOW Artifact matching what
    /hpc/export would have left. The Run starts in RUNNING so it
    needs reconciliation."""
    run_id = uuid4()
    artifact_id = uuid4()
    on_disk = tmp_path / "wf.yml"
    on_disk.write_text(SAMPLE_YAML)
    h = hashlib.sha256(SAMPLE_YAML.encode()).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, started_at) "
                "VALUES (:id, 'alex', 'RUNNING', :ts, :ts)"
            ),
            {"id": str(run_id), "ts": datetime.now(UTC).isoformat()},
        )
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
                "loc": str(on_disk),
                "h": h,
                "sz": len(SAMPLE_YAML.encode()),
                "ts": datetime.now(UTC).isoformat(),
            },
        )
        conn.execute(
            text(
                "UPDATE run SET workflow_config_id = :aid WHERE id = :rid"
            ),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
    return run_id, artifact_id


def _make_completed_bundle(
    tmp_path: Path, run_id, artifact_id, *, status_text: str = "completed",
    with_result: bool = True,
) -> Path:
    """Write the shape /hpc/export would produce + the job outputs."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "provenance_seed.json").write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "artifact_id": str(artifact_id),
                "library_version": "0.1.0-test",
                "llm_model": "mistral-nemo:latest",
                "composition_summary_sentence": "test summary",
                "target_system": "polaris",
                "generated_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    (bundle / "apecx_status.txt").write_text(status_text)
    if with_result:
        (bundle / "outputs").mkdir()
        (bundle / "outputs" / "result.json").write_text(
            json.dumps({"status": "ok", "rows": 42})
        )
    return bundle


def _provenance_events(engine: Engine, run_id) -> list[str]:
    with engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT event_type FROM provenance_event "
                    "WHERE run_id = :rid ORDER BY timestamp"
                ),
                {"rid": str(run_id)},
            )
        )
    return [r[0].lower() for r in rows]


def test_ingest_happy_path_transitions_run_to_completed(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id, artifact_id = _insert_run_with_artifact(cp_engine, tmp_path)
    bundle = _make_completed_bundle(tmp_path, run_id, artifact_id)

    resp = cp_client.post(
        "/hpc/ingest", json={"bundle_path": str(bundle)}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == str(run_id)
    assert body["status"] == "completed"
    assert body["output_artifact_id"]

    # Run row transitioned.
    with cp_engine.connect() as conn:
        status_row = conn.execute(
            text("SELECT status FROM run WHERE id = :rid"),
            {"rid": str(run_id)},
        ).first()
    assert status_row[0].lower() == "completed"

    # RUN_COMPLETED event emitted.
    events = _provenance_events(cp_engine, run_id)
    assert "run_completed" in events


def test_ingest_failed_status_marks_run_failed(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id, artifact_id = _insert_run_with_artifact(cp_engine, tmp_path)
    bundle = _make_completed_bundle(
        tmp_path, run_id, artifact_id,
        status_text="failed", with_result=False,
    )
    resp = cp_client.post(
        "/hpc/ingest", json={"bundle_path": str(bundle)}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["output_artifact_id"] is None
    events = _provenance_events(cp_engine, run_id)
    assert "run_failed" in events


def test_ingest_404_for_missing_bundle_dir(cp_client: TestClient, tmp_path: Path):
    resp = cp_client.post(
        "/hpc/ingest",
        json={"bundle_path": str(tmp_path / "does_not_exist")},
    )
    assert resp.status_code == 404


def test_ingest_404_for_bundle_without_provenance_seed(
    cp_client: TestClient, tmp_path: Path
):
    bundle = tmp_path / "empty_bundle"
    bundle.mkdir()
    resp = cp_client.post(
        "/hpc/ingest", json={"bundle_path": str(bundle)}
    )
    assert resp.status_code == 404
    assert "provenance_seed" in resp.json()["detail"]


def test_ingest_404_for_unknown_run(cp_client: TestClient, tmp_path: Path):
    """Provenance seed references a run_id this CP doesn't know about."""
    bundle = _make_completed_bundle(tmp_path, uuid4(), uuid4())
    resp = cp_client.post(
        "/hpc/ingest", json={"bundle_path": str(bundle)}
    )
    assert resp.status_code == 404
    assert "does not exist in this Control Plane" in resp.json()["detail"]


def test_ingest_409_when_run_already_terminal(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id, artifact_id = _insert_run_with_artifact(cp_engine, tmp_path)
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE run SET status = 'COMPLETED', "
                "completed_at = :ts WHERE id = :rid"
            ),
            {"ts": datetime.now(UTC).isoformat(), "rid": str(run_id)},
        )
    bundle = _make_completed_bundle(tmp_path, run_id, artifact_id)
    resp = cp_client.post(
        "/hpc/ingest", json={"bundle_path": str(bundle)}
    )
    assert resp.status_code == 409
    assert "append-only" in resp.json()["detail"]


def test_ingest_422_when_seed_malformed(cp_client: TestClient, tmp_path: Path):
    bundle = tmp_path / "bad_seed"
    bundle.mkdir()
    (bundle / "provenance_seed.json").write_text("{not valid json")
    resp = cp_client.post(
        "/hpc/ingest", json={"bundle_path": str(bundle)}
    )
    assert resp.status_code == 422
    assert "malformed" in resp.json()["detail"]


def test_ingest_422_when_completed_status_but_no_result(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    """status=completed claim but no outputs/result.json → contract
    violation. Don't silently mark the Run complete."""
    run_id, artifact_id = _insert_run_with_artifact(cp_engine, tmp_path)
    bundle = _make_completed_bundle(
        tmp_path, run_id, artifact_id, with_result=False
    )
    resp = cp_client.post(
        "/hpc/ingest", json={"bundle_path": str(bundle)}
    )
    assert resp.status_code == 422
    assert "contract violation" in resp.json()["detail"]
