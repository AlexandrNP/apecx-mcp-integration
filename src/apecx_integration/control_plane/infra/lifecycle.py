"""High-level "does whatever the URL needs" entry point.

``ensure_infra_ready(db_url)`` is the one function the ``apecx-cp serve``
CLI calls before ``uvicorn.run``. It:

1. Looks at the URL. SQLite → no container; LOCAL_POSTGRES_MANAGED →
   bring up a container via the detected runtime; REMOTE_POSTGRES_BYO →
   do nothing, trust the operator.
2. Runs ``alembic upgrade head`` idempotently so first-boot creates the
   schema and subsequent boots no-op.

``teardown_infra(db_url, remove_data)`` is the inverse — only meaningful
when the URL is LOCAL_POSTGRES_MANAGED; a no-op otherwise.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from apecx_integration.control_plane.infra.runtime import (
    ContainerRuntime,
    PostgresConfig,
    detect_runtime,
)
from apecx_integration.control_plane.infra.urls import InfraMode, decide_infra_mode

log = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """Where Apptainer / non-named-volume runtimes should persist PG data.

    ``~/.apecx_cp/postgres_data`` is chosen to be under the user's home
    (writable without sudo, persists across reboots). DockerRuntime
    ignores this path because the docker-compose.yml uses a named volume
    — but we still populate the field to keep the config uniform.
    """
    return Path.home() / ".apecx_cp" / "postgres_data"


def _alembic_cfg_for(db_url: str, *, alembic_root: Path) -> Config:
    cfg = Config(str(alembic_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(alembic_root / "migrations"))
    return cfg


def _find_alembic_root() -> Path:
    """Locate the alembic.ini + migrations/ pair.

    Looks in two places, in order:

      1. Bundled inside the installed package at
         ``apecx_integration/_alembic/``. This is the path that
         works in any install mode (uv tool / pipx / pip --user /
         editable) because the lookup is relative to this module's
         file location, not to the cwd or repo root.
      2. The repo root (parent walk from this file). This is the
         legacy editable-install path; preserved so existing tests
         that use ``REPO_ROOT / "alembic.ini"`` keep working.

    The two copies (in-package vs repo-root) are kept byte-equivalent
    by a regression test (``tests/integration/test_alembic_bundled_in_package.py``).
    The in-package copy is the one that ships with the wheel.
    """
    # 1. In-package copy. ``Path(__file__).parents[2]`` is
    # ``apecx_integration/`` regardless of install mode.
    in_package = Path(__file__).resolve().parents[2] / "_alembic"
    if (in_package / "alembic.ini").is_file():
        return in_package
    # 2. Legacy repo-root walk.
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "alembic.ini").is_file():
            return parent
    raise FileNotFoundError(
        "alembic.ini not found in the in-package _alembic/ directory "
        "AND no parent of infra/lifecycle.py contains alembic.ini. "
        "This indicates a broken installation — reinstall via "
        "``pip install ...`` or ``uv tool install ...`` and verify "
        "the package was built with the bundled migrations."
    )


def _current_db_revision(db_url: str) -> str | None:
    """Revision the DB is stamped at, or ``None`` for a fresh / un-stamped DB."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def _revision_known_to_scripts(cfg: Config, revision: str) -> bool:
    """Is ``revision`` present in our bundled migration scripts?

    Uses ``walk_revisions`` (which never raises on an unknown id) rather than
    ``get_revision`` (which raises alembic ``CommandError`` for an unknown
    revision — the very case we are probing for).
    """
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    return revision in {rev.revision for rev in script.walk_revisions()}


def _quarantine_incompatible_sqlite(db_url: str, revision: str) -> None:
    """Move an incompatible SQLite DB aside so a fresh one can be created.

    Triggered when the DB is stamped at a revision this build does NOT ship
    (e.g. created by a newer / dev version — a downgrade). The control-plane DB
    holds operational run/approval state (regenerable), NOT precious
    user-authored data — but we RENAME (never delete) so the old file is kept
    for forensics. Raises for non-SQLite URLs: we must not auto-drop an
    operator's Postgres.
    """
    import time

    from sqlalchemy.engine import make_url

    url = make_url(db_url)
    if not url.drivername.startswith("sqlite"):
        raise RuntimeError(
            f"Control-plane DB at {db_url} is stamped at revision {revision!r}, which this "
            f"build does not recognize (it ships migrations only up to its bundled head). The "
            f"DB was likely created by a NEWER/dev build. This build will NOT auto-modify a "
            f"non-SQLite database — back it up and point APECX_CP_DB_URL at a fresh database "
            f"(or downgrade the DB schema), then restart."
        )
    db_path = Path(url.database) if url.database else None
    if db_path is None or not db_path.exists():
        return  # nothing to move (already fresh)
    backup = db_path.with_name(f"{db_path.name}.incompatible-{revision}-{int(time.time())}")
    db_path.rename(backup)
    log.warning("=" * 64)
    log.warning(
        "Control-plane DB %s was stamped at revision %r, UNKNOWN to this build (likely "
        "created by a newer/dev version). Moved it aside to %s and recreating a fresh "
        "database so the backend can start. The old operational state is preserved in "
        "that file.",
        db_path,
        revision,
        backup.name,
    )
    log.warning("=" * 64)


def ensure_infra_ready(
    db_url: str,
    *,
    runtime: ContainerRuntime | None = None,
    data_dir: Path | None = None,
) -> None:
    decision = decide_infra_mode(db_url)
    log.info("infra decision for %s: %s", db_url, decision.mode.value)

    if decision.mode is InfraMode.LOCAL_POSTGRES_MANAGED:
        resolved_runtime = runtime or detect_runtime()
        config = PostgresConfig(data_dir=str(data_dir or default_data_dir()))
        log.info("bringing up Postgres via %s", resolved_runtime.kind.value)
        resolved_runtime.ensure_postgres_running(config)

    # SQLite and BYO both proceed directly to migrations. SQLite will
    # create the file on first connect; BYO must already have a
    # reachable server — alembic upgrade head fails loudly if it doesn't.
    log.info("running alembic upgrade head against %s", db_url)
    cfg = _alembic_cfg_for(db_url, alembic_root=_find_alembic_root())
    # Resilience (2026-06-15): if the DB is stamped at a revision this build does
    # NOT ship (e.g. created by a newer/dev version → alembic "Can't locate
    # revision"), a raw upgrade crash takes down the whole backend + the MCP
    # server that autostarts it (observed on a desktop install with a 0007-stamped
    # cp.db against a 0006-head build). Detect the unknown stamp and quarantine the
    # incompatible SQLite DB (rename, never delete) so a fresh schema can be built;
    # for non-SQLite, raise an actionable error rather than touch the operator's DB.
    current = _current_db_revision(db_url)
    if current is not None and not _revision_known_to_scripts(cfg, current):
        _quarantine_incompatible_sqlite(db_url, current)
    command.upgrade(cfg, "head")


def teardown_infra(
    db_url: str,
    *,
    remove_data: bool = False,
    runtime: ContainerRuntime | None = None,
    data_dir: Path | None = None,
) -> None:
    decision = decide_infra_mode(db_url)
    if decision.mode is not InfraMode.LOCAL_POSTGRES_MANAGED:
        log.info(
            "teardown skipped: %s does not correspond to a managed container (mode=%s)",
            db_url,
            decision.mode.value,
        )
        return
    resolved_runtime = runtime or detect_runtime()
    config = PostgresConfig(data_dir=str(data_dir or default_data_dir()))
    resolved_runtime.teardown(config, remove_data=remove_data)
