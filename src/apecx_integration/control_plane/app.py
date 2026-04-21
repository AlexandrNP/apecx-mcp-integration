"""FastAPI application entrypoint for the Control Plane (Tier 2).

TX1 status (post-debt-clearing): the persistence-only routes
(``/approvals/*``, ``/runs/*``) are fully wired to the T09 DB layer.
Composer-dependent routes (``/workflows/start``, ``/workflows/plan``,
``/workflows/diff``) and HPC routes (``/hpc/*``) remain 501 until the
downstream tasks land — their 501 detail messages point at the exact
implementation_plan.md task that unblocks each one.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import Engine

from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.routes import approval, hpc, status, workflow


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build the FastAPI app.

    ``engine`` is injected by tests to point at an isolated SQLite file
    (or a containerized Postgres). If not supplied, ``make_engine()``
    resolves ``APECX_CP_DB_URL`` or falls back to the SQLite default.
    """
    resolved_engine = engine or make_engine()
    session_factory = make_session_factory(resolved_engine)

    app = FastAPI(
        title="APECx Control Plane",
        description=(
            "Tier 2 of the APECx integration. Holds run state, provenance, approvals, "
            "artifacts, allocation accountant. See architectural_plan.md §3.1 and §R3."
        ),
        version="0.0.1",
    )
    app.state.engine = resolved_engine
    app.state.session_factory = session_factory
    app.state.recorder = ProvenanceRecorder(session_factory)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "phase": "scaffold"}

    app.include_router(workflow.router)
    app.include_router(approval.router)
    app.include_router(status.router)
    app.include_router(hpc.router)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "apecx_integration.control_plane.app:app", host="127.0.0.1", port=8000, reload=False
    )
