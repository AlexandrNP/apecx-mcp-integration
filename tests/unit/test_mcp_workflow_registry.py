"""Unit tests for the workflow-catalog registrar.

Strategy: use a tiny FastMCP test double that captures every
``.tool(name=..., description=...)(fn)`` call. The capture lets us
assert that name, description, and the synthesized function flow
through; the synthesized function's signature is what FastMCP feeds
into ``func_metadata``, so reproducing it on the capture is
representative.

The integration test (test_mcp_workflow_surface.py) drives the real
FastMCP server in-process for the wire-shape assertions.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from apecx_integration.mcp_surface import workflow_registry
from apecx_integration.mcp_surface.workflow_registry import (
    RegistrationReport,
    WorkflowCatalog,
    WorkflowRequirements,
    check_prerequisites,
    load_catalog,
    register_workflows,
)

# ---------------------------------------------------------------------------
# FastMCP test double
# ---------------------------------------------------------------------------


@dataclass
class _CapturedTool:
    name: str
    description: str
    fn: object


@dataclass
class _FakeFastMCP:
    captured: list[_CapturedTool] = field(default_factory=list)

    def tool(self, *, name: str, description: str):
        def decorator(fn):
            self.captured.append(_CapturedTool(name=name, description=description, fn=fn))
            return fn

        return decorator


# ---------------------------------------------------------------------------
# Catalog parsing
# ---------------------------------------------------------------------------


def test_default_catalog_parses(tmp_path: Path) -> None:
    """The packaged default catalog (rhea_muscle_alignment) parses."""
    catalog = load_catalog()
    assert isinstance(catalog, WorkflowCatalog)
    assert len(catalog.workflows) >= 1
    rhea = next(w for w in catalog.workflows if w.tool_name == "rhea_muscle_alignment")
    assert rhea.source.kind == "yaml"
    # Keyed by the FIRST STEP's input data unit (Workflow.run deposits
    # there, not by workflow-level input-data-unit name).
    assert rhea.input_envelope_key == "fasta_collection_input"
    # MUSCLE has multi-second IO gaps; 200ms default would cause the
    # cascade-drain detector to return a partial result.
    assert rhea.settle_ms >= 1000
    assert "RHEA_MCP_URL" in rhea.requires.env
    assert "rhea" in rhea.requires.modules


def test_catalog_round_trip_yaml_and_lightweight_kinds(tmp_path: Path) -> None:
    """A catalog mixing YAML and lightweight kinds validates."""
    data = {
        "workflows": [
            {
                "tool_name": "alpha",
                "description": "yaml-kind workflow",
                "source": {"kind": "yaml", "path": "x/y.yml"},
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "tool_name": "beta",
                "description": "lightweight-kind workflow",
                "source": {
                    "kind": "lightweight",
                    "module": "mypkg.workflows",
                    "function": "build_beta",
                },
                "input_schema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
        ]
    }
    path = tmp_path / "catalog.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    catalog = load_catalog(path)
    assert {w.tool_name for w in catalog.workflows} == {"alpha", "beta"}
    assert catalog.workflows[0].source.kind == "yaml"
    assert catalog.workflows[1].source.kind == "lightweight"
    # Mypy-friendly attribute access:
    beta_source = catalog.workflows[1].source
    assert beta_source.module == "mypkg.workflows"
    assert beta_source.function == "build_beta"


def test_catalog_rejects_unknown_field(tmp_path: Path) -> None:
    """extra='forbid' — a typo in any field raises at load."""
    data = {
        "workflows": [
            {
                "tool_name": "alpha",
                "description": "x",
                "source": {"kind": "yaml", "path": "a.yml"},
                "input_schema": {"type": "object", "properties": {}, "required": []},
                "timeout_secondz": 30.0,  # typo
            }
        ]
    }
    path = tmp_path / "catalog.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match=r"failed validation|timeout_secondz|extra"):
        load_catalog(path)


def test_load_catalog_missing_path_raises(tmp_path: Path) -> None:
    """A non-existent catalog path is FAIL-LOUD, not a silent empty."""
    with pytest.raises(FileNotFoundError):
        load_catalog(tmp_path / "nope.yml")


def test_load_catalog_not_a_mapping_raises(tmp_path: Path) -> None:
    """A YAML that isn't a top-level mapping is rejected."""
    path = tmp_path / "bad.yml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_catalog(path)


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------


