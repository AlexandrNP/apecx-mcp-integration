"""EO thin-surface primitives (EO-03/04/05): run_workflow / inspect_run / inspect_workflow /
apecx_context, plus the session run store.

The live ``run_workflow`` test drives a REAL workflow built with the lightweight
``WorkflowBuilder`` (one of the framework-native authoring paths) and run via real
``Workflow.run`` — no mock of the run itself. Only the catalog lookup is redirected (the
test workflow isn't in the packaged catalog), which is the legitimate seam.
"""

from __future__ import annotations

import asyncio

import pytest
from nanobrain.core.step import BaseStep, StepConfig
from nanobrain.lightweight.workflow_builder import WorkflowBuilder

from apecx_integration.composition.runtime.run_store import RunStore, get_run_store
from apecx_integration.mcp_surface import workflow_registry
from apecx_integration.mcp_surface.tools import eo_primitives


# --------------------------------------------------------------------------- #
# A module-level step so from_config can resolve it by dotted path. It emits a
# WorkflowResult-shaped output into the workflow's output DU, exercising
# run_workflow's envelope-extraction path end to end.
# --------------------------------------------------------------------------- #
class _EnvelopeEmitStep(BaseStep):
    COMPONENT_TYPE = "test_eo_envelope_emit"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data, **kw):
        from apecx_integration.composition.schemas.workflow_result import WorkflowResult

        wr = WorkflowResult(markdown="# done\nconserved sites: 3")
        return {"emit_out": wr.model_dump(mode="json")}


def _build_test_workflow():
    b = WorkflowBuilder("eo_test_wf", "EO primitive live-run fixture")
    b.add_input("wf_in", "DataUnitMemory")
    b.add_output("wf_out", "DataUnitMemory")
    b.add_step(
        "emit",
        f"{__name__}._EnvelopeEmitStep",
        input_data_units={
            "emit_in": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "emit_in"}
        },
        output_data_units={
            "emit_out": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "emit_out"}
        },
        triggers=[
            {"class": "nanobrain.core.trigger.DataUnitChangeTrigger", "data_unit": "emit_in"}
        ],
    )
    b.add_link("wf_in", "emit.emit_in", link_type="direct")
    b.add_link("emit.emit_out", "wf_out", link_type="direct")
    return b.load()


def _test_catalog():
    from apecx_integration.mcp_surface.workflow_registry import (
        WorkflowCatalog,
        WorkflowCatalogEntry,
        WorkflowSourceYAML,
    )

    return WorkflowCatalog(
        workflows=[
            WorkflowCatalogEntry(
                tool_name="eo_test_wf",
                description="EO primitive live-run fixture",
                source=WorkflowSourceYAML(kind="yaml", path="unused-monkeypatched.yml"),
                input_schema={"type": "object", "properties": {}, "required": []},
                input_envelope_key="wf_in",
            )
        ]
    )


@pytest.fixture(autouse=True)
def _clean_state():
    get_run_store().clear()
    yield
    get_run_store().clear()


# --------------------------------------------------------------------------- #
# Run store (real, in-memory)
# --------------------------------------------------------------------------- #
def test_run_store_record_get_roundtrip():
    from apecx_integration.composition.runtime.provenance_wiring import RunSummary

    store = RunStore()
    rec = store.record(
        workflow_name="w",
        status="completed",
        run_summary=RunSummary(steps=[]),
        workflow_result=None,
    )
    assert store.get(rec.run_id) is rec
    assert store.get("nope") is None
    assert [r.run_id for r in store.session_runs()] == [rec.run_id]


def test_run_store_session_runs_ordered():
    from apecx_integration.composition.runtime.provenance_wiring import RunSummary

    store = RunStore()
    a = store.record(
        workflow_name="a",
        status="completed",
        run_summary=RunSummary(steps=[]),
        workflow_result=None,
    )
    b = store.record(
        workflow_name="b",
        status="completed",
        run_summary=RunSummary(steps=[]),
        workflow_result=None,
    )
    assert [r.workflow_name for r in store.session_runs()] == ["a", "b"]
    assert a.order < b.order


