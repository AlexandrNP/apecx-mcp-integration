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
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import Engine

from apecx_integration.control_plane.db import get_db_url, make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.routes import (
    approval,
    dashboard,
    hpc,
    metrics,
    status,
    verified_synonyms,
    workflow,
)

log = logging.getLogger(__name__)

# Per-request correlation id (set by the request-log middleware). A future logging
# Filter can read this to stamp EVERY record with the request id; for now the access
# line carries it explicitly.
_request_id_var: ContextVar[str] = ContextVar("cp_request_id", default="-")


def _sanitize_request_id(inbound: str | None) -> str:
    """Sanitize a caller-supplied ``X-Request-ID`` (ASCII alnum/-/_ only, ≤64 chars) so it can't
    inject newlines/control chars into the log line or response header; mint a short id when
    absent/empty. Note ``c.isascii()`` is REQUIRED — bare ``isalnum()`` admits Unicode letters,
    which then blow up latin-1 response-header encoding (a caller-controlled 500)."""
    if inbound:
        cleaned = "".join(c for c in inbound if (c.isascii() and c.isalnum()) or c in "-_")[:64]
        if cleaned:
            return cleaned
    return uuid.uuid4().hex[:12]


def _resolve_sweep_interval() -> float:
    """Seconds between stuck-run sweeps. Overridable via ``APECX_RUN_SWEEP_INTERVAL_SECONDS``
    (operators tune it; tests drive it fast). Falls back to 300 s on a missing/invalid/non-positive
    value — a bad env var must never break serve startup."""
    try:
        val = float(os.environ.get("APECX_RUN_SWEEP_INTERVAL_SECONDS", "300"))
    except ValueError:
        return 300.0
    # Must be finite + positive: `inf`/`nan`/≤0 would silently DISABLE the reaper (sleep(inf)
    # never wakes) — the exact silent-non-execution class this whole fix exists to prevent.
    return val if 0 < val < float("inf") else 300.0


async def _run_sweep_loop(sweeper, *, interval_seconds: float, stale_after) -> None:
    """Periodically run the RunStateSweeper so a run whose executor died mid-flight is reaped to
    FAILED (with an actionable provenance note) instead of sitting in RUNNING forever. The sweeper
    is fully built + tested but was never invoked at serve time until this wiring. ``sweep`` is
    SYNC — run it off-loop via ``to_thread`` so it never blocks the event loop; a sweep exception
    must never kill the loop (a transient DB hiccup should not stop future sweeps)."""
    import asyncio
    from contextlib import suppress

    while True:
        await asyncio.sleep(interval_seconds)
        with suppress(Exception):
            reaped = await asyncio.to_thread(sweeper.sweep, stale_after=stale_after)
            if reaped:
                log.warning(
                    "RunStateSweeper reaped %d stale run(s) → FAILED (stale_after=%s).",
                    len(reaped),
                    stale_after,
                )


# Default config paths resolved RELATIVE TO this module file, so they
# work in BOTH editable-install AND isolated-wheel install modes
# (uv tool / pipx / pip --user). ``Path(__file__).resolve().parent.parent``
# is the ``apecx_integration/`` package root regardless of how the
# package was installed.
#
# ``configs/approval_policy.yml`` is shipped inside the package at
# ``_configs/approval_policy.yml`` (bundled copy) so it ships with the
# wheel; see ``_configs/`` directory and the byte-equivalence
# regression test (``tests/integration/test_alembic_bundled_in_package.py``).
_PKG_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_COMPOSER_CONFIG = _PKG_ROOT / "composition" / "composer_config.yml"
_DEFAULT_APPROVAL_POLICY = _PKG_ROOT / "_configs" / "approval_policy.yml"
_DEFAULT_WORKFLOW_BASE_DIR = _PKG_ROOT / "composition" / "workflows" / "rag_e2e_synthesis"


