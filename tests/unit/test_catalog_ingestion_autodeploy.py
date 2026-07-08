"""InfraOrchestrator.ensure_catalog_seeded() — auto-seed the rhea tool catalog.

The rhea-server container auto-builds + runs, but its EXTERNAL postgres carries the
tool catalog. On an unseeded machine that catalog is empty and every rhea tool is
unavailable until ingestion runs. ``ensure_catalog_seeded`` detects that state via the
``galaxytools`` ROW COUNT (``_catalog_row_count`` — the ground truth; the MCP
``tools/list`` count is NOT usable because it always lists ``find_tools`` = 1 whether
the catalog is empty or seeded) and runs ``docker exec ... update_tools`` inside the
container so rhea works after nothing but ``uv install`` + ``apecx-setup``.

These are UNIT tests: ``_catalog_row_count`` + the docker subprocess are monkeypatched
(pytest built-ins only — no unittest.mock per repo policy), so they pin
``ensure_catalog_seeded``'s BRANCHING. The real detection primitive
(``_catalog_row_count``'s psql round-trip) + the actual truncate→ingest→re-seed path
are covered by the committed, docker/ollama-gated
``tests/integration/test_catalog_ingestion_live.py`` (TRUNCATE galaxytools →
ensure_catalog_seeded → row count 0→>0 → idempotent already_seeded on re-call).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from apecx_integration.infrastructure import orchestrator as orch_mod
from apecx_integration.infrastructure.backends import (
    BackendSpec,
    ContainerSpec,
    Probe,
    ProbeResult,
)
from apecx_integration.infrastructure.orchestrator import InfraOrchestrator


def _rhea_orch() -> InfraOrchestrator:
    """An orchestrator whose only backend is a fake rhea_mcp container. Detection is
    driven by monkeypatching ``_catalog_row_count`` in each test (see ``_set_counts``),
    so the probe here is a never-load-bearing stub."""

    async def _probe() -> ProbeResult:
        return ProbeResult(healthy=True, detail="stub", latency_ms=1.0)

    spec = BackendSpec(
        name="rhea_mcp",
        display_name="Rhea MCP (container)",
        kind="docker_container",
        required=True,
        probe=Probe(name="rhea_mcp", fn=_probe),
        actionable_message="fake rhea",
        container=ContainerSpec(
            image="apecx/rhea-server:test",
            container_name="apecx-rhea-server",
            ports=((3001, 3001),),
        ),
    )
    # docker_binary truthy so self._docker is not None (no real docker invoked;
    # subprocess.run is monkeypatched in the tests that reach ingestion).
    return InfraOrchestrator([spec], autostart_enabled=True, docker_binary="/fake/docker")


def _set_counts(monkeypatch, o: InfraOrchestrator, counts: list) -> None:
    """Make ``o._catalog_row_count()`` return ``counts`` in order (one per call):
    the 1st call is the detection, the 2nd (if reached) is the post-ingest re-count."""
    seq = iter(counts)

    async def _fake_count() -> int | None:
        return next(seq)

    monkeypatch.setattr(o, "_catalog_row_count", _fake_count)


def _record_run(calls: list, returncode: int):
    """A fake ``subprocess.run`` that records the argv and returns ``returncode``."""

    def _fake(cmd, *a, **k):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=returncode, stdout=b"ingested\n", stderr=b"boom")

    return _fake


def test_already_seeded_skips_docker_exec(monkeypatch):
    o = _rhea_orch()
    _set_counts(monkeypatch, o, [7])  # catalog already has rows
    calls: list = []
    monkeypatch.setattr(orch_mod.subprocess, "run", _record_run(calls, 0))

    out = asyncio.run(o.ensure_catalog_seeded())

    assert out["seeded"] is True
    assert out["action"] == "already_seeded"
    assert calls == [], "a seeded catalog must NOT trigger a docker exec ingestion"


def test_empty_catalog_runs_ingestion_then_ingested(monkeypatch):
    o = _rhea_orch()
    _set_counts(monkeypatch, o, [0, 1])  # detect 0 → ingest → re-count 1
    calls: list = []
    monkeypatch.setattr(orch_mod.subprocess, "run", _record_run(calls, 0))

    out = asyncio.run(o.ensure_catalog_seeded())

    assert out["seeded"] is True
    assert out["action"] == "ingested"
    assert out["ingest_only"] == "muscle"
    assert len(calls) == 1, "exactly one docker exec ingestion"
    argv = calls[0]
    assert argv[0] == "/fake/docker"
    assert "exec" in argv
    assert "RHEA_INGEST_ONLY=muscle" in argv
    assert "apecx-rhea-server" in argv
    assert argv[-1] == "cd /app && uv run python -m rhea.preprocess.update_tools"


def test_ingest_only_env_override_is_passed(monkeypatch):
    o = _rhea_orch()
    _set_counts(monkeypatch, o, [0, 1])
    calls: list = []
    monkeypatch.setattr(orch_mod.subprocess, "run", _record_run(calls, 0))
    monkeypatch.setenv("APECX_RHEA_INGEST_ONLY", "muscle,blast")

    out = asyncio.run(o.ensure_catalog_seeded())

    assert out["ingest_only"] == "muscle,blast"
    assert "RHEA_INGEST_ONLY=muscle,blast" in calls[0]


def test_nonzero_ingest_exit_fails_loud(monkeypatch):
    o = _rhea_orch()
    _set_counts(monkeypatch, o, [0])  # detect empty; ingestion fails before re-count
    calls: list = []
    monkeypatch.setattr(orch_mod.subprocess, "run", _record_run(calls, 1))

    with pytest.raises(RuntimeError, match="ingestion FAILED"):
        asyncio.run(o.ensure_catalog_seeded())
    assert len(calls) == 1, "ingestion was attempted before failing loud"


def test_count_unavailable_skips(monkeypatch):
    """When the catalog row count can't be read (no postgres backend / no docker /
    query error), ``_catalog_row_count`` returns None → we skip (don't guess)."""
    o = _rhea_orch()
    _set_counts(monkeypatch, o, [None])
    calls: list = []
    monkeypatch.setattr(orch_mod.subprocess, "run", _record_run(calls, 0))

    out = asyncio.run(o.ensure_catalog_seeded())

    assert out["seeded"] is False
    assert out["action"] == "skipped"
    assert calls == [], "cannot ingest without a readable catalog count"


def test_no_docker_binary_skips():
    o = _rhea_orch()
    o._docker = None

    out = asyncio.run(o.ensure_catalog_seeded())

    assert out["seeded"] is False
    assert out["action"] == "skipped"


def test_no_rhea_backend_skips():
    # A roster WITHOUT a rhea backend → ensure_catalog_seeded skips.
    spec = BackendSpec(
        name="postgres",
        display_name="pg",
        kind="docker_container",
        required=True,
        probe=Probe(name="pg", fn=lambda: None),  # never called
        actionable_message="",
        container=ContainerSpec(image="pg", container_name="pg", ports=((5432, 5432),)),
    )
    o = InfraOrchestrator([spec], autostart_enabled=True, docker_binary="/fake/docker")

    out = asyncio.run(o.ensure_catalog_seeded())

    assert out["seeded"] is False
    assert out["action"] == "skipped"
    assert "no rhea backend" in out["reason"]
