"""T09 AC6: SQLite WAL mode on, concurrent reads do not block writes.

Integration test against a real on-disk SQLite file. No mocks.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from sqlalchemy import text

from apecx_integration.control_plane.db import make_engine


@pytest.mark.integration
def test_wal_mode_on_after_connect(tmp_path: Path) -> None:
    db_file = tmp_path / "cp.db"
    engine = make_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode == "wal", f"expected WAL, got {mode!r}"


@pytest.mark.integration
def test_foreign_keys_enforced_per_connection(tmp_path: Path) -> None:
    """PRAGMA foreign_keys must be ON so the circular-FK from migration
    0001 is actually enforced at runtime — not just declared.
    """
    db_file = tmp_path / "cp.db"
    engine = make_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        fk_on = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert fk_on == 1


@pytest.mark.integration
def test_reader_not_blocked_by_writer(tmp_path: Path) -> None:
    """WAL mode contract: an open writer transaction must not prevent a
    concurrent reader on a different connection from completing a SELECT
    within a reasonable time.

    Under the default DELETE journal mode this test would hang (or error
    on busy-timeout) because the writer holds a RESERVED/PENDING lock that
    blocks the reader.
    """
    db_file = tmp_path / "cp.db"
    engine = make_engine(f"sqlite:///{db_file}")

    # Seed a table and one row so the reader has something non-trivial to see.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)"))
        conn.execute(text("INSERT INTO kv (k, v) VALUES ('seed', 'value')"))

    writer_ready = threading.Event()
    writer_release = threading.Event()
    reader_value: list[str | None] = []
    reader_error: list[BaseException] = []

    def writer() -> None:
        # Hold an explicit write transaction open until the reader has
        # finished. Use raw sqlite3 to BEGIN IMMEDIATE so the writer lock
        # is taken right away (SQLAlchemy's autocommit-begin-deferred would
        # not grab the write lock until the first write).
        conn = sqlite3.connect(db_file, isolation_level=None, timeout=1.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO kv (k, v) VALUES ('writer_key', 'writer_val')")
            writer_ready.set()
            writer_release.wait(timeout=5.0)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def reader() -> None:
        try:
            writer_ready.wait(timeout=5.0)
            with engine.connect() as conn:
                # Short busy timeout: under DELETE mode this would wake as
                # SQLITE_BUSY almost immediately; under WAL it just returns.
                conn.execute(text("PRAGMA busy_timeout = 500"))
                result = conn.execute(text("SELECT v FROM kv WHERE k = 'seed'")).scalar()
                reader_value.append(result)
        except BaseException as exc:  # noqa: BLE001
            reader_error.append(exc)

    t_writer = threading.Thread(target=writer)
    t_reader = threading.Thread(target=reader)
    t_writer.start()
    t_reader.start()

    # Reader should finish quickly despite writer holding the tx open.
    t_reader.join(timeout=3.0)
    assert not t_reader.is_alive(), "reader blocked on writer — WAL not in effect?"
    assert not reader_error, f"reader errored: {reader_error}"
    assert reader_value == ["value"]

    writer_release.set()
    t_writer.join(timeout=3.0)
    assert not t_writer.is_alive()

    # After writer commits, the row is visible.
    with engine.connect() as conn:
        seen = conn.execute(
            text("SELECT v FROM kv WHERE k = 'writer_key'")
        ).scalar()
    assert seen == "writer_val"

    # Sanity: the raw sqlite3 connection is subject to the same journal
    # mode (WAL is a file-level attribute). Read it via raw sqlite3 to
    # avoid any SQLAlchemy pragma-caching oddity.
    raw = sqlite3.connect(db_file)
    try:
        mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        raw.close()
    assert mode == "wal"
