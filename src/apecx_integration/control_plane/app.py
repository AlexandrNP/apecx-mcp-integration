"""FastAPI application entrypoint for the Control Plane (Tier 2).

TX1 status (post-debt-clearing): the persistence-only routes
(``/approvals/*``, ``/runs/*``) are fully wired to the T09 DB layer.
Composer-dependent routes (``/workflows/start``, ``/workflows/plan``,
``/workflows/diff``) and HPC routes (``/hpc/*``) remain 501 until the
downstream tasks land — their 501 detail messages point at the exact
implementation_plan.md task that unblocks each one.

CLI:
    apecx-cp                 # serve — ensures infra + runs uvicorn
    apecx-cp serve           # same
    apecx-cp teardown        # stops a locally-managed Postgres container
    apecx-cp teardown --remove-data   # also drops the named volume
                                      # (Docker) or bind-mount dir (Apptainer)
"""

from __future__ import annotations

import argparse
import logging
import sys

from fastapi import FastAPI
from sqlalchemy import Engine

from apecx_integration.control_plane.db import get_db_url, make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.routes import approval, hpc, status, workflow


def create_app(engine: Engine | None = None) -> FastAPI:
    """Build the FastAPI app.

    ``engine`` is injected by tests to point at an isolated SQLite file
    (or a containerized Postgres). If not supplied, ``make_engine()``
    resolves ``APECX_CP_DB_URL`` or falls back to the SQLite default.

    This function does NOT touch infrastructure — the CLI ``main()``
    below is responsible for bringing up Postgres and running migrations
    before uvicorn is started. Keeping infra out of ``create_app`` means
    tests (which pass their own engine) do not spin up Docker.
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


def _serve(args: argparse.Namespace) -> int:
    from apecx_integration.control_plane.infra.lifecycle import ensure_infra_ready

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db_url = get_db_url()
    ensure_infra_ready(db_url)

    import uvicorn

    uvicorn.run(
        "apecx_integration.control_plane.app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


def _teardown(args: argparse.Namespace) -> int:
    from apecx_integration.control_plane.infra.lifecycle import teardown_infra

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    teardown_infra(get_db_url(), remove_data=args.remove_data)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apecx-cp")
    subparsers = parser.add_subparsers(dest="cmd")

    serve_p = subparsers.add_parser("serve", help="Run the Control Plane HTTP server.")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.set_defaults(func=_serve)

    teardown_p = subparsers.add_parser(
        "teardown",
        help="Stop the locally-managed Postgres container (no-op for BYO / SQLite).",
    )
    teardown_p.add_argument(
        "--remove-data",
        action="store_true",
        help="Also delete the persistent Postgres data (volume on Docker, "
        "bind-mount dir on Apptainer). DESTRUCTIVE — only for explicit reset.",
    )
    teardown_p.set_defaults(func=_teardown)

    args = parser.parse_args(argv)
    if not args.cmd:
        # Default to serve when invoked bare.
        args = parser.parse_args(["serve"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
