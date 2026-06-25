"""Fixtures for the G127/EF7 pinned e2e — a minimal, network-free, faithful reproduction of the scenario:
a degrade-loud TOP-LEVEL step that runs a NESTED sub-workflow whose inner step FAILS, catches the failure
(``except`` → continues), and produces output. The nested ``g127_inner_fail`` step emits a ``step_failed``
event that bubbles into ``run_summary.steps`` (exactly like ``muscle_alignment`` inside the rhea_genomic
leg), so it exercises the real run_workflow G127 classifier.

NOT a ``test_*.py`` module (the bench conftest excludes those from collection, and these are importable
component classes referenced by class-path in the lightweight builder).
"""

from __future__ import annotations

from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_SELF = "tests.integration._g127_fixtures"


def _du(name: str) -> dict[str, Any]:
    return {name: {"class": _DU, "name": name}}


def _trig(du: str) -> list[dict[str, Any]]:
    return [{"class": _TRIGGER, "data_unit": du}]


class InnerFailStep(BaseStep):
    """A nested step that always raises — the source of the nested ``step_failed`` event."""

    COMPONENT_TYPE = "g127_inner_fail_step"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        raise ValueError("g127 test: inner nested step always fails")


class CatchingParentStep(BaseStep):
    """Runs a NESTED sub-workflow whose inner step fails, CATCHES it (degrade-loud), and continues with a
    real output — mirrors RheaGenomicAnalysisStep's contract. The inner step's ``step_failed`` bubbles into
    the outer run_summary; with the G127 fix it must NOT fail the run (parent caught it + an envelope was
    produced)."""

    COMPONENT_TYPE = "g127_catching_parent_step"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        from nanobrain.lightweight.workflow_builder import WorkflowBuilder

        # Build + run a NESTED sub-workflow whose inner step raises. Workflow.run SWALLOWS the inner step
        # exception (G127) — it returns without raising, but the inner step's step_failed event bubbles into
        # the OUTER run_summary. The whole build+initialize+run is wrapped so ANY inner failure is caught
        # (degrade-loud — this step must NOT fail).
        try:
            b = WorkflowBuilder("g127_inner_wf", "nested wf whose inner step fails (g127 test)")
            b.add_input("inner_in", "DataUnitMemory")
            b.add_step(
                "g127_inner_fail",
                f"{_SELF}.InnerFailStep",
                input_data_units=_du("inner_fail_in"),
                output_data_units=_du("inner_fail_out"),
                triggers=_trig("inner_fail_in"),
            )
            b.add_output("inner_out")
            b.add_link("inner_in", "g127_inner_fail.inner_fail_in", link_type="direct")
            b.add_link("g127_inner_fail.inner_fail_out", "inner_out", link_type="direct")
            wf = b.load()
            await wf.initialize()
            await wf.run({"inner_in": {"x": 1}}, timeout=20, settle_ms=500)
        except Exception:  # noqa: BLE001 — degrade-loud is the contract under test (DO NOT fail)
            pass
        # caught the nested failure + continue with a real result (loud — markdown carries the note). The
        # EnvelopeStep requires a non-empty `markdown` to produce a WorkflowResult (the "envelope present"
        # signal the G127 fix keys off).
        # markdown-only (no `data` — EnvelopeStep's data slot is a typed DataShape, not an arbitrary dict).
        return {
            "markdown": "g127 test: degrade-loud — caught a nested step failure and continued.",
        }


def build_g127_degrade_loud_workflow():
    """[workflow_input → catching_parent (nests a failing sub-workflow, catches) → envelope]. Driven via
    run_workflow, a correct G127 check must return status=ok (the only step_failed is the caught nested one;
    the envelope IS produced)."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(
        "g127_degrade_loud",
        "G127 e2e fixture: a degrade-loud parent that catches a nested failure.",
    )
    b.add_input("workflow_input", "DataUnitMemory")
    b.add_step(
        "catching_parent",
        f"{_SELF}.CatchingParentStep",
        input_data_units=_du("parent_in"),
        output_data_units=_du("parent_out"),
        triggers=_trig("parent_in"),
    )
    b.add_step(
        "envelope",
        "apecx_integration.composition.steps.envelope_step.EnvelopeStep",
        input_data_units=_du("envelope_in"),
        output_data_units=_du("workflow_result"),
        triggers=_trig("envelope_in"),
    )
    b.add_output("workflow_output")
    b.add_link("workflow_input", "catching_parent.parent_in", link_type="direct")
    b.add_link("catching_parent.parent_out", "envelope.envelope_in", link_type="direct")
    b.add_link("envelope.workflow_result", "workflow_output", link_type="direct")
    return b.load()
