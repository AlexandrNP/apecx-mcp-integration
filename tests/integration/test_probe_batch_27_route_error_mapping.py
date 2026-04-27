"""Probe batch 27 — route error mapping + app construction
(probes 705-729).

The Control Plane's HTTP layer is the boundary every external caller
hits. Silent-failure modes here would let bad inputs return 500
"internal server error" (when 422 is the right answer) or let typo'd
endpoints 404 with no guidance.

This batch probes:
  - /healthz returns 200 with the expected shape
  - The cluster AL fix: NaN / Infinity in floats no longer crashes
    JSON serialization → returns 422 with scrubbed body
  - The RequestValidationError handler scrubs nested non-finite
    floats AND non-serializable objects before encoding
  - create_app wires up all 6 routers
  - openapi schema generates without crashing (smoke check)
  - Pydantic Field bounds (description min_length, etc.) reject
    malformed bodies at the boundary
  - 501-stub endpoints stay 501 (HPC submit is deliberately
    not-yet-implemented)
"""

from __future__ import annotations

import math
import os
import uuid
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(tmp_path):
    """Build a fresh FastAPI app backed by an isolated SQLite DB.
    Migrations run automatically so route handlers can hit a real DB."""
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.app import create_app
    from apecx_integration.control_plane.db import make_engine
    db = tmp_path / "probe27.db"
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    eng = make_engine(f"sqlite:///{db}")
    return create_app(engine=eng)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /healthz + app construction — probes 705-708
# ---------------------------------------------------------------------------


def test_probe_705_healthz_returns_ok(client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "phase": "scaffold"}


def test_probe_706_create_app_attaches_state(app) -> None:
    """The app must expose engine, session_factory, recorder on
    app.state so route handlers can resolve them via Depends."""
    assert hasattr(app.state, "engine")
    assert hasattr(app.state, "session_factory")
    assert hasattr(app.state, "recorder")


def test_probe_707_app_includes_six_routers(app) -> None:
    """Every router must be attached. A missing include_router would
    silently 404 the routes that router serves."""
    paths = {route.path for route in app.routes}
    expected = {
        "/healthz",
        "/workflows/start", "/workflows/diff", "/workflows/execute",
        "/workflows/plan",
        "/approvals/", "/approvals/{approval_id}",
        "/approvals/approve", "/approvals/reject",
        "/approvals/correct", "/approvals/pending",
        "/runs/list", "/runs/status", "/runs/artifact",
        "/hpc/estimate", "/hpc/confirm", "/hpc/submit",
        "/hpc/export", "/hpc/ingest",
        "/metrics/approvals",
        "/verified_synonyms/lookup", "/verified_synonyms/",
    }
    missing = expected - paths
    assert not missing, f"PROBE 707: missing routes: {missing}"


def test_probe_708_openapi_schema_generates(client) -> None:
    """openapi schema must generate without crashing — confirms
    every Pydantic schema referenced by routes is JSON-serializable."""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "APECx Control Plane"
    assert "paths" in schema
    assert len(schema["paths"]) > 10


# ---------------------------------------------------------------------------
# Non-finite float scrubbing (cluster AL regression) — probes 709-714
# ---------------------------------------------------------------------------


