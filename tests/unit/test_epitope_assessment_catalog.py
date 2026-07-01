"""#2 (2026-07-01) — the two epitope follow-up workflows are cataloged (tuned + typed schema).

``conserved_epitope_candidate_assessment`` and ``epitope_combination_feasibility_assessment`` were
runnable but had NO ``mcp_workflow_catalog.yml`` entry -> empty ``input_schema {}`` + a one-line AST
description, so the frontier LLM had no signal to pick them or pass their evidence-handle +
approval-token params. These pin the curated catalog entries: ``tuned=True``, a typed ``input_schema``
surfacing the load-bearing params, a "USE IT FOR" description, and a ``source.function`` that actually
imports + builds.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from apecx_integration.mcp_surface.tools import discovery

_NAMES = (
    "conserved_epitope_candidate_assessment",
    "epitope_combination_feasibility_assessment",
)


def _runnable_by_name() -> dict:
    out = asyncio.run(discovery.list_workflows())
    return {r["name"]: r for r in out["runnable"]}


@pytest.mark.parametrize("name", _NAMES)
def test_assessment_entry_is_tuned_with_typed_schema(name):
    row = _runnable_by_name()[name]
    assert row["tuned"] is True, f"{name} is not a tuned catalog entry (still AST-derived)"
    props = (row.get("input_schema") or {}).get("properties") or {}
    assert props, f"{name} has an empty input_schema — the LLM can't tell what params to pass"
    assert "USE IT FOR" in (row.get("description") or ""), f"{name} lacks a curated USE-IT-FOR desc"


def test_candidate_assessment_schema_names_evidence_handle_and_approval_token():
    props = _runnable_by_name()["conserved_epitope_candidate_assessment"]["input_schema"][
        "properties"
    ]
    # The two load-bearing params the workflow needs (documented in the plan): an upstream evidence
    # handle + an approval token to release peptide sequences.
    assert "evidence_data_handle" in props
    assert "design_approval_id" in props


def test_combination_feasibility_schema_names_evidence_and_approval():
    props = _runnable_by_name()["epitope_combination_feasibility_assessment"]["input_schema"][
        "properties"
    ]
    assert {"evidence_data_handle", "candidate_assessment_handle", "design_approval_id"} <= set(
        props
    )


@pytest.mark.parametrize(
    "module,function",
    [
        (
            "apecx_integration.composition.workflows.conserved_epitope_candidate_assessment.builder",
            "build_conserved_epitope_candidate_assessment_workflow",
        ),
        (
            "apecx_integration.composition.workflows.epitope_combination_feasibility_assessment.builder",
            "build_epitope_combination_feasibility_assessment_workflow",
        ),
    ],
)
def test_catalog_source_function_imports_and_builds(module, function):
    # Guards a typo'd module path / function name in the catalog source: the entry must actually
    # resolve + build a workflow with child steps.
    fn = getattr(importlib.import_module(module), function)
    wf = fn()
    assert wf.child_steps, f"{function} built a workflow with no steps"
