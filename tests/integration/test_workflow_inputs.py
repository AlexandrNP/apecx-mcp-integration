"""Derive required inputs from a workflow's own schema (RoC-2b). Builds the REAL workflow
(no network); proves the workflow — not the catalog — is the single source of truth."""

from __future__ import annotations

import pytest

from apecx_integration.mcp_surface.workflow_inputs import derive_required_inputs, find_param_gaps

pytestmark = pytest.mark.integration


def _wf():
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        build_viral_conserved_sites_workflow,
    )

    return build_viral_conserved_sites_workflow()


def test_derive_required_from_real_workflow_schema():
    d = derive_required_inputs(_wf(), "fetch_in")
    # Exactly matches the step_input_schema declared in the builder — single source of truth.
    assert set(d["required"]) == {"taxon_id", "protein"}
    assert d["properties"]["taxon_id"]["type"] == "integer"
    assert d["properties"]["protein"]["type"] == "string"
    # obtain_via hint threaded from the schema for the frontier LLM.
    assert "harmonized_search" in d["obtain_via"]["taxon_id"]


def test_derive_step_without_schema_is_empty():
    # The 'align' step declares no step_input_schema → no derivable required params (degrade, not crash).
    assert derive_required_inputs(_wf(), "align_in")["required"] == []


def test_derive_unknown_or_none_du_is_empty():
    wf = _wf()
    assert derive_required_inputs(wf, "nonexistent_du")["required"] == []
    assert derive_required_inputs(wf, None)["required"] == []


def test_find_param_gaps_missing():
    derived = {
        "required": ["taxon_id", "protein"],
        "properties": {"taxon_id": {"type": "integer"}, "protein": {"type": "string"}},
        "obtain_via": {"taxon_id": "resolve via harmonized_search"},
    }
    gaps = find_param_gaps({"protein": "E1"}, derived)
    assert [g.param_name for g in gaps] == ["taxon_id"]
    assert gaps[0].issue == "missing"
    assert "harmonized_search" in gaps[0].obtain_via


def test_find_param_gaps_illtyped_but_digit_string_ok():
    derived = {
        "required": ["taxon_id"],
        "properties": {"taxon_id": {"type": "integer"}},
        "obtain_via": {},
    }
    # A non-numeric string for an integer is ill-typed...
    gaps = find_param_gaps({"taxon_id": "not-an-int"}, derived)
    assert gaps and gaps[0].issue == "ill_typed"
    # ...but a digit string and a real int are both fine (matches the step's tolerance).
    assert find_param_gaps({"taxon_id": "37124"}, derived) == []
    assert find_param_gaps({"taxon_id": 37124}, derived) == []
