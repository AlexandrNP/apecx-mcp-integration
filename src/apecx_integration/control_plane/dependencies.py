"""FastAPI dependencies for the Control Plane.

The app stores the engine, session factory, ProvenanceRecorder, and
(T01) optional Composer + ApprovalPolicy on ``app.state`` at
create-time so tests can swap them with test-specific resources.
Route handlers pull per-request resources via ``Depends(get_*)``;
writes explicitly ``session.commit()`` — we don't auto-commit in the
dependency because read handlers don't need to commit and
auto-committing muddles the success/failure boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session, sessionmaker

from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder

if TYPE_CHECKING:
    from apecx_integration.composition.approval_policy import ApprovalPolicy
    from apecx_integration.composition.composer import Composer
    from apecx_integration.control_plane.executors.local import LocalExecutor


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    with factory() as session:
        yield session


def get_session_factory(request: Request) -> sessionmaker[Session]:
    """Return the raw factory so routes that ``await`` external work
    can scope each session to a single non-async block.

    Audit §2.1 (docs/codebase_audit_2026_04_24.md): a FastAPI route
    that takes ``Depends(get_session)`` holds the session (and its
    pooled connection) across any ``await`` in the body. Under load,
    a route that awaits an external service (e.g.,
    ``composer.compose()`` calling out to an LLM) starves the
    connection pool.

    Routes that do NOT await across DB work should keep using
    ``get_session``. Routes that DO await across DB work should
    inject this factory and explicitly open / close one session per
    pre-await write block and one per post-await write block.
    """
    return request.app.state.session_factory


def get_recorder(request: Request) -> ProvenanceRecorder:
    return request.app.state.recorder


def get_composer(request: Request) -> Composer:
    """Return the Composer the app was built with, or raise 503.

    Composer is lazy because: (1) constructing one loads prompt files
    + walks catalog manifests — cheap but non-trivial; (2) tests need
    a placeholder-LLM composer, which they inject at app-create time;
    (3) deployments without an LLM backend (e.g. schema-only smoke
    runs) shouldn't be forced to build one just to start the API.
    """
    composer = getattr(request.app.state, "composer", None)
    if composer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Composer is not configured on this Control Plane. "
                "Set APECX_COMPOSER_CONFIG_PATH or pass composer= "
                "into create_app()."
            ),
        )
    return composer


def get_approval_policy(request: Request) -> ApprovalPolicy:
    policy = getattr(request.app.state, "approval_policy", None)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "ApprovalPolicy is not configured on this Control "
                "Plane. Set APECX_APPROVAL_POLICY_PATH or pass "
                "approval_policy= into create_app()."
            ),
        )
    return policy


def get_local_executor(request: Request) -> LocalExecutor:
    executor = getattr(request.app.state, "local_executor", None)
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "LocalExecutor is not configured on this Control "
                "Plane. Pass local_executor= into create_app() or set "
                "APECX_WORKFLOW_BASE_DIR so the app can build one."
            ),
        )
    return executor
