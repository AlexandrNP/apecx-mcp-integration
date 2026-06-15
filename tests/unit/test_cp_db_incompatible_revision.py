"""Regression: a control-plane DB stamped AHEAD of the shipped migrations must
not crash the backend.

Reproduces the 2026-06-15 desktop failure: `~/.apecx-cp/cp.db` was stamped at
revision `0007` (created by a dev branch's migration that never merged), while
the published build ships only up to `0006`. `apecx-cp serve` ran
`alembic upgrade head`, alembic raised `Can't locate revision '0007'`, the
backend died, and the MCP server that autostarts it disconnected — "fails
miserably" with a raw traceback in a /tmp log.

The fix (lifecycle.ensure_infra_ready): detect a DB stamped at a revision unknown
to the bundled scripts and, for SQLite, quarantine it (rename, never delete) so a
fresh schema is built; for non-SQLite, raise an actionable error.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apecx_integration.control_plane.infra.lifecycle import (
    _current_db_revision,
    _quarantine_incompatible_sqlite,
    ensure_infra_ready,
)

HEAD = "0006"  # current bundled head


def _stamp(db_path: Path, revision: str) -> None:
    """Create a SQLite DB whose alembic_version says `revision` (no real schema)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        conn.commit()
    finally:
        conn.close()


def test_db_stamped_ahead_of_code_is_quarantined_and_recreated(tmp_path: Path):
    db = tmp_path / "cp.db"
    _stamp(db, "9999")  # a revision this build does NOT ship (ahead-of-code)
    url = f"sqlite:///{db}"

    # Before the fix this raised alembic CommandError and killed the backend.
    ensure_infra_ready(url)

    # The incompatible DB was moved aside (renamed, not deleted) for forensics.
    quarantined = list(tmp_path.glob("cp.db.incompatible-9999-*"))
    assert len(quarantined) == 1, f"expected one quarantined file, got {quarantined}"
    assert quarantined[0].stat().st_size > 0

    # A fresh DB was created and migrated to the bundled head.
    assert db.exists()
    assert _current_db_revision(url) == HEAD


def test_fresh_db_migrates_normally_without_quarantine(tmp_path: Path):
    db = tmp_path / "cp.db"
    url = f"sqlite:///{db}"
    ensure_infra_ready(url)
    assert _current_db_revision(url) == HEAD
    # No quarantine artifact on the happy path.
    assert not list(tmp_path.glob("*.incompatible-*"))


def test_known_older_revision_upgrades_in_place(tmp_path: Path):
    # A DB at a KNOWN older revision (REAL schema) is a normal upgrade — NOT quarantined.
    from alembic import command

    from apecx_integration.control_plane.infra.lifecycle import (
        _alembic_cfg_for,
        _find_alembic_root,
    )

    db = tmp_path / "cp.db"
    url = f"sqlite:///{db}"
    # Build a genuine schema at an older revision, then upgrade it.
    command.upgrade(_alembic_cfg_for(url, alembic_root=_find_alembic_root()), "0004")
    assert _current_db_revision(url) == "0004"

    ensure_infra_ready(url)  # 0004 is known → upgrade in place to head, no quarantine
    assert _current_db_revision(url) == HEAD
    assert not list(tmp_path.glob("*.incompatible-*"))


def test_non_sqlite_incompatible_db_raises_actionable_not_autodrop():
    # We must NOT auto-modify an operator's Postgres — raise an actionable error.
    with pytest.raises(RuntimeError, match="APECX_CP_DB_URL"):
        _quarantine_incompatible_sqlite("postgresql+psycopg://u:p@localhost:5432/apecx_cp", "9999")
