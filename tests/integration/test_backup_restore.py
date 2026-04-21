"""T09 AC5: backup and restore preserves all rows.

Creates a real migrated SQLite DB, inserts known rows, runs
scripts/backup_state.sh, deletes the original, runs
scripts/restore_test.sh, verifies row counts match.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from apecx_integration.control_plane.db import make_engine
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _seed_schema_and_rows(db_url: str, n_runs: int) -> list[str]:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")

    engine = make_engine(db_url)
    ids: list[str] = []
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        for _ in range(n_runs):
            rid = str(uuid4())
            ids.append(rid)
            conn.execute(
                text(
                    "INSERT INTO run (id, user_id, status, created_at) "
                    "VALUES (:id, :uid, 'PENDING', :ts)"
                ),
                {"id": rid, "uid": "tester", "ts": now},
            )
    return ids


@pytest.mark.integration
def test_backup_and_restore_roundtrip_sqlite(tmp_path: Path) -> None:
    src_db = tmp_path / "cp.db"
    backup = tmp_path / "cp.bak"
    restored_db = tmp_path / "restored.db"
    src_url = f"sqlite:///{src_db}"

    ids = _seed_schema_and_rows(src_url, n_runs=7)

    env = {**os.environ, "APECX_CP_DB_URL": src_url}
    subprocess.run(
        ["bash", str(SCRIPTS / "backup_state.sh"), str(backup)],
        check=True,
        env=env,
        capture_output=True,
    )
    assert backup.exists() and backup.stat().st_size > 0

    src_db.unlink()
    assert not src_db.exists()

    result = subprocess.run(
        ["bash", str(SCRIPTS / "restore_test.sh"), str(backup), str(restored_db)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "run rows = 7" in result.stdout, result.stdout

    restored_engine = make_engine(f"sqlite:///{restored_db}")
    with restored_engine.connect() as conn:
        restored_ids = {row[0] for row in conn.execute(text("SELECT id FROM run")).all()}
    assert restored_ids == set(ids)


@pytest.mark.integration
def test_backup_script_fails_clearly_on_missing_db(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope.db"
    backup_out = tmp_path / "cp.bak"
    env = {**os.environ, "APECX_CP_DB_URL": f"sqlite:///{nonexistent}"}
    res = subprocess.run(
        ["bash", str(SCRIPTS / "backup_state.sh"), str(backup_out)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "not found" in res.stderr.lower()


@pytest.mark.integration
def test_restore_script_rejects_unknown_format(tmp_path: Path) -> None:
    junk = tmp_path / "junk.bak"
    junk.write_text("this is not a database backup at all")
    res = subprocess.run(
        ["bash", str(SCRIPTS / "restore_test.sh"), str(junk), str(tmp_path / "x.db")],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "unknown backup format" in res.stderr.lower()
