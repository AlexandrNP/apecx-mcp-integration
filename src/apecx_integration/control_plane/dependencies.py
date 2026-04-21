"""FastAPI dependencies for the Control Plane.

The app stores the engine, session factory, and ProvenanceRecorder on
``app.state`` at create-time so that tests can swap them with a
test-specific engine. Route handlers pull a per-request Session via
``Depends(get_session)``; writes explicitly ``session.commit()`` — we
don't auto-commit in the dependency because read handlers don't need
to commit and auto-committing muddles the success/failure boundary.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    with factory() as session:
        yield session


def get_recorder(request: Request) -> ProvenanceRecorder:
    return request.app.state.recorder
