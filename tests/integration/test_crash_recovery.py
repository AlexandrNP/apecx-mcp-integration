"""T09 AC4: atomicity under crash.

Spawn a subprocess that writes to a real SQLite DB. Kill it with SIGKILL
at two points — before COMMIT and after COMMIT — and assert the database
is either fully clean (row absent) or fully consistent (row present).

This is a real crash test, not a simulation: we fork, the child opens its
own engine, performs DDL, and is terminated by SIGKILL (cannot be caught,
cannot flush). The parent then opens a fresh connection and inspects.

Marked ``slow`` because each case sleeps briefly to give SQLite's WAL the
chance to flush (or not) before the kill.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from apecx_integration.control_plane.db import make_engine
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]


def _seed_schema(db_url: str) -> None:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")


def _child_insert_uncommitted(db_url: str, run_id: str, ready_event) -> None:
    """Open tx, INSERT a run row, signal ready, then sleep forever.

    Parent will SIGKILL this process before the ``sleep`` returns, so the
    INSERT is never committed.
    """
    engine = make_engine(db_url)
    now = datetime.now(UTC).isoformat()
    conn = engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "INSERT INTO run (id, user_id, status, created_at) VALUES (?, ?, ?, ?)",
            (run_id, "child", "PENDING", now),
        )
        ready_event.set()
        time.sleep(60)  # will be SIGKILL'd before this returns
    finally:
        conn.close()


def _child_insert_and_commit(db_url: str, run_id: str, ready_event) -> None:
    """Open tx, INSERT, COMMIT, signal ready, sleep forever."""
    engine = make_engine(db_url)
    now = datetime.now(UTC).isoformat()
    conn = engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "INSERT INTO run (id, user_id, status, created_at) VALUES (?, ?, ?, ?)",
            (run_id, "child", "PENDING", now),
        )
        cur.execute("COMMIT")
        ready_event.set()
        time.sleep(60)
    finally:
        conn.close()


def _kill_child(proc: mp.Process, ready_event) -> None:
    assert ready_event.wait(timeout=5.0), "child never signaled ready"
    assert proc.pid is not None
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=3.0)
    assert not proc.is_alive(), "child survived SIGKILL (shouldn't be possible)"


def _row_exists(db_url: str, run_id: UUID) -> bool:
    engine = make_engine(db_url)
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT id FROM run WHERE id = :rid"), {"rid": str(run_id)}
        ).scalar()
    return found is not None


@pytest.mark.integration
@pytest.mark.slow
def test_sigkill_before_commit_leaves_no_row(tmp_path: Path) -> None:
    db_file = tmp_path / "cp.db"
    db_url = f"sqlite:///{db_file}"
    _seed_schema(db_url)

    run_id = uuid4()
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(
        target=_child_insert_uncommitted,
        args=(db_url, str(run_id), ready),
        daemon=True,
    )
    proc.start()

    _kill_child(proc, ready)

    assert not _row_exists(db_url, run_id), "uncommitted INSERT survived SIGKILL — atomicity broken"


@pytest.mark.integration
@pytest.mark.slow
def test_sigkill_after_commit_preserves_row(tmp_path: Path) -> None:
    db_file = tmp_path / "cp.db"
    db_url = f"sqlite:///{db_file}"
    _seed_schema(db_url)

    run_id = uuid4()
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(
        target=_child_insert_and_commit,
        args=(db_url, str(run_id), ready),
        daemon=True,
    )
    proc.start()

    _kill_child(proc, ready)

    assert _row_exists(
        db_url, run_id
    ), "committed INSERT was lost after SIGKILL — durability broken"
