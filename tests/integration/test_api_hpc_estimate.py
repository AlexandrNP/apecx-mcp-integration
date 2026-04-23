"""T07 API integration: /hpc/estimate end-to-end against live FastAPI.

Fixture chain: migrated SQLite → Run row → Artifact row pointing at a
real on-disk workflow YAML → TestClient POST /hpc/estimate. No mocks;
all persistence + file I/O is real.

Covers the three error branches (404 run / 422 no config / 404
missing file) plus the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers (keep fixture-creation explicit so tests are easy to scan)
# ---------------------------------------------------------------------------

def _insert_run(engine: Engine, *, workflow_config_id=None):
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, workflow_config_id) "
                "VALUES (:id, 'alex', 'PENDING', :ts, :wcid)"
            ),
            {
                "id": str(run_id),
                "ts": datetime.now(UTC).isoformat(),
                "wcid": str(workflow_config_id) if workflow_config_id else None,
            },
        )
    return run_id


def _insert_workflow_artifact(
    engine: Engine,
    run_id,
    *,
    yaml_text: str,
    on_disk_path: Path,
) -> str:
    """Drop the yaml on disk + insert an Artifact row pointing at it."""
    on_disk_path.write_text(yaml_text, encoding="utf-8")
    import hashlib
    content_hash = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
    artifact_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', :loc, :h, :sz, "
                "'application/yaml', :ts)"
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
        # Back-link: set the Run's workflow_config_id to this artifact.
        conn.execute(
            text("UPDATE run SET workflow_config_id = :aid WHERE id = :rid"),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
    return artifact_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

SAMPLE_WORKFLOW_YAML = """\
name: sample_wf
description: "one LLM step + one reader"
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


def test_estimate_happy_path_returns_response_with_populated_fields(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    _insert_workflow_artifact(
        cp_engine, run_id,
        yaml_text=SAMPLE_WORKFLOW_YAML,
        on_disk_path=tmp_path / "wf.yml",
    )

    response = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
    assert response.status_code == 200, response.text

    body = response.json()
    # extract matches 'Agent' per the estimator heuristic? No — class
    # name is 'EntityExtractionStep'; doesn't contain Agent/LLM/Ollama
    # as substrings → falls to generic 0.1.
    # 'DelimitedFileReaderStep' contains 'FileReader' → 0.01.
    # Total = 0.11.
    assert body["total_core_hours"] == pytest.approx(0.11)
    assert set(body["per_step_core_hours"].keys()) == {"extract", "read"}
    assert body["endpoint"] == "local"
    # Confidence interval is ~(0.3× total, 3.0× total).
    low, high = body["confidence_interval"]
    assert low == pytest.approx(0.033, abs=1e-3)
    assert high == pytest.approx(0.33, abs=1e-3)
    assert body["novel_python_capped_at"] is None


# ---------------------------------------------------------------------------
# 404: unknown run
# ---------------------------------------------------------------------------

def test_estimate_returns_404_for_unknown_run(cp_client: TestClient):
    response = cp_client.post(
        "/hpc/estimate",
        json={"run_id": str(uuid4())},
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 422: run has no workflow_config_id
# ---------------------------------------------------------------------------

def test_estimate_returns_422_when_run_has_no_workflow_config(
    cp_client: TestClient, cp_engine: Engine
):
    run_id = _insert_run(cp_engine)  # no workflow_config_id
    response = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
    assert response.status_code == 422
    assert "no workflow_config_id" in response.json()["detail"]


# ---------------------------------------------------------------------------
# NOTE: the "404 when Artifact row is missing" branch in the route is
# defensive — the ``run.workflow_config_id → artifact.id`` FK is
# enforced at DB level (T09 uses ``use_alter`` but the constraint is
# active), so an update to a non-existent Artifact raises IntegrityError
# at insert-time. That makes the route-level check unreachable via the
# normal API. The check stays in the route for defense in depth (e.g.,
# if someone runs with FKs disabled via PRAGMA). No test here.


# ---------------------------------------------------------------------------
# 404: Artifact row exists but on-disk file is gone
# ---------------------------------------------------------------------------

def test_estimate_returns_404_when_on_disk_file_missing(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    on_disk = tmp_path / "wf_to_delete.yml"
    _insert_workflow_artifact(
        cp_engine, run_id,
        yaml_text=SAMPLE_WORKFLOW_YAML,
        on_disk_path=on_disk,
    )
    # Delete the file (simulate manual tamper / bypassed API).
    on_disk.unlink()
    response = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
    assert response.status_code == 404
    assert "on-disk file" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 422: Artifact yaml malformed
# ---------------------------------------------------------------------------

def test_estimate_returns_422_when_artifact_yaml_is_malformed(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    _insert_workflow_artifact(
        cp_engine, run_id,
        yaml_text="name: x\n\tinvalid tab indent\n",
        on_disk_path=tmp_path / "bad.yml",
    )
    response = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 422: Artifact yaml is a list, not a mapping
# ---------------------------------------------------------------------------

def test_estimate_returns_422_when_artifact_yaml_is_a_list(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    _insert_workflow_artifact(
        cp_engine, run_id,
        yaml_text="- a\n- b\n- c\n",
        on_disk_path=tmp_path / "list.yml",
    )
    response = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 422: Artifact yaml is a dict but has no steps: block
# ---------------------------------------------------------------------------

def test_estimate_returns_422_when_workflow_has_no_steps_block(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    _insert_workflow_artifact(
        cp_engine, run_id,
        yaml_text="name: missing_steps\ndescription: x\n",
        on_disk_path=tmp_path / "no_steps.yml",
    )
    response = cp_client.post("/hpc/estimate", json={"run_id": str(run_id)})
    assert response.status_code == 422
    assert "steps" in response.json()["detail"]
