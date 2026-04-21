"""Decide from a DB URL whether the app should self-provision infra.

The docker-compose.yml at the repo root exposes Postgres on
``localhost:5433``. Any DB URL that points at a loopback address on
that port (plus the expected user/db) is treated as "the local
managed Postgres" — the app will bring it up on startup, tear it
down on ``apecx-cp teardown``, and otherwise leave it alone.

Any other Postgres URL (non-localhost host, or different port) is
"bring your own" — the app assumes someone upstream manages it.
SQLite URLs never need infra management.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

LOCAL_POSTGRES_PORT = 5433
LOCAL_POSTGRES_DB = "apecx_cp"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class InfraMode(Enum):
    """What the lifespan hook should do with this URL at startup."""

    SQLITE_NO_INFRA = "sqlite_no_infra"
    LOCAL_POSTGRES_MANAGED = "local_postgres_managed"
    REMOTE_POSTGRES_BYO = "remote_postgres_byo"


@dataclass(frozen=True, kw_only=True)
class InfraDecision:
    mode: InfraMode
    # Only populated for LOCAL_POSTGRES_MANAGED; None otherwise.
    local_port: int | None = None
    reason: str


def decide_infra_mode(db_url: str) -> InfraDecision:
    """Classify a DB URL into an :class:`InfraMode`.

    Not a validator — only the shape relevant for "do we manage this?"
    is inspected. A malformed URL that happens to parse cleanly still
    fails loudly when SQLAlchemy tries to open a connection; we don't
    duplicate that validation here.
    """
    if db_url.startswith("sqlite"):
        return InfraDecision(
            mode=InfraMode.SQLITE_NO_INFRA,
            reason="SQLite needs no container; file is created on first connect.",
        )

    parsed = urlparse(db_url)
    # SQLAlchemy URLs use the dialect in the scheme, e.g. postgresql+psycopg.
    if not parsed.scheme.startswith(("postgresql", "postgres")):
        return InfraDecision(
            mode=InfraMode.REMOTE_POSTGRES_BYO,
            reason=(
                f"URL scheme {parsed.scheme!r} is neither sqlite nor postgres — "
                "treating as opaque/BYO and not managing infra."
            ),
        )

    host = (parsed.hostname or "").lower()
    port = parsed.port

    if host not in LOOPBACK_HOSTS:
        return InfraDecision(
            mode=InfraMode.REMOTE_POSTGRES_BYO,
            reason=f"Postgres host {host!r} is not loopback — assuming BYO.",
        )
    if port != LOCAL_POSTGRES_PORT:
        return InfraDecision(
            mode=InfraMode.REMOTE_POSTGRES_BYO,
            reason=(
                f"Postgres port {port} is not the managed port "
                f"{LOCAL_POSTGRES_PORT} — assuming BYO."
            ),
        )
    return InfraDecision(
        mode=InfraMode.LOCAL_POSTGRES_MANAGED,
        local_port=port,
        reason="Loopback Postgres on the managed port; app will self-provision.",
    )
