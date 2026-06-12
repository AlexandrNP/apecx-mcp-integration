"""ControlTransfer builders + invariants (RoC-1b). Pure Pydantic, no mocks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apecx_integration.composition.schemas.control_transfer import (
    ControlTransfer,
    ParamNeed,
    WorkflowNeed,
    ambiguous_entity_transfer,
    decomposition_choice_transfer,
    missing_param_transfer,
    needs_prerequisite_transfer,
)


def test_missing_param_carries_name_schema_obtain_via():
    ct = missing_param_transfer(
        [
            ParamNeed(
                param_name="taxon_id",
                param_schema={"type": "integer"},
                obtain_via="resolve virus→taxon_id via harmonized_search",
            )
        ]
    )
    assert ct.reason == "missing_param"
    p = ct.next_action.params[0]
    assert p.param_name == "taxon_id"
    assert p.param_schema == {"type": "integer"}
    assert "harmonized_search" in p.obtain_via
    assert "taxon_id" in ct.message
    # JSON round-trip (the surface serializes this).
    assert ct.model_dump(mode="json")["reason"] == "missing_param"


def test_ambiguous_entity_reproduces_disambiguation_shape():
    # The candidate list mirrors the shipped disambiguation envelope (iri/label per candidate).
    ct = ambiguous_entity_transfer(
        [
            {"iri": "http://x/A", "label": "Human RSV"},
            {"iri": "http://x/B", "label": "Bovine RSV"},
        ]
    )
    assert ct.reason == "ambiguous_entity"
    assert ct.next_action.kind == "choose_candidate"
    assert [c["iri"] for c in ct.next_action.candidates] == ["http://x/A", "http://x/B"]


def test_decomposition_choice_carries_plan():
    ct = decomposition_choice_transfer(
        [
            WorkflowNeed(
                workflow="viral_conserved_sites",
                required_inputs=["taxon_id", "protein"],
                missing=["taxon_id"],
            )
        ]
    )
    assert ct.reason == "decomposition_choice"
    w = ct.next_action.workflows[0]
    assert w.workflow == "viral_conserved_sites"
    assert w.missing == ["taxon_id"]


def test_needs_prerequisite():
    ct = needs_prerequisite_transfer("resolve the virus name to a taxon_id")
    assert ct.reason == "needs_prerequisite"
    assert ct.next_action.prerequisite == "resolve the virus name to a taxon_id"


def test_extra_forbid_rejects_typos():
    with pytest.raises(ValidationError):
        ParamNeed(param_name="x", typo_field=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ControlTransfer(
            reason="missing_param",
            next_action={"kind": "k"},
            message="m",
            bogus=1,  # type: ignore[call-arg]
        )


def test_invalid_reason_rejected():
    with pytest.raises(ValidationError):
        ControlTransfer(reason="not_a_reason", next_action={"kind": "k"}, message="m")  # type: ignore[arg-type]