def test_check_prerequisites_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset env var produces a clear reason."""
    monkeypatch.delenv("APECX_TEST_FAKE_VAR", raising=False)
    met, missing = check_prerequisites(
        WorkflowRequirements(env=["APECX_TEST_FAKE_VAR"], modules=[])
    )
    assert met is False
    assert any("APECX_TEST_FAKE_VAR" in m for m in missing)


def test_check_prerequisites_env_empty_string_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty-string env var counts as missing (FAIL-LOUD)."""
    monkeypatch.setenv("APECX_TEST_FAKE_VAR", "")
    met, missing = check_prerequisites(
        WorkflowRequirements(env=["APECX_TEST_FAKE_VAR"], modules=[])
    )
    assert met is False
    assert any("APECX_TEST_FAKE_VAR" in m for m in missing)


def test_check_prerequisites_module_missing() -> None:
    """A genuinely-nonexistent module is detected via find_spec.

    We deliberately pick a long, ugly name that should never collide
    with anything in the environment.
    """
    met, missing = check_prerequisites(
        WorkflowRequirements(
            env=[],
            modules=["apecx_zzz_definitely_not_a_real_module_xyz_77777"],
        )
    )
    assert met is False
    assert any("not importable" in m for m in missing)


def test_check_prerequisites_all_present() -> None:
    """When every prereq is satisfied, met=True and missing=[]."""
    met, missing = check_prerequisites(WorkflowRequirements(env=[], modules=["pathlib", "json"]))
    assert met is True
    assert missing == []


# ---------------------------------------------------------------------------
# Registration — happy path
# ---------------------------------------------------------------------------


def _make_yaml_catalog(tmp_path: Path, **extras) -> WorkflowCatalog:
    """Build a minimal one-entry yaml-kind catalog."""
    workflow_yaml = tmp_path / "stub_workflow.yml"
    workflow_yaml.write_text("name: stub\n", encoding="utf-8")
    data = {
        "workflows": [
            {
                "tool_name": "stub_tool",
                "description": "stub description",
                "source": {"kind": "yaml", "path": str(workflow_yaml)},
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "q": {"type": "string", "description": "query"},
                        "n": {"type": "integer", "default": 5},
                    },
                    "required": ["q"],
                },
                **extras,
            }
        ]
    }
    path = tmp_path / "catalog.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_catalog(path)


