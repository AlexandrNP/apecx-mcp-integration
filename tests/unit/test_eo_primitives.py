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


class _SynthEmitStep(BaseStep):
    """Emits a rag_synthesis-shaped output ({"synthesis": "<md>"}) so we can prove the
    EO-13c terminal-EnvelopeStep wiring in a real cascade WITHOUT an LLM."""

    COMPONENT_TYPE = "test_eo_synth_emit"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data, **kw):
        return {"synthesis": "# synthesized answer\nconserved sites: 5"}


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


# --------------------------------------------------------------------------- #
# EO-13c — a workflow ending in the REAL EnvelopeStep returns a WorkflowResult
# through run_workflow. This is the rag_e2e wiring pattern (synthesis-emitter →
# EnvelopeStep(markdown_input_key=synthesis) → output) proven in a real cascade
# with NO LLM, so it runs unconditionally (not Ollama-gated).
# --------------------------------------------------------------------------- #
def test_envelope_terminated_workflow_run_yields_workflow_result(monkeypatch, tmp_path):
    env_cfg = tmp_path / "env_term.yml"
    env_cfg.write_text("name: env_term\nmarkdown_input_key: synthesis\n")

    b = WorkflowBuilder("eo13c_wf", "EO-13c envelope-terminated cascade")
    b.add_input("wf_in", "DataUnitMemory")
    b.add_output("wf_out", "DataUnitMemory")
    b.add_step(
        "synth",
        f"{__name__}._SynthEmitStep",
        input_data_units={
            "synth_in": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "synth_in"}
        },
        output_data_units={
            "synth_out": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "synth_out"}
        },
        triggers=[
            {"class": "nanobrain.core.trigger.DataUnitChangeTrigger", "data_unit": "synth_in"}
        ],
    )
    b.add_step(
        "envelope",
        "apecx_integration.composition.steps.envelope_step.EnvelopeStep",
        markdown_input_key="synthesis",
        input_data_units={
            "envelope_input": {
                "class": "nanobrain.core.data_unit.DataUnitMemory",
                "name": "envelope_input",
            }
        },
        output_data_units={
            "workflow_result": {
                "class": "nanobrain.core.data_unit.DataUnitMemory",
                "name": "workflow_result",
            }
        },
        triggers=[
            {"class": "nanobrain.core.trigger.DataUnitChangeTrigger", "data_unit": "envelope_input"}
        ],
    )
    b.add_link("wf_in", "synth.synth_in", link_type="direct")
    b.add_link("synth.synth_out", "envelope.envelope_input", link_type="direct")
    b.add_link("envelope.workflow_result", "wf_out", link_type="direct")
    wf = b.load()

    monkeypatch.setattr(workflow_registry, "load_catalog", _test_catalog_for("eo13c_wf"))
    monkeypatch.setattr(workflow_registry, "_load_workflow_for_entry", lambda entry: wf)

    out = asyncio.run(eo_primitives.run_workflow("eo13c_wf", {"q": "EEEV"}))
    assert out["status"] == "ok", out
    # The terminal EnvelopeStep wrapped rag_synthesis's "synthesis" output into the envelope.
    assert "synthesized answer" in out["markdown"]
    assert "conserved sites: 5" in out["markdown"]
    assert out["run_id"]


# --------------------------------------------------------------------------- #
# run_workflow_streamed — stage-report extraction (E2-S)
# --------------------------------------------------------------------------- #
def _step_complete(step_name: str, stage_reports: list[dict]):
    """A fake G37 step_complete StepEvent carrying an accumulated stage_reports list."""
    from nanobrain.core.step_events import StepEvent

    return StepEvent(
        event_type="step_complete",
        step_name=step_name,
        run_id="run-xyz",
        timestamp_iso="2026-06-13T00:00:00+00:00",
        payload={"outputs": {"stage_reports": stage_reports}, "duration_seconds": 0.1},
    )


def _rep(stage: str, order: int):
    return {"stage": stage, "order": order, "markdown": f"{stage}-md", "data": {"k": stage}}