# --------------------------------------------------------------------------- #
# run_workflow — live (real WorkflowBuilder workflow, real Workflow.run)
# --------------------------------------------------------------------------- #
def test_run_workflow_live_returns_envelope_and_records_run(monkeypatch):
    wf = _build_test_workflow()
    monkeypatch.setattr(workflow_registry, "load_catalog", _test_catalog)
    monkeypatch.setattr(workflow_registry, "_load_workflow_for_entry", lambda entry: wf)

    out = asyncio.run(eo_primitives.run_workflow("eo_test_wf", {"q": "EEEV"}))
    # The workflow emitted a WorkflowResult envelope; run_workflow surfaced it + a run_id.
    assert out["status"] == "ok"
    assert "conserved sites" in out["markdown"]
    assert out["error"] is None
    assert isinstance(out["run_id"], str) and out["run_id"]

    # The run was recorded and is inspectable.
    run_id = out["run_id"]
    inspected = asyncio.run(eo_primitives.inspect_run(run_id))
    assert inspected["run_id"] == run_id
    assert inspected["workflow_name"] == "eo_test_wf"
    assert inspected["status"] in {"completed", "completed_no_await"}
    assert "summary" in inspected and "steps" in inspected["summary"]

    # apecx_context reflects the session run.
    ctx = asyncio.run(eo_primitives.apecx_context())
    assert ctx["n_runs"] == 1
    assert ctx["runs"][0]["run_id"] == run_id
    assert ctx["runs"][0]["workflow_name"] == "eo_test_wf"


# --------------------------------------------------------------------------- #
# run_workflow — error gates (no silent success)
# --------------------------------------------------------------------------- #
def test_run_workflow_unknown_name_is_loud_error():
    out = asyncio.run(eo_primitives.run_workflow("does_not_exist"))
    assert out["status"] == "error"
    assert "unknown workflow" in out["error"]
    assert "does_not_exist" in out["error"]


def test_run_workflow_blank_name_is_error():
    out = asyncio.run(eo_primitives.run_workflow("   "))
    assert out["status"] == "error"
    assert "name" in out["error"]


def test_run_workflow_non_dict_params_is_error():
    out = asyncio.run(eo_primitives.run_workflow("eo_test_wf", params="not-a-dict"))  # type: ignore[arg-type]
    assert out["status"] == "error"


# --------------------------------------------------------------------------- #
# inspect_run — error path
# --------------------------------------------------------------------------- #
def test_inspect_run_unknown_id_is_loud():
    out = asyncio.run(eo_primitives.inspect_run("deadbeef"))
    assert "error" in out
    assert "unknown run_id" in out["error"]


# --------------------------------------------------------------------------- #
# inspect_workflow — real static inspection of a packaged catalog workflow
# (rhea_muscle_alignment; static YAML walk, no RHEA backend needed)
# --------------------------------------------------------------------------- #
def test_inspect_workflow_static_real_catalog_entry():
    out = asyncio.run(eo_primitives.inspect_workflow("rhea_muscle_alignment"))
    assert "error" not in out, out
    # WorkflowInspection dump carries the workflow name + a steps list.
    assert "steps" in out
    assert isinstance(out["steps"], list) and out["steps"]


def test_inspect_workflow_unknown_name_is_error():
    out = asyncio.run(eo_primitives.inspect_workflow("nope_wf"))
    assert "error" in out
    assert "unknown workflow" in out["error"]


# --------------------------------------------------------------------------- #
# apecx_context — empty session
# --------------------------------------------------------------------------- #
def test_apecx_context_empty_session():
    out = asyncio.run(eo_primitives.apecx_context())
    assert out == {"runs": [], "n_runs": 0}


# --------------------------------------------------------------------------- #
# Surface registration — the four EO primitives are on the server
# --------------------------------------------------------------------------- #
def test_build_server_registers_eo_primitives():
    from apecx_integration.mcp_surface.server import build_server

    names = {t.name for t in asyncio.run(build_server().list_tools())}
    assert {"run_workflow", "inspect_run", "inspect_workflow", "apecx_context"} <= names
