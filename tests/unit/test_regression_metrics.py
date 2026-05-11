"""Unit tests for ``apecx_integration.regression_metrics``.

We exercise the computation against a real migrated SQLite DB
seeded with hand-crafted GeneratedArtifact rows. No mocks for the
DB layer (workspace mocks-carve-out — the DB is a real external
dependency this code talks to).

The tests pin:

  1. Counts roll up correctly across multiple artifacts.
  2. compose_retries distribution captures both retried and
     happy-path artifacts.
  3. runtime_violations are tallied by rule_id AND failure_class.
  4. retrieval_gap rate reflects only steps with the flag set.
  5. ``since`` filter narrows the read window.
  6. JSON output round-trips through json.loads cleanly so the
     CLI's ``--json`` consumers don't have to wrestle with Python
     -only datatypes (datetime, etc.).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from apecx_integration.control_plane.db import (
    make_engine,
    make_session_factory,
)
from apecx_integration.regression_metrics import (
    compute_regression_metrics,
    report_as_json,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]


def _migrated_engine(tmp_path: Path):
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    return make_engine(url)


def _insert_artifact(
    engine,
    *,
    artifact_id: UUID,
    composition_summary: dict,
    created_at: datetime | None = None,
) -> None:
    """Insert one Run + one Artifact + one GeneratedArtifact row.

    Three rows because (a) Artifact.run_id is a NOT NULL FK to
    Run.id and (b) GeneratedArtifact.artifact_id is a FK to
    Artifact.id. ``created_at`` defaults to UTC-now so the
    ``since`` filter has a real timestamp to compare against.
    """
    ts = (created_at or datetime.now(UTC)).isoformat()
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'rm_test', 'COMPLETED', :ts)"
            ),
            {"id": str(run_id), "ts": ts},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', '/tmp/fake', "
                "'deadbeef', 0, 'application/yaml', :ts)"
            ),
            {"id": str(artifact_id), "rid": str(run_id), "ts": ts},
        )
        conn.execute(
            text(
                "INSERT INTO generated_artifact (artifact_id, source_prompt, "
                "library_version, llm_model, llm_model_version_hash, "
                "composition_summary) "
                "VALUES (:aid, 'p', 'v', 'm', 'h', :cs)"
            ),
            {
                "aid": str(artifact_id),
                "cs": json.dumps(composition_summary),
            },
        )


def test_empty_db_returns_zero_counts(tmp_path):
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    report = compute_regression_metrics(session_factory)
    assert report.total_artifacts == 0
    assert report.compose_retries.distribution == {}
    assert report.runtime_violations.total_violations == 0
    assert report.retrieval_gap.total_steps == 0
    assert report.retrieval_gap.rate == 0.0


def test_compose_retries_distribution(tmp_path):
    """Three artifacts: two with zero retries, one with two retries."""
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    _insert_artifact(engine, artifact_id=uuid4(), composition_summary={"compose_retries": 0})
    _insert_artifact(engine, artifact_id=uuid4(), composition_summary={"compose_retries": 0})
    _insert_artifact(engine, artifact_id=uuid4(), composition_summary={"compose_retries": 2})

    report = compute_regression_metrics(session_factory)
    assert report.total_artifacts == 3
    assert report.compose_retries.distribution == {0: 2, 2: 1}
    assert report.compose_retries.artifacts_with_retries == 1
    assert report.compose_retries.retry_rate == pytest.approx(1 / 3)


def test_runtime_violations_breakdown(tmp_path):
    """Two artifacts; one has two violations across two rule_ids."""
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={
            "compose_retries": 0,
            "runtime_violations": [
                {
                    "rule_id": "step_inline_config_forbidden",
                    "failure_class": "load_failed",
                    "exception_type": "ValueError",
                    "exception_message": "...",
                    "recorded_at": "2026-05-11T00:00:00+00:00",
                },
                {
                    "rule_id": "module_not_found",
                    "failure_class": "load_failed",
                    "exception_type": "ModuleNotFoundError",
                    "exception_message": "...",
                    "recorded_at": "2026-05-11T00:01:00+00:00",
                },
            ],
        },
    )
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={"compose_retries": 0},
    )

    report = compute_regression_metrics(session_factory)
    rv = report.runtime_violations
    assert rv.total_violations == 2
    assert rv.artifacts_with_violations == 1
    assert rv.by_rule_id == {
        "step_inline_config_forbidden": 1,
        "module_not_found": 1,
    }
    assert rv.by_failure_class == {"load_failed": 2}


def test_retrieval_gap_rate(tmp_path):
    """Total of 4 steps across two artifacts; 1 has retrieval_gap=True."""
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={
            "compose_retries": 0,
            "step_categorizations": [
                {"step_id": "a", "category": "composed_standard", "retrieval_gap": False},
                {"step_id": "b", "category": "composed_parameterized", "retrieval_gap": True},
            ],
        },
    )
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={
            "compose_retries": 0,
            "step_categorizations": [
                {"step_id": "c", "category": "composed_standard", "retrieval_gap": False},
                {"step_id": "d", "category": "composed_standard", "retrieval_gap": False},
            ],
        },
    )

    report = compute_regression_metrics(session_factory)
    rg = report.retrieval_gap
    assert rg.total_steps == 4
    assert rg.retrieval_gap_steps == 1
    assert rg.rate == pytest.approx(0.25)


def test_since_filter_excludes_old_artifacts(tmp_path):
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    old_ts = datetime.now(UTC) - timedelta(days=30)
    new_ts = datetime.now(UTC)
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={"compose_retries": 1},
        created_at=old_ts,
    )
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={"compose_retries": 0},
        created_at=new_ts,
    )

    cutoff = datetime.now(UTC) - timedelta(days=1)
    # ``cutoff`` is tz-aware; SQLite stores ISO strings — comparison
    # works lexicographically over ISO-with-offset strings only when
    # both have the same offset. Normalize cutoff to naive UTC to
    # match SQLAlchemy's default rendering on SQLite.
    report = compute_regression_metrics(session_factory, since=cutoff.replace(tzinfo=None))
    assert report.total_artifacts == 1
    assert report.compose_retries.artifacts_with_retries == 0


def test_json_output_roundtrips(tmp_path):
    """Ensure ``report_as_json`` produces a string ``json.loads`` can
    parse — i.e., no Python-only types leak through."""
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={
            "compose_retries": 1,
            "runtime_violations": [
                {
                    "rule_id": "framework_violation_unclassified",
                    "failure_class": "load_failed",
                    "exception_type": "ValueError",
                    "exception_message": "x",
                    "recorded_at": "now",
                }
            ],
        },
    )
    report = compute_regression_metrics(session_factory)
    serialized = report_as_json(report)
    parsed = json.loads(serialized)
    assert parsed["total_artifacts"] == 1
    assert parsed["runtime_violations"]["total_violations"] == 1
    # Computed properties (rate, retry_rate) must be present in JSON
    # so external dashboards don't have to recompute them.
    assert "retry_rate" in parsed["compose_retries"]
    assert "rate" in parsed["retrieval_gap"]


def test_table_output_mentions_all_three_signals(tmp_path):
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={"compose_retries": 1},
    )
    report = compute_regression_metrics(session_factory)
    text_out = report.to_table()
    # The three signals MUST each appear in the human-readable
    # output. If a future refactor drops one, operators silently
    # lose the metric.
    assert "Compose retries (C1)" in text_out
    assert "Runtime violations (C2)" in text_out
    assert "Retrieval gap (A2)" in text_out


def test_malformed_runtime_violations_are_skipped(tmp_path):
    """Defense in depth: if a composition_summary somehow contains a
    non-list / non-dict runtime_violations entry, the computation
    must NOT crash. Operators expect monotonic, never-crashing
    metric runs."""
    engine = _migrated_engine(tmp_path)
    session_factory = make_session_factory(engine)
    _insert_artifact(
        engine,
        artifact_id=uuid4(),
        composition_summary={
            "compose_retries": "not-a-number",  # type drift
            "runtime_violations": "not-a-list",
            "step_categorizations": [
                "not a dict",
                {"retrieval_gap": True},
            ],
        },
    )
    report = compute_regression_metrics(session_factory)
    assert report.total_artifacts == 1
    # The non-int "not-a-number" falls into the 0 bucket.
    assert report.compose_retries.distribution == {0: 1}
    assert report.runtime_violations.total_violations == 0
    # Only the real dict is counted; the "not a dict" string is
    # skipped without crashing.
    assert report.retrieval_gap.total_steps == 1
    assert report.retrieval_gap.retrieval_gap_steps == 1