def test_stage_streamer_emits_each_report_once_in_arrival_order():
    """The subscriber diffs the accumulating stage_reports list by (stage, order): one
    on_stage call per NEW report, in arrival order, no dupes — even though every event
    re-carries the full cumulative list."""
    received: list[dict] = []
    sub = eo_primitives._make_stage_streamer(received.append)

    a, b, c = _rep("context_assembly", 1), _rep("data_readiness", 0), _rep("structural_evidence", 2)
    # Accumulating lists, as the real cascade delivers them (each step re-carries the prior).
    sub(_step_complete("assemble", [a]))
    sub(_step_complete("data_readiness", [a, b]))
    sub(_step_complete("structural", [a, b, c]))

    assert [r["stage"] for r in received] == [
        "context_assembly",
        "data_readiness",
        "structural_evidence",
    ]
    # Identity fields are carried through from the emitting event.
    assert all(r["run_id"] == "run-xyz" for r in received)
    assert received[0]["step_name"] == "assemble"
    assert received[1]["step_name"] == "data_readiness"
    # Markdown + data round-trip verbatim.
    assert received[0]["markdown"] == "context_assembly-md"
    assert received[0]["data"] == {"k": "context_assembly"}


def test_stage_streamer_ignores_non_complete_and_reportless_events():
    """step_start / step_failed / a step_complete with no stage_reports → no on_stage call."""
    from nanobrain.core.step_events import StepEvent

    received: list[dict] = []
    sub = eo_primitives._make_stage_streamer(received.append)

    sub(StepEvent("step_start", "s", "r", "t", payload={"inputs": {}}))
    sub(
        StepEvent(
            "step_failed", "s", "r", "t", payload={"exception": {"type": "X", "message": "m"}}
        )
    )
    sub(_step_complete("plain", []))  # empty list
    sub(StepEvent("step_complete", "s", "r", "t", payload={"outputs": {"no_reports": True}}))
    assert received == []


def test_stage_streamer_throwing_on_stage_does_not_propagate():
    """A throwing on_stage callback is swallowed (observability != correctness): the
    subscriber returns normally so the framework's publish loop / the run is untouched."""

    def boom(_report):
        raise RuntimeError("desktop pane crashed")

    sub = eo_primitives._make_stage_streamer(boom)
    # Must NOT raise — if it did, the framework's step would see a subscriber exception.
    sub(_step_complete("assemble", [_rep("context_assembly", 1)]))


# --------------------------------------------------------------------------- #
# run_workflow_streamed — live (real cascade, no LLM): streamed == returned
# --------------------------------------------------------------------------- #
class _StageEmitStep1(BaseStep):
    COMPONENT_TYPE = "test_stage_emit_1"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data, **kw):
        from apecx_integration.composition.steps._stage_report import append_stage_report

        bundle = {"query": "x"}
        append_stage_report(bundle, stage="alpha", order=0, markdown="alpha contributed")
        return bundle


class _StageEmitStep2(BaseStep):
    COMPONENT_TYPE = "test_stage_emit_2"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data, **kw):
        from apecx_integration.composition.steps._stage_report import append_stage_report

        data = input_data
        if isinstance(data, dict) and set(data) == {"s2_in"} and isinstance(data["s2_in"], dict):
            data = data["s2_in"]
        bundle = dict(data)
        append_stage_report(bundle, stage="beta", order=1, markdown="beta contributed")
        return bundle


