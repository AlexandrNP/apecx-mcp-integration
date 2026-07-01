"""protein_name_normalization — resolve a user protein name to the BV-BRC product term.

A tiny lightweight workflow wrapping ``ProteinNameNormalizationStep``.

``build_protein_name_normalization_core_workflow`` is the single builder — the NESTING variant with
NO terminal EnvelopeStep: it ends at the ``normalize`` step, so ``workflow_output`` is the plain
``{taxon_id, protein, feature_type, ...}`` dict. That plain dict (a) composes cleanly inside a
``SubworkflowStep`` — a terminal EnvelopeStep's ``WorkflowResult.status: 'ok'`` would collide with
``SubworkflowStep``'s own ``status`` gate — and (b) is directly usable for standalone
tests/debugging via ``build_...core_workflow().run({"norm_in": {...}})``. There is no
EnvelopeStep/catalog variant: a ``taxon_id``-required, no-free-text tool is the exact anti-pattern
that retired ``viral_conserved_sites`` from the desktop surface (2026-06-16, see
``mcp_workflow_catalog.yml``), so this is deliberately NOT registered as an MCP tool — the flat-dict
core covers both nesting and direct invocation.

The inner first-step input DU is ``norm_in`` — it MUST differ from the embedding ``SubworkflowStep``'s
own input DU (``fetch_in`` in the conserved-sites builder) per the G117 step-vs-inner naming rule.
The DirectLink inherits ``auto_transfer: true`` from ``config_version 2``.
"""

from __future__ import annotations

from typing import Any

_DU = "nanobrain.core.data_unit.DataUnitMemory"
_TRIGGER = "nanobrain.core.trigger.DataUnitChangeTrigger"
_STEPS = "apecx_integration.composition.steps"


def _du(name: str) -> dict[str, Any]:
    return {name: {"class": _DU, "name": name}}


def _trig(du: str) -> list[dict[str, Any]]:
    return [{"class": _TRIGGER, "data_unit": du}]


def _normalization_builder():
    """Shared workflow_input → normalize cascade (no terminal step / output bound)."""
    from nanobrain.lightweight.workflow_builder import WorkflowBuilder

    b = WorkflowBuilder(
        "protein_name_normalization",
        "Resolve a user protein name to the BV-BRC product term for a taxon (token-subset match).",
    )
    b.add_input("workflow_input", "DataUnitMemory")
    b.add_step(
        "normalize",
        f"{_STEPS}.protein_name_normalization_step.ProteinNameNormalizationStep",
        input_data_units=_du("norm_in"),
        output_data_units=_du("norm_out"),
        triggers=_trig("norm_in"),
    )
    b.add_link("workflow_input", "normalize.norm_in", link_type="direct")
    return b


def build_protein_name_normalization_core_workflow():
    """No-arg builder: ``workflow_output`` is the ``normalize`` step's dict directly (no EnvelopeStep)."""
    b = _normalization_builder()
    b.add_output("workflow_output", "DataUnitMemory")
    b.add_link("normalize.norm_out", "workflow_output", link_type="direct")
    return b.load()


__all__ = ["build_protein_name_normalization_core_workflow"]
