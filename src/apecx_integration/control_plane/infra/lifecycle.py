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


def _alembic_cfg_for(db_url: str, *, repo_root: Path) -> Config:
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    return cfg


def _find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "alembic.ini").is_file():
            return parent
    raise FileNotFoundError(
        "alembic.ini not found in any parent of infra/lifecycle.py; "
        "cannot run migrations from outside the repo."
    )


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
    cfg = _alembic_cfg_for(db_url, repo_root=_find_repo_root())
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
            "teardown skipped: %s does not correspond to a managed container "
            "(mode=%s)", db_url, decision.mode.value,
        )
        return
    resolved_runtime = runtime or detect_runtime()
    config = PostgresConfig(data_dir=str(data_dir or default_data_dir()))
    resolved_runtime.teardown(config, remove_data=remove_data)
