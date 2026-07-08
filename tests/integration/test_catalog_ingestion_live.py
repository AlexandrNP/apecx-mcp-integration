"""Live integration test for the rhea catalog-ingestion autodeploy.

Proves ``InfraOrchestrator.ensure_catalog_seeded`` against the real
orchestrator-spawned backends: it detects an EMPTY rhea tool catalog (postgres
``galaxytools`` row count 0) and runs the ingestion INSIDE the running
``apecx-rhea-server`` container, so rhea works after nothing but ``uv install`` +
``apecx-setup`` (no manual ``apecx-setup rhea`` ingestion step).

This is the recorded artifact behind the design's detection fix: the MCP
``tools/list`` count always reports ``find_tools`` (1) whether the catalog is empty
or seeded, so detection must use the postgres row count — a fact only a live
truncate-then-ingest exercises (the unit tests monkeypatch ``_catalog_row_count``).

DESTRUCTIVE-but-self-healing: it TRUNCATEs ``galaxytools`` then re-seeds it via the
idempotent ``RHEA_INGEST_ONLY=muscle`` ingest (~10s), leaving the catalog as it found
it (muscle present). Gated on the live stack — postgres :5435, the rhea-server MCP
:3001, and Ollama :11434 (the ingestion's embedding backend). Auto-skips when any is
down.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess

import pytest

pytestmark = pytest.mark.integration

_PG_CONTAINER = "apecx-rhea-postgres"


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_DOCKER = shutil.which("docker")
_PG_UP = _port_open("localhost", 5435)
_RHEA_UP = _port_open("localhost", 3001)
_OLLAMA_UP = _port_open("localhost", 11434)

_skip = pytest.mark.skipif(
    not (_DOCKER and _PG_UP and _RHEA_UP and _OLLAMA_UP),
    reason=(
        "live stack required: docker + postgres :5435 + rhea-server :3001 + "
        "Ollama :11434 (the ingest embedding backend). Start via `apecx-mcp` / the "
        "orchestrator; pull mxbai-embed-large."
    ),
)


def _catalog_rows() -> int:
    """Row count of galaxytools via psql in the postgres container (-1 if the query errors)."""
    res = subprocess.run(
        [
            _DOCKER,
            "exec",
            _PG_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "rhea",
            "-tAc",
            "SELECT COUNT(*) FROM galaxytools;",
        ],
        capture_output=True,
        timeout=15,
    )
    out = res.stdout.decode("utf-8", "replace").strip()
    try:
        return int(out)
    except ValueError:
        return -1


@_skip
def test_orchestrator_auto_ingests_an_empty_catalog():
    """TRUNCATE galaxytools → ensure_catalog_seeded() → catalog re-seeded (0 → >0).

    This is the exact code path apecx-mcp startup takes (start_all → THIS →
    prewarm). Asserts the row-count detection fires (not the always-1 tools/list),
    the ingest runs, and a second call short-circuits (idempotent already_seeded).
    """
    from apecx_integration.infrastructure.orchestrator import (
        InfraOrchestrator,
        reset_orchestrator_for_testing,
    )

    reset_orchestrator_for_testing()

    # Unseed: truncate the catalog so ensure_catalog_seeded must re-ingest.
    subprocess.run(
        [
            _DOCKER,
            "exec",
            _PG_CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "rhea",
            "-c",
            "TRUNCATE galaxytools;",
        ],
        capture_output=True,
        timeout=15,
        check=True,
    )
    assert _catalog_rows() == 0, "TRUNCATE did not empty galaxytools"

    async def _drive():
        orch = InfraOrchestrator()
        first = await orch.ensure_catalog_seeded(timeout_s=300)
        second = await orch.ensure_catalog_seeded(timeout_s=300)
        return first, second

    first, second = asyncio.run(_drive())

    # First call detected the empty catalog (via ROW COUNT) and ingested.
    assert first["action"] == "ingested", f"expected ingested, got {first!r}"
    assert first["seeded"] is True, f"catalog not seeded after ingest: {first!r}"
    assert _catalog_rows() > 0, "galaxytools still empty after auto-ingest"

    # Second call is idempotent: catalog now has rows → short-circuit, no re-ingest.
    assert second["action"] == "already_seeded", (
        f"second call should short-circuit on a seeded catalog, got {second!r}"
    )

    reset_orchestrator_for_testing()
