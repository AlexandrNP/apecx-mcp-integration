"""FastAPI application entrypoint for the Control Plane (Tier 2).

Route surface (as of 2026-04-22):

    /healthz                — always-on
    /workflows/start        — T01 P1 (composer + policy gated, 503 if unwired)
    /workflows/plan         — preview-mode composition
    /workflows/diff         — T06 categorization + novel Python
    /workflows/execute      — T01 P2 LocalExecutor (503 if unwired)
    /approvals/*            — TX1 HITL lifecycle
    /runs/*, verified-synonyms, /metrics/*
    /hpc/estimate           — T07
    /hpc/confirm            — T07 allocation confirmation
    /hpc/export             — T05 PBS bundle generator
    /hpc/ingest             — T05 AC3 bundle reconciliation
    /hpc/submit             — **still 501**, blocked on T04/T05 executor-runtime

Composer + approval-policy + local-executor are injected at app
construction time (``create_app(engine, composer=..., approval_policy=...,
local_executor=...)``). When unset, the corresponding routes surface 503
with a specific "X is not configured" detail so operators can trace.

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
from apecx_integration.control_plane.routes import (
    approval,
    hpc,
    metrics,
    status,
    verified_synonyms,
    workflow,
)


def create_app(
    engine: Engine | None = None,
    *,
    composer=None,
    approval_policy=None,
    local_executor=None,
) -> FastAPI:
    """Build the FastAPI app.

    ``engine`` is injected by tests to point at an isolated SQLite file
    (or a containerized Postgres). If not supplied, ``make_engine()``
    resolves ``APECX_CP_DB_URL`` or falls back to the SQLite default.

    ``composer`` and ``approval_policy`` (T01) are optional. When None,
    the ``/workflows/start`` route raises 503 with a pointer at how to
    configure them; tests that don't exercise that route don't need to
    pass either. Production deployments build them from env vars in the
    CLI entrypoint (``_serve``).

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
    app.state.composer = composer
    app.state.approval_policy = approval_policy
    app.state.local_executor = local_executor

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "phase": "scaffold"}

    app.include_router(workflow.router)
    app.include_router(approval.router)
    app.include_router(status.router)
    app.include_router(hpc.router)
    app.include_router(metrics.router)
    app.include_router(verified_synonyms.router)

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
    from apecx_integration.control_plane.infra.apptainer_runtime import (
        ApptainerRuntime,
        _managed_data_path,
    )
    from apecx_integration.control_plane.infra.lifecycle import (
        default_data_dir,
        teardown_infra,
    )
    from apecx_integration.control_plane.infra.runtime import (
        PostgresConfig,
        detect_runtime,
    )
    from apecx_integration.control_plane.infra.urls import InfraMode, decide_infra_mode

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    db_url = get_db_url()
    if args.remove_data and not args.yes:
        decision = decide_infra_mode(db_url)
        if decision.mode is InfraMode.LOCAL_POSTGRES_MANAGED:
            # Resolve the exact path / volume that would be destroyed so
            # the user sees what they're agreeing to.
            runtime = detect_runtime()
            cfg = PostgresConfig(data_dir=str(default_data_dir()))
            if isinstance(runtime, ApptainerRuntime):
                target = (
                    f"rm -rf on {_managed_data_path(cfg)!r} (managed Apptainer "
                    "bind-mount subdirectory)"
                )
            else:
                # Docker: we drop the named volume, not a host path.
                target = (
                    "docker volume rm apecx_cp_postgres_data (all Postgres " "state; unrecoverable)"
                )
            print(f"--remove-data is DESTRUCTIVE. It will run:\n  {target}")
            answer = input("Proceed? [type 'yes' to confirm]: ").strip().lower()
            if answer != "yes":
                print("Aborted.")
                return 1

    teardown_infra(db_url, remove_data=args.remove_data)
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
        help="Also delete the persistent Postgres data (named volume on "
        "Docker, managed bind-mount subdir on Apptainer). DESTRUCTIVE — "
        "only for explicit reset. Prompts before running unless --yes is "
        "also passed.",
    )
    teardown_p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the interactive confirmation prompt for --remove-data. "
        "Intended for scripts / CI.",
    )
    teardown_p.set_defaults(func=_teardown)

    args = parser.parse_args(argv)
    if not args.cmd:
        # Default to serve when invoked bare.
        args = parser.parse_args(["serve"])
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