def create_app(
    engine: Engine | None = None,
    *,
    composer=None,
    approval_policy=None,
    local_executor=None,
    recorder: ProvenanceRecorder | None = None,
    start_monitor: bool = False,
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

    ``recorder`` lets the caller supply the SAME ``ProvenanceRecorder``
    instance that the composer / executor / artifact store hold a
    reference to. This is load-bearing: the recorder maintains a
    per-run in-memory hash-cursor cache (cluster X), and that cache
    is per-instance. Two recorders writing to the same run produce a
    forking provenance chain (cluster AD, 2026-04-26) — recorder A's
    next write picks A's stale cached predecessor, missing the
    intervening events recorder B wrote. The serve path builds one
    recorder and passes it both here and into ``_build_components_from_env``.
    Tests that don't exercise composer/executor + routes for the
    same run can pass nothing and a fresh recorder is created.

    This function does NOT touch infrastructure — the CLI ``main()``
    below is responsible for bringing up Postgres and running migrations
    before uvicorn is started. Keeping infra out of ``create_app`` means
    tests (which pass their own engine) do not spin up Docker.
    """
    resolved_engine = engine or make_engine()
    session_factory = make_session_factory(resolved_engine)

    # Optional always-on serve daemons (W3). Only attached when serving (start_monitor=True); tests
    # build the app WITHOUT it, so create_app stays infra-free (no docker polling in tests). Two
    # background loops run for the serving lifetime: the InfraMonitor (backend polling) and the
    # RunStateSweeper (reaps runs stuck in RUNNING/PAUSED after a dead executor — see _run_sweep_loop;
    # without this wiring the sweeper existed but was never called, so a dead run sat RUNNING forever).
    monitor_lifespan = None
    if start_monitor:
        import asyncio
        from contextlib import asynccontextmanager, suppress

        from apecx_integration.control_plane.notifications.sweeper import (
            DEFAULT_STALE_AFTER,
            RunStateSweeper,
        )
        from apecx_integration.infrastructure.monitor import get_monitor

        sweeper = RunStateSweeper(session_factory, recorder or ProvenanceRecorder(session_factory))
        sweep_interval = _resolve_sweep_interval()

        @asynccontextmanager
        async def monitor_lifespan(_app: FastAPI):
            monitor_task = asyncio.create_task(get_monitor().run_forever())
            sweep_task = asyncio.create_task(
                _run_sweep_loop(
                    sweeper,
                    interval_seconds=sweep_interval,
                    stale_after=DEFAULT_STALE_AFTER,
                )
            )
            try:
                yield
            finally:
                for task in (monitor_task, sweep_task):
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task

    app = FastAPI(
        title="APECx Control Plane",
        description=(
            "Tier 2 of the APECx integration. Holds run state, provenance, approvals, "
            "artifacts, allocation accountant. See architectural_plan.md §3.1 and §R3."
        ),
        version="0.0.1",
        lifespan=monitor_lifespan,
    )

    @app.middleware("http")
    async def _request_log(request, call_next):
        """Mint/propagate a request id, emit a structured access line, echo the id on the
        response so a client / load balancer / operator can correlate one request. Complements
        (does not replace) uvicorn's access log; must NOT swallow handler exceptions."""
        rid = _sanitize_request_id(request.headers.get("x-request-id"))
        token = _request_id_var.set(rid)
        start = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        log.info(
            "cp-access rid=%s %s %s -> %d %.1fms",
            rid,
            request.method,
            request.url.path,
            response.status_code,
            (time.monotonic() - start) * 1000.0,
        )
        response.headers["X-Request-ID"] = rid
        return response

    app.state.engine = resolved_engine
    app.state.session_factory = session_factory
    app.state.recorder = recorder or ProvenanceRecorder(session_factory)
    app.state.composer = composer
    app.state.approval_policy = approval_policy
    app.state.local_executor = local_executor

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "phase": "scaffold"}

    # Probe batch 1 (2026-04-26) — fail-fast for non-finite floats.
    # Standard JSON forbids NaN / Infinity / -Infinity; Python's
    # json.loads accepts them by default. Pydantic happily binds them
    # to ``float`` fields; a per-field finiteness validator catches
    # them, BUT the resulting RequestValidationError context contains
    # the raw float, and FastAPI's default error response serializer
    # (Python's json.dumps with allow_nan=False after fastapi
    # encodes) crashes on encode → 500 with no body. From the user's
    # perspective: a malformed input gets a generic "internal server
    # error" instead of a clean 422 explaining what's wrong.
    #
    # Register an exception handler that scrubs non-finite floats
    # from the error context before serialization. Same intent as
    # adding a strict JSON parser at the request boundary, but
    # cheaper to ship.
    from fastapi import Request as _FastAPIRequest
    from fastapi.exceptions import RequestValidationError as _RVE
    from fastapi.responses import JSONResponse as _JSONResponse

    def _scrub(value):
        # Replace any value the default JSON encoder can't handle —
        # non-finite floats, ValueError/other exception objects in
        # Pydantic's error context, etc. — with a string repr.
        import math as _math

        if isinstance(value, float):
            if not _math.isfinite(value):
                return f"<non-finite: {value!r}>"
            return value
        if isinstance(value, dict):
            return {k: _scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_scrub(v) for v in value]
        if isinstance(value, (str, int, bool)) or value is None:
            return value
        # Anything else (ValueError, Exception subclasses, custom
        # objects) — stringify so json.dumps can serialize.
        return repr(value)

    @app.exception_handler(_RVE)
    async def _request_validation_handler(request: _FastAPIRequest, exc: _RVE) -> _JSONResponse:
        # Scrub the error list so JSON serialization can't crash on
        # non-finite floats nested in the input context.
        scrubbed = _scrub(exc.errors())
        return _JSONResponse(status_code=422, content={"detail": scrubbed})

    app.include_router(workflow.router)
    app.include_router(approval.router)
    app.include_router(status.router)
    app.include_router(hpc.router)
    app.include_router(metrics.router)
    app.include_router(verified_synonyms.router)
    app.include_router(dashboard.router)

    return app


