"""Startup-time infrastructure orchestrator for ``apecx-mcp``.

This subpackage brings up (and probes for liveness) the five backends
``apecx-mcp`` depends on:

* Postgres (``apecx-rhea-postgres``) — pgvector vector store
* Redis (``apecx-redis``) — task queue + caches
* MinIO (``apecx-rhea-minio``) — S3-compatible object store
* Ollama — local LLM endpoint (operator-installed host process)
* Rhea MCP — MCP server backing the structural-bioinformatics tools
  (Docker container, built by ``apecx-setup rhea``)

The orchestrator is **operational plumbing**, not a nanobrain workflow
component. It is launched as a fire-and-forget asyncio task from
``apecx_integration.mcp_surface.server.build_server`` and exposes
its state via the ``infrastructure_status`` MCP tool. Backends that
are already up when ``start_all()`` runs are *reused* (not respawned);
the ``atexit`` teardown tears down ONLY containers / processes this
orchestrator spawned.

See ``docs/apecx_mcp_infrastructure.md`` for the operator-facing
reference.
"""

from apecx_integration.infrastructure.backends import (
    BackendSpec,
    BackendState,
    ContainerSpec,
    ProbeResult,
)
from apecx_integration.infrastructure.containers import (
    APECX_REDIS,
    APECX_RHEA_MINIO,
    APECX_RHEA_POSTGRES,
    all_container_specs,
)
from apecx_integration.infrastructure.orchestrator import (
    InfraOrchestrator,
    get_orchestrator,
    reset_orchestrator_for_testing,
)

__all__ = [
    "APECX_REDIS",
    "APECX_RHEA_MINIO",
    "APECX_RHEA_POSTGRES",
    "BackendSpec",
    "BackendState",
    "ContainerSpec",
    "InfraOrchestrator",
    "ProbeResult",
    "all_container_specs",
    "get_orchestrator",
    "reset_orchestrator_for_testing",
]
