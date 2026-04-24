"""T05 API integration — ``/hpc/export`` PBS bundle generation.

Live FastAPI + migrated SQLite + real workflow YAML on disk. Mirrors
the ``test_api_hpc_estimate.py`` fixture pattern.
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


SAMPLE_YAML = """\
name: export_test_wf
description: "export test"
version: "0.1"
steps:
  extract:
    class: "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"
    config: "steps/entity_extraction.yml"
links: {}
"""


def _insert_run_with_artifact(
    engine: Engine, tmp_path: Path, *, yaml_text: str = SAMPLE_YAML,
    with_generated: bool = True,
):
    """Insert Run (NULL workflow_config_id) → Artifact → backlink.

    The FK is use_alter, so insert Run with NULL first, then the
    Artifact (which FKs to run.id), then UPDATE run with the artifact id.
    """
    run_id = uuid4()
    artifact_id = uuid4()
    on_disk = tmp_path / "wf.yml"
    on_disk.write_text(yaml_text)
    content_hash = hashlib.sha256(yaml_text.encode()).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'PENDING', :ts)"
            ),
            {
                "id": str(run_id),
                "ts": datetime.now(UTC).isoformat(),
            },
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
                "h": content_hash,
                "sz": len(yaml_text.encode()),
                "ts": datetime.now(UTC).isoformat(),
            },
        )
        conn.execute(
            text(
                "UPDATE run SET workflow_config_id = :aid WHERE id = :rid"
            ),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
        if with_generated:
            conn.execute(
                text(
                    "INSERT INTO generated_artifact (artifact_id, "
                    "source_prompt, library_version, llm_model, "
                    "llm_model_version_hash, composition_summary, "
                    "parent_artifact_id) VALUES "
                    "(:aid, 'prompt', '0.1.0', 'mistral-nemo', "
                    ":lmh, :cs, NULL)"
                ),
                {
                    "aid": str(artifact_id),
                    "lmh": "0" * 64,
                    "cs": '{"summary_sentence": "This workflow has 1 '
                          'step(s). 1 compose library components "}',
                },
            )
    return run_id, artifact_id, on_disk


def test_export_happy_path_writes_full_bundle(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id, _, _ = _insert_run_with_artifact(cp_engine, tmp_path)
    bundle_dir = tmp_path / "bundle"

    resp = cp_client.post(
        "/hpc/export",
        json={
            "run_id": str(run_id),
            "target_system": "polaris",
            "output_directory": str(bundle_dir),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bundle_path"].endswith("bundle")
    assert "qsub submit.pbs" in body["submit_command"]

    # Every required file present.
    bundle = Path(body["bundle_path"])
    for f in (
        "submit.pbs",
        "run.sh",
        "workflow.yml",
        "staging_plan.yml",
        "provenance_seed.json",
        "README.md",
    ):
        assert (bundle / f).is_file()

    # Provenance seed embeds the Run's identity.
    seed = json.loads((bundle / "provenance_seed.json").read_text())
    assert seed["run_id"] == str(run_id)


def test_export_404_for_unknown_run(
    cp_client: TestClient, tmp_path: Path
):
    resp = cp_client.post(
        "/hpc/export",
        json={
            "run_id": str(uuid4()),
            "target_system": "polaris",
            "output_directory": str(tmp_path / "bundle"),
        },
    )
    assert resp.status_code == 404


def test_export_422_for_unsupported_system(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id, _, _ = _insert_run_with_artifact(cp_engine, tmp_path)
    resp = cp_client.post(
        "/hpc/export",
        json={
            "run_id": str(run_id),
            "target_system": "frontier",
            "output_directory": str(tmp_path / "bundle"),
        },
    )
    assert resp.status_code == 422
    assert "not supported" in resp.json()["detail"]


def test_export_422_when_generated_artifact_row_missing(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id, _, _ = _insert_run_with_artifact(
        cp_engine, tmp_path, with_generated=False
    )
    resp = cp_client.post(
        "/hpc/export",
        json={
            "run_id": str(run_id),
            "target_system": "polaris",
            "output_directory": str(tmp_path / "bundle"),
        },
    )
    assert resp.status_code == 422
    assert "GeneratedArtifact" in resp.json()["detail"]


def test_export_404_when_on_disk_yaml_missing(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id, _, yaml_path = _insert_run_with_artifact(cp_engine, tmp_path)
    yaml_path.unlink()
    resp = cp_client.post(
        "/hpc/export",
        json={
            "run_id": str(run_id),
            "target_system": "polaris",
            "output_directory": str(tmp_path / "bundle"),
        },
    )
    assert resp.status_code == 404