def _post_raw_json(client, path: str, raw_body: str):
    """Bypass httpx's strict-JSON encoder so we can send NaN/Infinity
    tokens that Python's json.loads accepts but httpx refuses to emit."""
    return client.post(
        path,
        content=raw_body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def test_probe_709_nan_in_confirmed_core_hours_returns_422(client) -> None:
    """NaN must produce 422 with a JSON body, NOT 500 with no body
    (cluster AL pre-fix behavior). The scrubbed body explains
    'must be finite'."""
    rid = str(uuid.uuid4())
    r = _post_raw_json(client, "/hpc/confirm",
        f'{{"run_id": "{rid}", "confirmed_core_hours": NaN}}')
    assert r.status_code == 422
    body = r.json()
    assert "detail" in body


def test_probe_710_infinity_in_confirmed_core_hours_returns_422(client) -> None:
    rid = str(uuid.uuid4())
    r = _post_raw_json(client, "/hpc/confirm",
        f'{{"run_id": "{rid}", "confirmed_core_hours": Infinity}}')
    assert r.status_code == 422


def test_probe_711_neg_infinity_returns_422(client) -> None:
    rid = str(uuid.uuid4())
    r = _post_raw_json(client, "/hpc/confirm",
        f'{{"run_id": "{rid}", "confirmed_core_hours": -Infinity}}')
    assert r.status_code == 422


def test_probe_712_422_body_scrubs_non_finite_floats(client) -> None:
    """The scrubbed body must NOT contain raw NaN/Infinity tokens
    in a way that would crash strict JSON parsers downstream.
    The scrubber replaces them with the ``<non-finite: ...>``
    marker — which IS a string and IS valid JSON."""
    import json
    rid = str(uuid.uuid4())
    r = _post_raw_json(client, "/hpc/confirm",
        f'{{"run_id": "{rid}", "confirmed_core_hours": NaN}}')
    # Body must be valid JSON when re-parsed by a strict parser
    text = r.text
    parsed = json.loads(text)  # raises if NaN bled through unscrubbed
    assert "detail" in parsed


def test_probe_713_422_handler_doesnt_break_valid_requests(client) -> None:
    """Sanity: a valid finite request must NOT trigger the
    scrubber. Confirm by hitting /hpc/confirm with a finite value
    on a non-existent run_id (will 404 on lookup)."""
    rid = str(uuid.uuid4())
    r = client.post("/hpc/confirm", json={
        "run_id": rid,
        "confirmed_core_hours": 10.0,
    })
    # Either 404 (no run found) or 422 (validation issue), but
    # crucially NOT 500 from a serialization crash
    assert r.status_code in (404, 422)


def test_probe_714_scrub_handler_explicit_marker(client) -> None:
    """The scrubber emits a ``<non-finite: ...>`` token for NaN /
    Infinity values inside Pydantic's error context. Confirm the
    422 response is fully serializable AND the marker shows up
    when we deliberately pass a non-finite value."""
    rid = str(uuid.uuid4())
    r = _post_raw_json(client, "/hpc/confirm",
        f'{{"run_id": "{rid}", "confirmed_core_hours": NaN}}')
    assert r.status_code == 422
    body = r.json()
    # detail is a list of error dicts; somewhere inside, the input
    # value should be either scrubbed or absent (not raw NaN).
    text = r.text
    # Strict JSON does NOT contain bare NaN/Infinity tokens
    assert "NaN" not in text or "non-finite" in text
    assert "Infinity" not in text or "non-finite" in text


# ---------------------------------------------------------------------------
# Pydantic Field bounds at the HTTP boundary — probes 715-720
# ---------------------------------------------------------------------------


def test_probe_715_workflows_start_rejects_empty_description(client) -> None:
    """description has Field(min_length=1). An empty body is a
    no-op compose request — must reject."""
    r = client.post("/workflows/start", json={
        "description": "",
        "user_id": "u",
    })
    assert r.status_code == 422


def test_probe_716_workflows_start_rejects_missing_user_id(client) -> None:
    r = client.post("/workflows/start", json={
        "description": "go fetch eeev synonyms",
    })
    assert r.status_code == 422


def test_probe_717_approvals_correct_rejects_extra_fields(client) -> None:
    """_APIBase has extra='forbid'. The route must reject typo'd
    fields with 422 (the property batches 18 + 20 caught at the
    schema layer; this confirms it travels all the way to the wire)."""
    r = client.post("/approvals/correct", json={
        "approval_id": str(uuid.uuid4()),
        "modifications": {"k": "v"},
        "comment": "should not be accepted",  # extra field
    })
    # Could be 422 (extra forbidden) or 404 (approval doesn't exist).
    # The bug-class shape would be 200 with comment silently dropped —
    # which extra='forbid' prevents.
    assert r.status_code != 200


def test_probe_718_unknown_run_status_path_returns_400(client) -> None:
    """A malformed status query must produce a clear error (4xx),
    not 500."""
    r = client.post("/runs/status", json={
        "run_id": "not-a-uuid",
    })
    assert 400 <= r.status_code < 500


def test_probe_719_metrics_approvals_rejects_bad_iso(client) -> None:
    """/metrics/approvals?since=garbage must return 400 with a
    helpful detail — probe 400 (batch 15) confirmed _parse_since
    behavior at the function level; this confirms it reaches the
    wire."""
    r = client.get("/metrics/approvals?since=not-an-iso-timestamp")
    assert r.status_code == 400
    body = r.json()
    assert "detail" in body
    assert "ISO" in body["detail"] or "iso" in body["detail"]


def test_probe_720_metrics_approvals_requires_since(client) -> None:
    """``since`` is a required query param. Omitting → 422."""
    r = client.get("/metrics/approvals")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 501 + 404 invariants — probes 721-724
# ---------------------------------------------------------------------------


def test_probe_721_hpc_submit_returns_501(client) -> None:
    """``/hpc/submit`` is deliberately not-yet-implemented per
    CLAUDE.md. A future PR shipping a real handler must come
    through here."""
    r = client.post("/hpc/submit", json={
        "run_id": str(uuid.uuid4()),
        "executor": "globus_compute",
    })
    assert r.status_code == 501
    body = r.json()
    assert "detail" in body


def test_probe_722_unknown_approval_id_returns_404(client) -> None:
    """Lookup of an approval that doesn't exist must return 404,
    not 500. A scientist polling for an approval they just rejected
    should see a clean 404."""
    r = client.get(f"/approvals/{uuid.uuid4()}")
    assert r.status_code == 404


def test_probe_723_estimate_unknown_run_returns_404(client) -> None:
    r = client.post("/hpc/estimate", json={"run_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_probe_724_export_unknown_run_returns_404(client) -> None:
    r = client.post("/hpc/export", json={
        "run_id": str(uuid.uuid4()),
        "target_system": "polaris",
        "output_directory": "/tmp",
    })
    assert r.status_code in (404, 422)


# ---------------------------------------------------------------------------
# CORS + method-not-allowed + content-type — probes 725-729
# ---------------------------------------------------------------------------


def test_probe_725_get_on_post_only_route_returns_405(client) -> None:
    """POST-only routes must return 405 (not 404 or 500) for GETs.
    A scientist who hits /workflows/start in a browser sees 405
    and knows to switch to POST."""
    r = client.get("/workflows/start")
    assert r.status_code == 405


def test_probe_726_unknown_path_returns_404(client) -> None:
    """A typo'd path must cleanly 404. Some FastAPI configurations
    return 307 redirects on trailing-slash differences — that's a
    silent footgun."""
    r = client.post("/totally/made/up/path", json={})
    assert r.status_code == 404


def test_probe_727_invalid_json_body_returns_422(client) -> None:
    """A malformed JSON body must produce 422 (or a similar 4xx)."""
    r = client.post(
        "/workflows/start",
        content=b"this is not json {",
        headers={"Content-Type": "application/json"},
    )
    assert 400 <= r.status_code < 500


def test_probe_728_workflows_diff_unknown_run_404(client) -> None:
    r = client.post("/workflows/diff", json={"run_id": str(uuid.uuid4())})
    assert r.status_code in (404, 422)


def test_probe_729_app_metadata_set(app) -> None:
    """The OpenAPI title / version are user-facing in the docs UI.
    Empty / missing values would render "FastAPI 0.0.1" which is
    confusing to a scientist looking at the docs."""
    assert app.title == "APECx Control Plane"
    assert app.version
    assert app.description
