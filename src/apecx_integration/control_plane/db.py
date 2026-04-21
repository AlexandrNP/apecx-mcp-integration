"""Control Plane SQLAlchemy engine + session factory (T09).

SQLite is the laptop default. Postgres is the parity target (T09 AC7).

SQLite WAL mode is enabled on every new connection via an event listener.
Rationale (AC6): concurrent readers do not block writers and vice versa.
Under the default journal mode (DELETE) a writer holds a reserved lock that
blocks any reader until commit — unacceptable for a Control Plane that
serves HTTP concurrent to workflow steps writing provenance.

``synchronous=NORMAL`` pairs with WAL: it skips the fsync-on-every-commit
and relies on WAL checkpoint fsyncs instead. Durability is still atomic at
the transaction level; only a power loss between WAL commit and checkpoint
can lose the most recent transactions, which is acceptable for a laptop
workflow tool and matches what SQLite's own docs recommend for WAL.
"""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def get_db_url() -> str:
    """Resolve the Control Plane DB URL.

    Env var ``APECX_CP_DB_URL`` takes precedence. Default is an on-disk
    SQLite file at ``./apecx_cp.db`` (CWD-relative; fine for tests and
    laptop use, set the env var in production).
    """
    return os.environ.get("APECX_CP_DB_URL", "sqlite:///./apecx_cp.db")


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Create an engine with SQLite WAL mode wired in on connect.

    SQLite / FastAPI threadpool note: ``check_same_thread=False`` lets a
    pooled connection cross Python-thread boundaries, which FastAPI's sync
    handler dispatch (anyio threadpool) requires. SQLAlchemy's engine-level
    connection pool is still the gate that prevents two threads from
    simultaneously executing on the same DBAPI connection: as long as each
    unit of work acquires a connection/session via the factory and releases
    it before yielding back, there is no thread-sharing of mid-flight
    transaction state. What this does NOT protect against:
      * holding a Session across an await that yields to another request;
      * spawning a worker thread from inside a handler that reuses the
        handler's Session.
    Both are bugs we will catch under load, not at compile time; if the
    Control Plane ever gets real concurrent traffic, add a Session-scope
    stress test before assuming this still holds.
    """
    resolved = url or get_db_url()
    engine = create_engine(
        resolved,
        echo=echo,
        connect_args={"check_same_thread": False} if resolved.startswith("sqlite") else {},
        future=True,
    )
    if engine.dialect.name == "sqlite":
        _install_sqlite_pragmas(engine)
    return engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    """Enable WAL journal mode and NORMAL sync on every new SQLite connection."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        # PRAGMA foreign_keys must be set per-connection on SQLite; it is
        # off by default and the declarative FK constraints are otherwise
        # silently ignored at runtime.
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
