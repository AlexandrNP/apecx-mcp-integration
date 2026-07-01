"""W3.2 — dashboard routes GET /status (JSON) + GET /dashboard (HTML) over the live monitor state.

These exercise the REAL route → get_monitor().snapshot() → get_orchestrator().status() → real probes
(whatever the backends' actual health), so they are a real integration of the view over the daemon
state, not a mock. create_app() is built WITHOUT start_monitor, so no daemon runs during the test."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apecx_integration.control_plane.app import create_app


@pytest.fixture(name="client")
def _client() -> TestClient:
    return TestClient(create_app())


def test_status_returns_expected_shape(client: TestClient):
    r = client.get("/status")
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= {"overall", "backends", "recent_failures"}
    assert isinstance(data["backends"], list) and data["backends"]
    assert isinstance(data["recent_failures"], list)
    # Each backend row carries the fields the views render (incl. the W3 `reachable`).
    b0 = data["backends"][0]
    assert {"name", "state", "reachable", "detail"} <= set(b0)
    assert "ollama" in {b["name"] for b in data["backends"]}  # ollama is always in the roster


def test_dashboard_returns_html(client: TestClient):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "apecx-mcp infrastructure" in r.text
    assert "auto-refresh" in r.text


def test_start_monitor_lifespan_launches_and_stops_the_daemon(monkeypatch):
    # start_monitor=True attaches the daemon lifespan. Patch run_forever to a no-op blocker so the
    # test verifies startup (task launched) + shutdown (cancelled cleanly) WITHOUT real docker ticks.
    import asyncio

    from apecx_integration.infrastructure import monitor as monmod

    launched = {"v": False}

    async def _fake_run_forever(self, **kw):
        launched["v"] = True
        await asyncio.Event().wait()  # block until the lifespan cancels us

    monkeypatch.setattr(monmod.InfraMonitor, "run_forever", _fake_run_forever)
    with TestClient(create_app(start_monitor=True)) as c:
        assert c.get("/status").status_code == 200
    assert launched["v"] is True


def test_dashboard_html_escapes_interpolated_detail():
    # A backend detail carrying HTML (e.g. probe error from a remote endpoint) must be escaped, not
    # injected into the page (review-gate W3).
    from apecx_integration.control_plane.routes.dashboard import _render_html

    data = {
        "overall": "down",
        "backends": [
            {
                "name": "x",
                "state": "down",
                "reachable": False,
                "detail": "<script>alert(1)</script>",
            }
        ],
        "recent_failures": [],
    }
    out = _render_html(data)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