def _build_stage_workflow():
    b = WorkflowBuilder("eo_stage_wf", "stage-report streaming fixture")
    b.add_input("wf_in", "DataUnitMemory")
    b.add_output("wf_out", "DataUnitMemory")
    b.add_step(
        "s1",
        f"{__name__}._StageEmitStep1",
        input_data_units={
            "s1_in": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "s1_in"}
        },
        output_data_units={
            "s1_out": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "s1_out"}
        },
        triggers=[{"class": "nanobrain.core.trigger.DataUnitChangeTrigger", "data_unit": "s1_in"}],
    )
    b.add_step(
        "s2",
        f"{__name__}._StageEmitStep2",
        input_data_units={
            "s2_in": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "s2_in"}
        },
        output_data_units={
            "s2_out": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "s2_out"}
        },
        triggers=[{"class": "nanobrain.core.trigger.DataUnitChangeTrigger", "data_unit": "s2_in"}],
    )
    b.add_link("wf_in", "s1.s1_in", link_type="direct")
    b.add_link("s1.s1_out", "s2.s2_in", link_type="direct")
    b.add_link("s2.s2_out", "wf_out", link_type="direct")
    return b.load()


def test_run_workflow_streamed_streams_stages_and_returns_same_result(monkeypatch):
    """Real cascade (no LLM): run_workflow_streamed pushes alpha then beta in arrival
    order (no dupes), AND returns the SAME envelope run_workflow returns for the same run."""
    monkeypatch.setattr(workflow_registry, "load_catalog", _test_catalog_for("eo_stage_wf"))
    monkeypatch.setattr(
        workflow_registry, "_load_workflow_for_entry", lambda entry: _build_stage_workflow()
    )

    streamed: list[dict] = []
    out_streamed = asyncio.run(
        eo_primitives.run_workflow_streamed("eo_stage_wf", {"q": "x"}, streamed.append)
    )
    out_plain = asyncio.run(eo_primitives.run_workflow("eo_stage_wf", {"q": "x"}))

    # Stages streamed live, each once, in arrival (step-completion) order.
    assert [r["stage"] for r in streamed] == ["alpha", "beta"]
    assert streamed[0]["step_name"] == "s1" and streamed[1]["step_name"] == "s2"

    # The streamed run's envelope equals the headless run's, modulo the per-run run_id.
    assert out_streamed["status"] == out_plain["status"] == "ok"
    assert out_streamed["markdown"] == out_plain["markdown"]
    assert out_streamed["data_preview"] == out_plain["data_preview"]


# --------------------------------------------------------------------------- #
# run_workflow_streaming — MCP desktop transport (progress + log notifications)
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self):
        self.logs: list[dict] = []

    async def send_log_message(self, *, level, data, logger=None, related_request_id=None):
        self.logs.append({"level": level, "data": data, "logger": logger})


class _FakeContext:
    """Minimal FastMCP-Context stand-in recording the two notification channels."""

    def __init__(self):
        self.session = _FakeSession()
        self.progress: list[tuple[float, str | None]] = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append((progress, message))


def test_run_workflow_streaming_emits_progress_and_log_per_stage(monkeypatch):
    """The MCP adapter binds on_stage → ctx.report_progress + ctx.session.send_log_message,
    once per stage, in order, and returns run_workflow_streamed's result verbatim. The
    sync-subscriber → async-notification bridge (queue + drain) is exercised end to end."""
    reports = [_rep("data_readiness", 0), _rep("structural_evidence", 2)]
    for r in reports:
        r.update(step_name="step-" + r["stage"], run_id="r1")

    async def _fake_streamed(name, params, on_stage):
        for r in reports:
            on_stage(r)  # subscriber fires synchronously, as in the real run
        return {"status": "ok", "markdown": "final doc", "run_id": "r1", "error": None}

    monkeypatch.setattr(eo_primitives, "run_workflow_streamed", _fake_streamed)

    ctx = _FakeContext()
    out = asyncio.run(eo_primitives.run_workflow("eo_stage_wf", {"q": "x"}, ctx))

    # Result returned verbatim (streaming did not alter the envelope).
    assert out == {"status": "ok", "markdown": "final doc", "run_id": "r1", "error": None}
    # One progress notification per stage, increasing counter, naming the stage, in order.
    assert ctx.progress == [
        (1.0, "stage complete: data_readiness"),
        (2.0, "stage complete: structural_evidence"),
    ]
    # One structured log notification per stage carrying the full report, in order.
    assert [lg["data"]["stage"] for lg in ctx.session.logs] == [
        "data_readiness",
        "structural_evidence",
    ]
    assert all(lg["data"]["event"] == "stage_report" for lg in ctx.session.logs)
    assert ctx.session.logs[0]["data"]["markdown"] == "data_readiness-md"