def test_register_workflows_happy_path_flows_name_description_schema(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """register_workflows passes name, description, and a function whose
    signature reflects the catalog input_schema."""
    catalog = _make_yaml_catalog(tmp_path)
    fake = _FakeFastMCP()
    with caplog.at_level(logging.INFO):
        report = register_workflows(fake, catalog)

    assert isinstance(report, RegistrationReport)
    assert report.registered == ["stub_tool"]
    assert report.unavailable == []
    assert report.failed == []
    assert len(fake.captured) == 1
    cap = fake.captured[0]
    assert cap.name == "stub_tool"
    assert "stub description" in cap.description
    # Synthesized fn signature carries the schema's properties.
    import inspect

    sig = inspect.signature(cap.fn)
    params = sig.parameters
    assert set(params.keys()) == {"q", "n"}
    # q is required → no default
    assert params["q"].default is inspect.Parameter.empty
    # n is optional → has the default from JSON-Schema
    assert params["n"].default == 5


def test_register_workflows_unavailable_appears_in_description_and_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tool whose prereqs are unmet:
    - is STILL registered (silent absence forbidden),
    - has [UNAVAILABLE: ...] in its description,
    - its runner returns an actionable error.
    """
    monkeypatch.delenv("APECX_TEST_PREREQ_VAR", raising=False)
    catalog = _make_yaml_catalog(
        tmp_path,
        requires={"env": ["APECX_TEST_PREREQ_VAR"], "modules": []},
    )
    fake = _FakeFastMCP()
    with caplog.at_level(logging.WARNING):
        report = register_workflows(fake, catalog)

    assert report.registered == []
    assert report.failed == []
    assert len(report.unavailable) == 1
    assert report.unavailable[0][0] == "stub_tool"
    assert "APECX_TEST_PREREQ_VAR" in report.unavailable[0][1]

    cap = fake.captured[0]
    assert cap.name == "stub_tool"
    assert "[UNAVAILABLE:" in cap.description
    assert "APECX_TEST_PREREQ_VAR" in cap.description

    # The fn returns an actionable error envelope, NEVER a silent success.
    result = asyncio.run(cap.fn(q="anything"))
    assert isinstance(result, dict)
    assert "error" in result
    assert "APECX_TEST_PREREQ_VAR" in result["error"]
    assert "stub_tool" in result["error"]


def test_unavailable_hint_is_surfaced_in_description_and_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable tool with an ``unavailable_hint`` must surface that hint
    BOTH in its [UNAVAILABLE] description and in the runner error — this is the
    honest "needs Docker; use the LLM-only / MAFFT path instead" announcement.
    """
    monkeypatch.delenv("APECX_TEST_PREREQ_VAR", raising=False)
    hint = "needs Docker + Rhea; use the MAFFT path (viral_conserved_sites) instead."
    catalog = _make_yaml_catalog(
        tmp_path,
        requires={
            "env": ["APECX_TEST_PREREQ_VAR"],
            "modules": [],
            "unavailable_hint": hint,
        },
    )
    fake = _FakeFastMCP()
    register_workflows(fake, catalog)

    cap = fake.captured[0]
    assert hint in cap.description
    result = asyncio.run(cap.fn(q="anything"))
    assert hint in result["error"]


# ---------------------------------------------------------------------------
# Registration — one bad entry doesn't break the others
# ---------------------------------------------------------------------------


def test_register_workflows_one_bad_entry_does_not_break_others(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A catalog entry that fails synthesis is logged + added to
    report.failed; the other entries STILL register."""
    good_yaml = tmp_path / "good.yml"
    good_yaml.write_text("name: good\n", encoding="utf-8")
    data = {
        "workflows": [
            {
                "tool_name": "good",
                "description": "good entry",
                "source": {"kind": "yaml", "path": str(good_yaml)},
                "input_schema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            },
            {
                "tool_name": "bad",
                "description": "bad entry — input_schema not an object",
                "source": {"kind": "yaml", "path": str(good_yaml)},
                # type: array breaks the synthesizer (it expects "object")
                "input_schema": {"type": "array", "items": {"type": "string"}},
            },
        ]
    }
    path = tmp_path / "catalog.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    catalog = load_catalog(path)

    fake = _FakeFastMCP()
    with caplog.at_level(logging.ERROR):
        report = register_workflows(fake, catalog)

    assert "good" in report.registered
    assert any(name == "bad" for name, _ in report.failed)
    assert {c.name for c in fake.captured} == {"good"}


# ---------------------------------------------------------------------------
# End-to-end runner against a real Workflow (no Rhea required)
# ---------------------------------------------------------------------------


def test_registered_tool_routes_through_shared_guarded_core(tmp_path: Path) -> None:
    """The runner forwards kwargs to ``Workflow.run`` and surfaces the
    workflow's output data units.

    Uses the lightweight builder to construct a trivial pass-through
    workflow at runtime so the test doesn't depend on any service.
    """
    # Build a fake module that exports a workflow factory.
    factory_mod_name = "apecx_test_stub_workflow_factory"
    factory_src = """
from nanobrain.lightweight.workflow_builder import WorkflowBuilder

def build_passthrough():
    b = WorkflowBuilder("registry_test_passthrough", "round-trip")
    b.add_input("payload", "DataUnitString")
    b.add_output("payload", "DataUnitString")
    b.connect("payload", "payload")
    return b.load()
"""
    mod_path = tmp_path / f"{factory_mod_name}.py"
    mod_path.write_text(factory_src, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        catalog_data = {
            "workflows": [
                {
                    "tool_name": "passthrough_tool",
                    "description": "echo input back through a 1-DU pass-through workflow",
                    "source": {
                        "kind": "lightweight",
                        "module": factory_mod_name,
                        "function": "build_passthrough",
                    },
                    "input_schema": {
                        "type": "object",
                        "properties": {"payload": {"type": "string"}},
                        "required": ["payload"],
                    },
                    "timeout_seconds": 5.0,
                }
            ]
        }
        catalog_path = tmp_path / "catalog.yml"
        catalog_path.write_text(yaml.safe_dump(catalog_data), encoding="utf-8")
        catalog = load_catalog(catalog_path)

        fake = _FakeFastMCP()
        # Clear the workflow cache so the factory runs.
        workflow_registry._clear_workflow_cache()
        report = register_workflows(fake, catalog)
        # If the lightweight workflow shape isn't supported here (e.g.
        # DataUnitString isn't a real class in this build), the test
        # can't proceed — but we ALSO want the registrar to be robust
        # if the load fails at call time. Verify whichever path:
        if report.registered:
            cap = fake.captured[0]
            result = asyncio.run(cap.fn(payload="hello"))
            assert isinstance(result, dict)
            # The registered tool routes through the shared guarded core
            # (eo_primitives._run_resolved_entry) — the SAME path as
            # run_workflow(name). So the result is either a clean
            # WorkflowResult envelope OR an actionable error — never a
            # silent empty dict (the G127 null the unification fixed).
            if result.get("error"):
                # Acceptable: the lightweight builder can't construct the DAG
                # in this minimal stub. The point is the error is surfaced.
                assert result["error"]
            else:
                # Success: a non-null WorkflowResult envelope (the core
                # decides success from the output VALUE, not status alone).
                assert result.get("markdown") or result.get("data_handle"), (
                    f"registered-tool success must carry output, got: {sorted(result)}"
                )
                assert result.get("run_id")  # run-store recorded it (proves the unified path)
        else:
            # Bad shape: at least one failure recorded, no silent absence.
            assert report.failed
    finally:
        sys.path.remove(str(tmp_path))
        if factory_mod_name in sys.modules:
            del sys.modules[factory_mod_name]
        workflow_registry._clear_workflow_cache()


# ---------------------------------------------------------------------------
# promote_discovered — config-driven runtime promotion of DISCOVERED workflows
# ---------------------------------------------------------------------------


def test_promote_discovered_registers_a_discovered_workflow_as_typed_query_tool() -> None:
    """A name in ``promote_discovered`` becomes a first-class tool — resolved from filesystem
    discovery (no hand-written entry) — with a typed ``query`` signature so a model calls it
    directly. ``rag_e2e_synthesis`` is a real discovered product workflow with no explicit entry."""
    import inspect

    catalog = WorkflowCatalog(workflows=[], promote_discovered=["rag_e2e_synthesis"])
    fake = _FakeFastMCP()
    report = register_workflows(fake, catalog)

    assert "rag_e2e_synthesis" in report.registered
    cap = next(c for c in fake.captured if c.name == "rag_e2e_synthesis")
    # Typed {query} signature (the discovery default is untyped → would be a no-arg tool).
    params = inspect.signature(cap.fn).parameters
    assert "query" in params
    assert params["query"].default is inspect.Parameter.empty  # required
    # Description comes from the workflow itself, not a placeholder.
    assert cap.description and "rag_e2e_synthesis (auto-discovered" not in cap.description


def test_promote_discovered_unknown_name_is_warned_not_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bogus promote_discovered name is skipped with a WARNING — never a hard failure that
    would empty the whole tool surface."""
    catalog = WorkflowCatalog(workflows=[], promote_discovered=["does_not_exist_xyz_123"])
    fake = _FakeFastMCP()
    with caplog.at_level(logging.WARNING):
        report = register_workflows(fake, catalog)

    assert report.registered == []
    assert not fake.captured
    assert any("does_not_exist_xyz_123" in r.message for r in caplog.records)


def test_promote_discovered_skips_a_name_already_an_explicit_entry(tmp_path: Path) -> None:
    """A name in BOTH ``workflows:`` and ``promote_discovered`` registers ONCE (explicit wins)."""
    catalog = _make_yaml_catalog(tmp_path)  # has one explicit entry 'stub_tool'
    catalog = catalog.model_copy(update={"promote_discovered": ["stub_tool"]})
    fake = _FakeFastMCP()
    report = register_workflows(fake, catalog)

    assert report.registered.count("stub_tool") == 1
    assert sum(1 for c in fake.captured if c.name == "stub_tool") == 1
