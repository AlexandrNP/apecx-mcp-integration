"""FastAPI application entrypoint for the Control Plane (Tier 2).

Round 3 status: all routes are registered with final Pydantic schemas. Handlers
currently raise HTTP 501 with a pointer to the implementation_plan.md task that
must land for the handler to do real work. This gives us a stable API surface
and drift-free OpenAPI spec before the DB / composer / HPC layers land.
"""

from __future__ import annotations

from fastapi import FastAPI

from apecx_integration.control_plane.routes import approval, hpc, status, workflow


def create_app() -> FastAPI:
    app = FastAPI(
        title="APECx Control Plane",
        description=(
            "Tier 2 of the APECx integration. Holds run state, provenance, approvals, "
            "artifacts, allocation accountant. See architectural_plan.md §3.1 and §R3."
        ),
        version="0.0.1",
    )

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