app = create_app()


def _build_components_from_env(
    engine: Engine,
    *,
    recorder: ProvenanceRecorder | None = None,
) -> tuple:
    """Build composer + approval policy + local executor from env vars.

    The 503 error messages on ``/workflows/start`` etc. promise that
    setting ``APECX_COMPOSER_CONFIG_PATH`` / ``APECX_APPROVAL_POLICY_PATH``
    / ``APECX_WORKFLOW_BASE_DIR`` configures the corresponding
    components — but pre-2026-04-25, ``apecx-cp serve`` ran the
    module-level ``app = create_app()`` (no kwargs) and ignored every
    one of those env vars. Result: every operator following the
    tutorial hit a 503 on the first ``/workflows/start`` call.

    This helper closes that gap. Defaults point at the in-repo
    ``composer_config.yml`` / ``approval_policy.yml`` / ``rag_e2e_synthesis``
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
    # Single recorder per process (cluster AD, 2026-04-26): the
    # caller is expected to pass the same instance that ``create_app``
    # will hang on ``app.state.recorder``. Cluster X's per-run hash
    # cache is per-instance, so two instances writing to one run
    # produce a forking chain. Fall back to a fresh recorder only
    # when the caller hasn't given us one — for symmetry with
    # tests that bootstrap components directly.
    if recorder is None:
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
    composer_path = Path(composer_path_env) if composer_path_env else _DEFAULT_COMPOSER_CONFIG
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
    policy_path = Path(policy_path_env) if policy_path_env else _DEFAULT_APPROVAL_POLICY
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
    workflow_dir = Path(workflow_dir_env) if workflow_dir_env else _DEFAULT_WORKFLOW_BASE_DIR
    if workflow_dir_env == "":
        _banner(
            "APECX_WORKFLOW_BASE_DIR set to empty string; local executor "
            "disabled, /workflows/execute will 503."
        )
    elif workflow_dir.is_dir():
        # EMPTY-FAIL (2026-05-12): the executor now rejects empty
        # input by default. Production usage flows REAL payloads
        # through ``/workflows/execute`` (the request body carries
        # the workflow input) — TODO when that route lands, drop
        # the env-var override. Operators who want to keep the
        # pre-EMPTY-FAIL "run with {} and accept whatever happens"
        # behavior set APECX_EXECUTOR_ALLOW_EMPTY_INPUT=1.
        allow_empty_in_env = os.environ.get("APECX_EXECUTOR_ALLOW_EMPTY_INPUT", "").lower() in (
            "1",
            "true",
            "yes",
        )
        allow_empty_out_env = os.environ.get("APECX_EXECUTOR_ALLOW_EMPTY_OUTPUT", "").lower() in (
            "1",
            "true",
            "yes",
        )
        # Catalog roots so a composed workflow reusing wrappers from multiple
        # catalog dirs resolves at run time (nanobrain config_base Strategy 7).
        # Resolves off composer_path, which defaults to the real config even when
        # the composer is disabled — harmless: the executor branch is independent
        # of the composer branch, and Strategy 7 is additive/no-op-on-success.
        # A genuinely missing config file -> [] (no-op).
        from apecx_integration.composition.component_catalog import catalog_search_roots

        catalog_roots = catalog_search_roots(composer_path)
        local_executor = LocalExecutor(
            session_factory=session_factory,
            artifact_store=store,
            recorder=recorder,
            workflow_base_dir=workflow_dir,
            allow_empty_input=allow_empty_in_env,
            allow_empty_output=allow_empty_out_env,
            config_search_paths=catalog_roots,
        )
        _banner(
            f"local executor wired against workflow_base_dir={workflow_dir} "
            f"(allow_empty_input={allow_empty_in_env} "
            f"allow_empty_output={allow_empty_out_env} "
            f"catalog_search_roots={len(catalog_roots)})"
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
    # Build the recorder once and share it: one in-memory hash
    # cache for the whole process, so the composer/executor and
    # the route handlers don't fork the chain (cluster AD).
    serve_session_factory = make_session_factory(engine)
    serve_recorder = ProvenanceRecorder(serve_session_factory)
    composer, policy, executor = _build_components_from_env(engine, recorder=serve_recorder)
    wired_app = create_app(
        engine=engine,
        composer=composer,
        approval_policy=policy,
        local_executor=executor,
        recorder=serve_recorder,
        start_monitor=True,  # W3: run the always-on infra monitor + stuck-run sweeper while serving
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
                    "docker volume rm apecx_cp_postgres_data (all Postgres state; unrecoverable)"
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
