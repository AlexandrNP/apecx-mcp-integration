"""T06 API integration: /workflows/diff end-to-end against live FastAPI.

Mirrors the fixture pattern of ``test_api_hpc_estimate.py``:
migrated SQLite → Run row → Artifact row on disk → GeneratedArtifact
metadata row → POST /workflows/diff → verify categorization + yaml
round-trip. No mocks; real persistence + real file I/O.
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
name: diff_test_wf
description: "mixed composed + novel"
version: "0.1"
steps:
  extract:
    class: "pkg.library.A"
    config: "steps/a.yml"
  rogue:
    class: "generated.Rogue"
    config: {}
links: {}
"""


def _insert_run(engine: Engine, *, workflow_config_id=None):
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at, "
                "workflow_config_id) "
                "VALUES (:id, 'alex', 'PENDING', :ts, :wcid)"
            ),
            {
                "id": str(run_id),
                "ts": datetime.now(UTC).isoformat(),
                "wcid": str(workflow_config_id) if workflow_config_id else None,
            },
        )
    return run_id


def _insert_diff_fixture(
    engine: Engine,
    run_id,
    *,
    yaml_text: str,
    on_disk_path: Path,
    with_generated_metadata: bool = True,
):
    """Drop YAML on disk; insert Artifact + GeneratedArtifact rows;
    back-link run.workflow_config_id -> artifact."""
    on_disk_path.write_text(yaml_text, encoding="utf-8")
    content_hash = hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()
    artifact_id = uuid4()
    now = datetime.now(UTC).isoformat()

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
                "ts": now,
            },
        )
        if with_generated_metadata:
            conn.execute(
                text(
                    "INSERT INTO generated_artifact "
                    "(artifact_id, source_prompt, library_version, "
                    "llm_model, llm_model_version_hash, "
                    "composition_summary, parent_artifact_id) "
                    "VALUES (:aid, :prompt, :lv, :lm, :lmh, :cs, NULL)"
                ),
                {
                    "aid": str(artifact_id),
                    "prompt": "extract pathogens and rogue-shell",
                    "lv": "0.1.0-test",
                    "lm": "mistral-small:latest",
                    "lmh": "0" * 64,
                    "cs": '{"steps_reused": 1, "steps_generated": 1, '
                          '"steps_swapped": 0, '
                          '"summary_sentence": "This workflow has 2 '
                          'step(s). 1 compose library components '
                          '(1 standard + 0 parameterized + 0 wrapped). '
                          '1 step(s) are novel Python requiring review.", '
                          '"step_categorizations": ['
                          '{"step_id": "extract", "step_class": '
                          '"pkg.library.A", "category": '
                          '"composed_standard", "reason": "library '
                          'class with canonical wrapper YAML path."},'
                          '{"step_id": "rogue", "step_class": '
                          '"generated.Rogue", "category": "novel", '
                          '"reason": "step_id appears in the '
                          'novel_python fence."}], '
                          '"review_notes": ["novel Python step: rogue"], '
                          '"novel_python_by_step": {"rogue": "class '
                          'Rogue: ...\\n"}}',
                },
            )
        conn.execute(
            text("UPDATE run SET workflow_config_id = :aid WHERE id = :rid"),
            {"aid": str(artifact_id), "rid": str(run_id)},
        )
    return artifact_id


def test_diff_happy_path_returns_categorization_and_novel_python(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    _insert_diff_fixture(
        cp_engine, run_id,
        yaml_text=SAMPLE_YAML,
        on_disk_path=tmp_path / "wf.yml",
    )

    response = cp_client.post(
        "/workflows/diff", json={"run_id": str(run_id)}
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["yaml_text"].startswith("name: diff_test_wf")
    assert list(body["novel_python_by_step"].keys()) == ["rogue"]
    assert "1 step(s) are novel Python" in body["summary_sentence"]

    cats = body["categorization"]
    assert len(cats) == 2
    by_id = {c["step_id"]: c for c in cats}
    assert by_id["extract"]["category"] == "composed_standard"
    assert by_id["rogue"]["category"] == "novel"


def test_diff_returns_404_for_unknown_run(cp_client: TestClient):
    response = cp_client.post(
        "/workflows/diff", json={"run_id": str(uuid4())}
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_diff_returns_422_when_run_has_no_workflow_config(
    cp_client: TestClient, cp_engine: Engine
):
    run_id = _insert_run(cp_engine)
    response = cp_client.post(
        "/workflows/diff", json={"run_id": str(run_id)}
    )
    assert response.status_code == 422
    assert "no workflow_config_id" in response.json()["detail"]


def test_diff_returns_404_when_on_disk_file_missing(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    on_disk = tmp_path / "to_delete.yml"
    _insert_diff_fixture(
        cp_engine, run_id, yaml_text=SAMPLE_YAML, on_disk_path=on_disk,
    )
    on_disk.unlink()
    response = cp_client.post(
        "/workflows/diff", json={"run_id": str(run_id)}
    )
    assert response.status_code == 404


def test_diff_returns_422_when_generated_artifact_row_missing(
    cp_client: TestClient, cp_engine: Engine, tmp_path: Path
):
    run_id = _insert_run(cp_engine)
    _insert_diff_fixture(
        cp_engine, run_id,
        yaml_text=SAMPLE_YAML,
        on_disk_path=tmp_path / "no_meta.yml",
        with_generated_metadata=False,
    )
    response = cp_client.post(
        "/workflows/diff", json={"run_id": str(run_id)}
    )
    assert response.status_code == 422
    assert "no GeneratedArtifact row" in response.json()["detail"]
