"""T09 AC1 + AC2: Alembic migration round-trip against a real SQLite file.

Hits a real SQLite file on disk via the Alembic runtime. No mocks. This is
an integration test against a real DB engine, per the workspace mocks
carve-out.

AC7 (Postgres parity) is covered by a sibling test (added in a later T09
commit) that runs the same migration against a testcontainers-Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "alembic_version",
    "allocation_estimate",
    "approval",
    "artifact",
    "component",
    "generated_artifact",
    "provenance_event",
    "run",
    "step",
    "verified_synonym",
}


def _alembic_cfg(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


@pytest.mark.integration
def test_upgrade_head_creates_all_tables(tmp_path: Path) -> None:
    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    command.upgrade(_alembic_cfg(url), "head")

    inspector = inspect(create_engine(url))
    tables = set(inspector.get_table_names())
    assert tables == EXPECTED_TABLES


@pytest.mark.integration
def test_downgrade_then_upgrade_round_trips(tmp_path: Path) -> None:
    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    cfg = _alembic_cfg(url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

    engine = create_engine(url)
    tables_after_down = set(inspect(engine).get_table_names())
    assert tables_after_down == {
        "alembic_version"
    }, "downgrade should leave only the alembic bookkeeping table"

    command.upgrade(cfg, "head")
    tables_after_up = set(inspect(engine).get_table_names())
    assert tables_after_up == EXPECTED_TABLES


@pytest.mark.integration
def test_circular_fk_present_after_upgrade(tmp_path: Path) -> None:
    """run.workflow_config_id -> artifact.id must be a real FK, not silently dropped
    by the use_alter/batch-rebuild handling.
    """
    db_file = tmp_path / "cp.db"
    url = f"sqlite:///{db_file}"
    command.upgrade(_alembic_cfg(url), "head")

    inspector = inspect(create_engine(url))
    run_fks = inspector.get_foreign_keys("run")
    wf_config_fk = next(
        (fk for fk in run_fks if fk["constrained_columns"] == ["workflow_config_id"]),
        None,
    )
    assert wf_config_fk is not None, "circular FK to artifact.id was dropped"
    assert wf_config_fk["referred_table"] == "artifact"
    assert wf_config_fk["referred_columns"] == ["id"]
