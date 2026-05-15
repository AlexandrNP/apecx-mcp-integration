"""Unit tests for the Rhea tool-env pre-warm phase.

Scope: shape-handling + catalog walk + report aggregation. The live
install path is exercised by the integration validation recorded in
``docs/apecx_mcp_infrastructure.md`` §2.2 — a mock-only test cannot
prove the install works against a real conda. Per the workspace
unit-mock / integration-test parity rule, every behavior verified
here has a matching live recorded validation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from apecx_integration.infrastructure.rhea_prewarm import (
    PrewarmReport,
    ToolPrewarmResult,
    _collect_tools_from_catalog,
)
from apecx_integration.mcp_surface.workflow_registry import WorkflowCatalogEntry


@dataclass
class _CatalogStub:
    """Minimal duck-typed stand-in for ``WorkflowCatalog``."""

    workflows: list[WorkflowCatalogEntry] = field(default_factory=list)


def _make_entry(tool_name: str, prewarm: list[str]) -> WorkflowCatalogEntry:
    """Build a minimal valid catalog entry with the given prewarm list."""
    return WorkflowCatalogEntry(
        tool_name=tool_name,
        description="x" * 20,
        source={"kind": "yaml", "path": "fake/path.yml"},
        input_schema={"type": "object"},
        prewarm_rhea_tools=prewarm,
    )


def test_collect_tools_dedupe_across_entries() -> None:
    """Two workflows declaring the same Rhea tool yield ONE entry."""
    cat = _CatalogStub(
        workflows=[
            _make_entry("wf_a", ["muscle", "blastp"]),
            _make_entry("wf_b", ["muscle"]),
            _make_entry("wf_c", []),
        ]
    )
    out = _collect_tools_from_catalog(cat)
    assert out == ["muscle", "blastp"], (
        f"dedupe should preserve first-seen order and skip empty lists; got {out!r}"
    )


def test_collect_tools_empty_catalog() -> None:
    """No workflows → empty list (not an error)."""
    cat = _CatalogStub(workflows=[])
    assert _collect_tools_from_catalog(cat) == []


def test_prewarm_rhea_tools_default_empty() -> None:
    """A catalog entry without ``prewarm_rhea_tools`` defaults to []."""
    entry = WorkflowCatalogEntry(
        tool_name="x",
        description="x" * 20,
        source={"kind": "yaml", "path": "y.yml"},
        input_schema={"type": "object"},
    )
    assert entry.prewarm_rhea_tools == []


def test_prewarm_rhea_tools_typo_is_rejected() -> None:
    """``extra='forbid'`` on the Pydantic model catches field typos.

    Why this matters: prewarm depends on the operator declaring the
    field correctly in catalog YAML. A silent typo
    (``prewarm_rhe_tools:``) would leave the field at its empty
    default and pre-warm would silently skip the tool — exactly the
    silent-failure shape the workspace rule
    ``Pydantic extra='forbid' rule`` exists to prevent.
    """
    import pydantic

    with pytest.raises(pydantic.ValidationError) as exc_info:
        WorkflowCatalogEntry(
            tool_name="x",
            description="x" * 20,
            source={"kind": "yaml", "path": "y.yml"},
            input_schema={"type": "object"},
            prewarm_rhe_tools=["muscle"],  # type: ignore[call-arg]
        )
    assert "prewarm_rhe_tools" in str(exc_info.value)


def test_report_all_ready_states() -> None:
    """``all_ready`` is True iff every tool is ready OR reused."""
    r = PrewarmReport(
        tools=[
            ToolPrewarmResult(tool_name="a", state="ready"),
            ToolPrewarmResult(tool_name="b", state="reused"),
        ]
    )
    assert r.all_ready is True


def test_report_all_ready_false_on_failure() -> None:
    """A single ``state='failed'`` flips ``all_ready`` to False."""
    r = PrewarmReport(
        tools=[
            ToolPrewarmResult(tool_name="a", state="ready"),
            ToolPrewarmResult(tool_name="b", state="failed", error="boom"),
        ]
    )
    assert r.all_ready is False


def test_report_snapshot_includes_error_only_when_present() -> None:
    """Snapshot omits the ``error`` field on success, includes it on failure.

    This is what ``infrastructure_status`` surfaces to the operator;
    the asymmetry keeps the success path quiet and the failure path
    actionable.
    """
    r = PrewarmReport(
        tools=[
            ToolPrewarmResult(tool_name="ok", state="ready", detail="cache miss"),
            ToolPrewarmResult(tool_name="bad", state="failed", error="conda exit 1", detail="oops"),
        ]
    )
    snap = r.snapshot()
    assert "error" not in snap["tools"][0]
    assert snap["tools"][1]["error"] == "conda exit 1"
    assert snap["all_ready"] is False


def test_fetch_tool_requirements_unwraps_inner_requirements_dict() -> None:
    """JSONB ``definition->'requirements'`` is a wrapper dict, not a list.

    Reproduces the 2026-05-15 silent-failure shape: psycopg returned
    ``{"containers": [], "requirements": [{...}]}`` and the old
    ``list(requirements_json)`` yielded the dict's KEYS — shipping
    ``['containers', 'requirements']`` to the subprocess where
    ``Requirement(**'containers')`` raised TypeError. The fix
    unwraps the nested ``requirements`` key; this test pins the
    correct behavior so a future refactor cannot silently regress
    the shape extraction.

    We mock psycopg here because we are testing PURE shape extraction
    on a fixture dict — the live integration validation (run against
    rhea Postgres on 2026-05-15) covered the real psycopg path.
    """
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    from apecx_integration.infrastructure.rhea_prewarm import _fetch_tool_requirements

    # Fixture: shape psycopg actually returns for muscle (verified
    # live against Rhea's galaxytools.muscle row, 2026-05-15).
    rhea_jsonb_payload = {
        "containers": [],
        "requirements": [{"type": "package", "value": "muscle", "version": "3.8.1551"}],
    }
    fake_cur = MagicMock()
    fake_cur.execute = AsyncMock()
    fake_cur.fetchone = AsyncMock(return_value=(rhea_jsonb_payload,))
    # Make cur usable as an async context manager.
    fake_cur.__aenter__ = AsyncMock(return_value=fake_cur)
    fake_cur.__aexit__ = AsyncMock(return_value=False)

    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cur)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=False)

    # psycopg.AsyncConnection.connect is awaited then used as an
    # async context manager — match that by returning an awaitable
    # that resolves to the context-managed connection.
    async def _fake_connect(*args, **kwargs):
        return fake_conn

    psycopg_stub = MagicMock()
    psycopg_stub.AsyncConnection.connect = _fake_connect
    with patch.dict(sys.modules, {"psycopg": psycopg_stub}):
        out = asyncio.run(_fetch_tool_requirements("postgresql://x/y", "muscle"))
    assert out == [{"type": "package", "value": "muscle", "version": "3.8.1551"}], (
        "extracted requirements list must be the inner 'requirements' key, "
        f"not the wrapper dict's keys; got {out!r}"
    )


def test_fetch_tool_requirements_rejects_wrapper_without_requirements_key() -> None:
    """Unknown JSONB shape FAIL-LOUDs with a specific, actionable message.

    Anti-silent-failure: an operator who somehow lands an unexpected
    JSONB shape in galaxytools (schema drift, manual edit, mid-flight
    migration) must see a clear error AT PREWARM TIME, not a
    confusing downstream ``TypeError`` in the rhea-venv subprocess.
    """
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    from apecx_integration.infrastructure.rhea_prewarm import _fetch_tool_requirements

    bogus_payload = {"containers": [], "other_key": []}  # missing 'requirements'
    fake_cur = MagicMock()
    fake_cur.execute = AsyncMock()
    fake_cur.fetchone = AsyncMock(return_value=(bogus_payload,))
    fake_cur.__aenter__ = AsyncMock(return_value=fake_cur)
    fake_cur.__aexit__ = AsyncMock(return_value=False)
    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cur)
    fake_conn.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_conn.__aexit__ = AsyncMock(return_value=False)

    async def _fake_connect(*args, **kwargs):
        return fake_conn

    psycopg_stub = MagicMock()
    psycopg_stub.AsyncConnection.connect = _fake_connect
    with (
        patch.dict(sys.modules, {"psycopg": psycopg_stub}),
        pytest.raises(RuntimeError) as exc_info,
    ):
        asyncio.run(_fetch_tool_requirements("postgresql://x/y", "muscle"))
    err = str(exc_info.value)
    assert "unexpected requirements shape" in err
    assert "muscle" in err
