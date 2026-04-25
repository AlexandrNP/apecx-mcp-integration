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
import os
import sys
from pathlib import Path

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

log = logging.getLogger(__name__)

# Repo root resolution for default config paths used by ``_serve``.
# This file lives at ``<repo>/src/apecx_integration/control_plane/app.py``,
# so parents[3] is the repo root in editable-install layouts. The CLI
# defaults only need to work for the editable-install case (the
# tutorial / development path); wheel deployments override every
# default via env vars.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_COMPOSER_CONFIG = (
    _REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
)
_DEFAULT_APPROVAL_POLICY = _REPO_ROOT / "configs" / "approval_policy.yml"
_DEFAULT_WORKFLOW_BASE_DIR = (
    _REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "violin_bvbrc"
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


def _build_components_from_env(engine: Engine) -> tuple:
    """Build composer + approval policy + local executor from env vars.

    The 503 error messages on ``/workflows/start`` etc. promise that
    setting ``APECX_COMPOSER_CONFIG_PATH`` / ``APECX_APPROVAL_POLICY_PATH``
    / ``APECX_WORKFLOW_BASE_DIR`` configures the corresponding
    components — but pre-2026-04-25, ``apecx-cp serve`` ran the
    module-level ``app = create_app()`` (no kwargs) and ignored every
    one of those env vars. Result: every operator following the
    tutorial hit a 503 on the first ``/workflows/start`` call.

    This helper closes that gap. Defaults point at the in-repo
    ``composer_config.yml`` / ``approval_policy.yml`` / ``violin_bvbrc``
    workflow dir, so a fresh ``apecx-cp serve`` works out of the box
    for the tutorial scenario. Operators with custom configs override
    via env vars.

    Returns ``(composer, approval_policy, local_executor)``. Any of
    the three may be ``None`` if explicitly disabled (set the
    corresponding ``APECX_*_PATH=`` to empty string), in which case
    the matching route returns 503 with the same "not configured"
    detail as before.
    """
    from apecx_integration.composition.approval_policy import ApprovalPolicy
    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.composition.composer import Composer
    from apecx_integration.control_plane.executors.local import LocalExecutor

    session_factory = make_session_factory(engine)
    recorder = ProvenanceRecorder(session_factory)
    store = ArtifactStore(session_factory=session_factory, recorder=recorder)

    # NOTE on stderr prints below: alembic.ini's [logger_root]
    # fileConfig (called by ``ensure_infra_ready -> command.upgrade``)
    # uses ``disable_existing_loggers=True`` by default, which disables
    # every logger imported before alembic ran — including
    # ``apecx_integration.control_plane.app``. Re-enabling each
    # disabled logger by walking the dict is fragile across Python
    # versions; the operator-facing component-loaded announcements
    # are not log records anyway, they're startup banners that should
    # always print regardless of log config. So we bypass the logging
    # module here and write directly to stderr. The tutorial documents
    # these lines as the "is the composer wired?" verification signal.
    # Found during the 2026-04-25 tutorial e2e validation pass —
    # earlier attempts (basicConfig, setLevel) failed because of the
    # disable_existing_loggers gotcha.
    def _banner(msg: str) -> None:
        print(f"INFO apecx-cp serve: {msg}", file=sys.stderr, flush=True)

    composer = None
    composer_path_env = os.environ.get("APECX_COMPOSER_CONFIG_PATH")
    composer_path = (
        Path(composer_path_env)
        if composer_path_env
        else _DEFAULT_COMPOSER_CONFIG
    )
    if composer_path_env == "":
        _banner(
            "APECX_COMPOSER_CONFIG_PATH set to empty string; composer "
            "disabled, /workflows/start will 503."
        )
    elif composer_path.is_file():
        composer = Composer.from_config(composer_path)
        composer._artifact_store = store  # noqa: SLF001 — documented hook
        _banner(f"composer loaded from {composer_path}")
    else:
        _banner(
            f"WARNING composer config {composer_path} not found; "
            "/workflows/start will 503. Set APECX_COMPOSER_CONFIG_PATH "
            "to override."
        )

    approval_policy = None
    policy_path_env = os.environ.get("APECX_APPROVAL_POLICY_PATH")
    policy_path = (
        Path(policy_path_env)
        if policy_path_env
        else _DEFAULT_APPROVAL_POLICY
    )
    if policy_path_env == "":
        _banner(
            "APECX_APPROVAL_POLICY_PATH set to empty string; approval "
            "policy disabled, /workflows/start will 503."
        )
    elif policy_path.is_file():
        approval_policy = ApprovalPolicy.load(policy_path)
        _banner(f"approval policy loaded from {policy_path}")
    else:
        _banner(
            f"WARNING approval policy {policy_path} not found; "
            "/workflows/start will 503. Set APECX_APPROVAL_POLICY_PATH "
            "to override."
        )

    local_executor = None
    workflow_dir_env = os.environ.get("APECX_WORKFLOW_BASE_DIR")
    workflow_dir = (
        Path(workflow_dir_env)
        if workflow_dir_env
        else _DEFAULT_WORKFLOW_BASE_DIR
    )
    if workflow_dir_env == "":
        _banner(
            "APECX_WORKFLOW_BASE_DIR set to empty string; local executor "
            "disabled, /workflows/execute will 503."
        )
    elif workflow_dir.is_dir():
        local_executor = LocalExecutor(
            session_factory=session_factory,
            artifact_store=store,
            recorder=recorder,
            workflow_base_dir=workflow_dir,
        )
        _banner(
            f"local executor wired against workflow_base_dir={workflow_dir}"
        )
    else:
        _banner(
            f"WARNING workflow base dir {workflow_dir} not found; "
            "/workflows/execute will 503. Set APECX_WORKFLOW_BASE_DIR."
        )

    return composer, approval_policy, local_executor


def _serve(args: argparse.Namespace) -> int:
    from apecx_integration.control_plane.infra.lifecycle import ensure_infra_ready

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db_url = get_db_url()
    ensure_infra_ready(db_url)

    # Build a fully-wired app for production. Pre-2026-04-25 the CLI
    # ran uvicorn against the module-level ``app = create_app()``
    # which had no composer / policy / executor wired, so every
    # /workflows/start request returned 503 even though the docstring
    # of ``create_app`` and the env-var error messages claimed
    # otherwise. Now: build the components from env vars, pass them
    # explicitly into ``create_app``, and run uvicorn against the
    # resulting wired app.
    engine = make_engine()
    composer, policy, executor = _build_components_from_env(engine)
    wired_app = create_app(
        engine=engine,
        composer=composer,
        approval_policy=policy,
        local_executor=executor,
    )

    import uvicorn

    uvicorn.run(
        wired_app,
        host=args.host,
        port=args.port,
        log_level="info",
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