def test_run_workflow_streaming_zero_stages_emits_served_from_cache(monkeypatch):
    """E4-7: an identical cached re-query deterministic-SKIPS execution → zero stages stream.
    The desktop must get ONE 'served_from_cache' notification so the pane isn't silently blank."""

    async def _fake_streamed(name, params, on_stage):
        return {
            "status": "ok",
            "markdown": "cached doc",
            "run_id": "r9",
            "error": None,
        }  # no on_stage calls

    monkeypatch.setattr(eo_primitives, "run_workflow_streamed", _fake_streamed)
    ctx = _FakeContext()
    out = asyncio.run(eo_primitives.run_workflow("eo_stage_wf", {"q": "x"}, ctx))

    assert out["status"] == "ok" and out["markdown"] == "cached doc"
    assert ctx.progress == []  # no per-stage progress (nothing executed)
    events = [lg["data"]["event"] for lg in ctx.session.logs]
    assert events == ["served_from_cache"]  # exactly one, and only this


def test_run_workflow_streaming_with_stages_emits_no_cache_notification(monkeypatch):
    """When stages DO stream, the served_from_cache notification must NOT fire."""
    rep = _rep("data_readiness", 0)
    rep.update(step_name="s", run_id="r1")

    async def _fake_streamed(name, params, on_stage):
        on_stage(rep)
        return {"status": "ok", "markdown": "fresh doc", "run_id": "r1", "error": None}

    monkeypatch.setattr(eo_primitives, "run_workflow_streamed", _fake_streamed)
    ctx = _FakeContext()
    asyncio.run(eo_primitives.run_workflow("eo_stage_wf", {"q": "x"}, ctx))
    assert "served_from_cache" not in [lg["data"]["event"] for lg in ctx.session.logs]


def test_run_workflow_without_ctx_runs_headless_no_streaming(monkeypatch):
    """ctx=None → headless path (resolve + _run_resolved_entry); the streaming impl is
    NOT invoked. (Merged tool: streaming is a desktop-only branch keyed on ctx.)"""

    async def _boom_stream(*a, **k):
        raise AssertionError("_run_workflow_streaming_impl must not run when ctx is None")

    async def _fake_resolved(entry, params=None):
        return {"status": "ok", "markdown": "doc", "run_id": "r2", "error": None}

    monkeypatch.setattr(eo_primitives, "_run_workflow_streaming_impl", _boom_stream)
    monkeypatch.setattr(eo_primitives, "_run_resolved_entry", _fake_resolved)
    # A real, resolvable catalog workflow so resolution succeeds before the run.
    out = asyncio.run(eo_primitives.run_workflow("viral_conserved_sites", {"taxon_id": 1}))
    assert out["status"] == "ok" and out["markdown"] == "doc"


def test_build_server_registers_single_run_workflow_tool():
    """The split is gone: run_workflow is the ONE workflow-run tool; the old
    run_workflow_streaming tool is no longer exposed."""
    from apecx_integration.mcp_surface.server import build_server

    names = {t.name for t in asyncio.run(build_server().list_tools())}
    assert "run_workflow" in names
    assert "run_workflow_streaming" not in names


def _test_catalog_for(name: str):
    from apecx_integration.mcp_surface.workflow_registry import (
        WorkflowCatalog,
        WorkflowCatalogEntry,
        WorkflowSourceYAML,
    )

    def _loader():
        return WorkflowCatalog(
            workflows=[
                WorkflowCatalogEntry(
                    tool_name=name,
                    description="fixture",
                    source=WorkflowSourceYAML(kind="yaml", path="unused.yml"),
                    input_schema={"type": "object", "properties": {}, "required": []},
                    input_envelope_key="wf_in",
                )
            ]
        )

    return _loader
