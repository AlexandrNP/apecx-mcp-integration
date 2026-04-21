"""FastAPI application entrypoint for the Control Plane (Tier 2).

Round 3 status: skeleton only. All routes are stubs that raise NotImplementedError
with actionable messages. The real implementations land in Phase 1 (T09 durable
state) and Phase 2 (composer, artifact store, diff routes).
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(
        title="APECx Control Plane",
        description=(
            "Tier 2 of the APECx integration. Holds run state, provenance, approvals, "
            "artifacts, allocation accountant. See architectural_plan.md §3.1 and §R3."
        ),
        version="0.0.1",
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "phase": "scaffold"}

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("apecx_integration.control_plane.app:app", host="127.0.0.1", port=8000, reload=False)
