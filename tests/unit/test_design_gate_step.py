"""Unit tests for DesignGateStep — the fan-in approval gate (no network).

The gate emits ``{markdown, control_transfer?}`` for the terminal EnvelopeStep:
a ``control_transfer`` present means a ``needs_input`` disposition.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.runtime.design_approval_store import (
    get_design_approval_store,
)
from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.composition.steps.design_gate_step import DesignGateStep

_QUERY = "chikv E1"
_PROTEIN = "structural polyprotein"


@pytest.fixture(autouse=True)
def _clean_store():
    """The design-approval store is a process-wide singleton — clear it before each test so
    a token issued in one test cannot leak into another. Pin AGENT locus so these ENFORCEMENT
    tests run in the mode where the gate is fail-closed (under the default DESKTOP locus the
    gate is advisory/muted — that path is pinned by test_desktop_locus_mutes_the_gate)."""
    prev_locus = get_active_locus()
    set_active_locus(ExecutionLocus.AGENT)
    get_design_approval_store().clear()
    yield
    get_design_approval_store().clear()
    set_active_locus(prev_locus)


def _stage(tmp_path: Path) -> DesignGateStep:
    p = tmp_path / "gate.yml"
    p.write_text("name: gate_test\n")
    return DesignGateStep.from_config(str(p))


def _inp(requested="evidence_only", approval=None, md="# Evidence\n\nbody [Globus pdb:1I9G]."):
    control = {"query": _QUERY, "protein": _PROTEIN, "requested_outputs": requested}
    if approval is not None:
        control["design_approval_id"] = approval
    return {"review_in": {"markdown": md}, "control_in": control}


def _issue_and_approve() -> str:
    """Real loop: issue a token scoped to (_QUERY, _PROTEIN) and operator-approve it."""
    store = get_design_approval_store()
    token = store.request(query=_QUERY, protein=_PROTEIN)
    store.approve(token)
    return token


def test_evidence_only_passes_markdown_no_control_transfer(tmp_path):
    out = asyncio.run(_stage(tmp_path).process(_inp()))
    assert out["markdown"].startswith("# Evidence")
    assert "control_transfer" not in out  # ok disposition
    assert "design" not in out["markdown"].lower()


def test_forwards_upstream_clarification_control_transfer(tmp_path):
    """An under-specified / ambiguous entity raised at the resolve step rides the control leg
    (gate.control_in ← taxon_review output); the gate must surface it as the terminal needs_input
    (control_transfer forwarded), ahead of the design gate (the HSV-1/HSV-2 clarification, 2026-06-27)."""
    inp = _inp()
    inp["control_in"]["control_transfer"] = {
        "reason": "ambiguous_entity",
        "next_action": {"kind": "choose_candidate", "candidates": []},
        "message": "The request resolved only to an UNDER-SPECIFIED taxon — specify HSV-1 vs HSV-2.",
    }
    out = asyncio.run(_stage(tmp_path).process(inp))
    assert out["control_transfer"]["reason"] == "ambiguous_entity"
    assert "UNDER-SPECIFIED" in out["control_transfer"]["message"]
    assert out["markdown"].startswith("# Evidence")  # the degraded report still rides along


def test_design_without_approval_attaches_needs_prerequisite_keeps_evidence(tmp_path):
    out = asyncio.run(_stage(tmp_path).process(_inp(requested="evidence_plus_design")))
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    # evidence is NOT discarded on a pause (degrade-loud) + the withholding is named.
    assert "# Evidence" in out["markdown"]
    assert "WITHHELD" in out["markdown"]


def test_desktop_locus_mutes_the_gate(tmp_path):
    """Under DESKTOP locus the design gate is ADVISORY: design output is RELEASED without any
    approval token (the HITL round-trip is not interoperable with the connected LLM). The
    autouse fixture pins AGENT for the enforcement tests; this one flips to DESKTOP."""
    set_active_locus(ExecutionLocus.DESKTOP)
    out = asyncio.run(_stage(tmp_path).process(_inp(requested="evidence_plus_design")))
    assert "control_transfer" not in out  # released, not a needs_input pause
    assert "WITHHELD" not in out["markdown"]
    assert "# Evidence" in out["markdown"]  # evidence retained
    assert "design" in out["markdown"].lower()  # the design section is present


def test_design_with_validated_approval_appends_section(tmp_path):
    """A server-issued, operator-approved, scope-matching token opens the gate."""
    token = _issue_and_approve()
    out = asyncio.run(
        _stage(tmp_path).process(_inp(requested="evidence_plus_design", approval=token))
    )
    assert "control_transfer" not in out  # ok disposition
    assert "Design / optimization hypotheses (approved)" in out["markdown"]
    assert token in out["markdown"]  # approval provenance attached
    assert "# Evidence" in out["markdown"]  # evidence retained
    assert "verified server-side" in out["markdown"]  # honest: the token WAS validated


def test_fabricated_token_is_rejected_bypass_closed(tmp_path):
    """THE BYPASS GUARD: a token the server never issued must NOT open the gate, even though
    it is non-blank. (Pre-fix, ANY non-blank string opened it.)"""
    out = asyncio.run(
        _stage(tmp_path).process(
            _inp(requested="evidence_plus_design", approval="dapprv-totally-made-up")
        )
    )
    assert "control_transfer" in out  # withheld
    assert "WITHHELD" in out["markdown"]
    assert "unknown" in out["markdown"]
    assert "(approved)" not in out["markdown"]  # design section NOT emitted


def test_issued_but_unapproved_token_is_withheld(tmp_path):
    """A token that was issued but NOT yet operator-approved must not open the gate."""
    store = get_design_approval_store()
    token = store.request(query=_QUERY, protein=_PROTEIN)  # pending, NOT approved
    out = asyncio.run(
        _stage(tmp_path).process(_inp(requested="evidence_plus_design", approval=token))
    )
    assert "control_transfer" in out
    assert "pending" in out["markdown"]


def test_approved_token_for_other_request_is_withheld_scope_bound(tmp_path):
    """An approval issued for a DIFFERENT query/protein must not open THIS request."""
    store = get_design_approval_store()
    other = store.request(query="dengue NS1", protein="NS1")
    store.approve(other)
    out = asyncio.run(
        _stage(tmp_path).process(_inp(requested="evidence_plus_design", approval=other))
    )
    assert "control_transfer" in out
    assert "scope mismatch" in out["markdown"]


def test_design_without_approval_issues_a_token_to_approve(tmp_path):
    """No token supplied → withhold AND issue a fresh token the caller can get approved."""
    out = asyncio.run(_stage(tmp_path).process(_inp(requested="evidence_plus_design")))
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    assert "# Evidence" in out["markdown"]
    assert "WITHHELD" in out["markdown"]
    assert "dapprv-" in out["markdown"]  # a concrete token to approve was issued
    assert "approve_design" in out["markdown"]


def test_blank_approval_token_is_not_approval(tmp_path):
    out = asyncio.run(
        _stage(tmp_path).process(_inp(requested="evidence_plus_design", approval="   "))
    )
    assert "control_transfer" in out  # still gated


def test_missing_requested_outputs_defaults_to_evidence_only(tmp_path):
    inp = {"review_in": {"markdown": "# E\n\nx"}, "control_in": {"query": "q"}}
    out = asyncio.run(_stage(tmp_path).process(inp))
    assert "control_transfer" not in out


def test_malformed_fanin_missing_control_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"review_in": {"markdown": "x"}}))


def test_empty_evidence_markdown_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"review_in": {"markdown": ""}, "control_in": {}}))
