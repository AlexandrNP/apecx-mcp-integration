"""Smoke test: the scaffolded package imports cleanly.

Per workspace CLAUDE.md, smoke tests may use mocks; this one does not need any.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.smoke


def test_top_level_package_imports() -> None:
    import apecx_integration  # noqa: F401
    from apecx_integration import __version__

    assert __version__ == "0.0.1"


def test_tier_subpackages_import() -> None:
    import apecx_integration.mcp_surface  # noqa: F401
    import apecx_integration.control_plane  # noqa: F401
    import apecx_integration.composition  # noqa: F401
    import apecx_integration.execution  # noqa: F401
    import apecx_integration.config  # noqa: F401


def test_control_plane_app_factory_runs() -> None:
    from apecx_integration.control_plane.app import create_app

    app = create_app()
    assert app.title == "APECx Control Plane"


def test_healthz_route_is_registered() -> None:
    from fastapi.testclient import TestClient

    from apecx_integration.control_plane.app import create_app

    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "phase": "scaffold"}
