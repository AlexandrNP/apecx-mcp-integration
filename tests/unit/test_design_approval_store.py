"""Unit tests for the fail-closed, scope-bound DesignApprovalStore (2026-06-14)."""

import pytest

from apecx_integration.composition.runtime.design_approval_store import (
    DesignApprovalStore,
    get_design_approval_store,
)

_Q = "conserved chikungunya structural epitopes"
_P = "structural polyprotein"


def test_blank_or_unknown_token_is_not_approved():
    s = DesignApprovalStore()
    ok, reason = s.validate(token="", query=_Q, protein=_P)
    assert not ok and "no design_approval_id" in reason
    ok, reason = s.validate(token="dapprv-fabricated", query=_Q, protein=_P)
    assert not ok and "unknown" in reason  # a fabricated token never validates


def test_pending_token_is_not_approved_until_operator_approves():
    """Issuing a token does NOT approve it — the human-in-the-loop step is the approval."""
    s = DesignApprovalStore()
    token = s.request(query=_Q, protein=_P)
    ok, reason = s.validate(token=token, query=_Q, protein=_P)
    assert not ok and "not 'approved'" in reason
    s.approve(token)
    ok, reason = s.validate(token=token, query=_Q, protein=_P)
    assert ok and reason == "approved"


def test_approved_token_is_scope_bound():
    """A token approved for ONE design request must NOT open a DIFFERENT one."""
    s = DesignApprovalStore()
    token = s.request(query=_Q, protein=_P)
    s.approve(token)
    # same scope (whitespace/case-insensitive) → ok
    ok, _ = s.validate(
        token=token,
        query="  Conserved   Chikungunya structural epitopes ",
        protein="STRUCTURAL polyprotein",
    )
    assert ok
    # different protein → scope mismatch, withheld
    ok, reason = s.validate(token=token, query=_Q, protein="nsP3")
    assert not ok and "scope mismatch" in reason
    # different query → scope mismatch, withheld
    ok, reason = s.validate(token=token, query="dengue envelope epitopes", protein=_P)
    assert not ok and "scope mismatch" in reason


def test_rejected_token_does_not_open():
    s = DesignApprovalStore()
    token = s.request(query=_Q, protein=_P)
    s.reject(token)
    ok, reason = s.validate(token=token, query=_Q, protein=_P)
    assert not ok and "rejected" in reason


def test_approve_unknown_token_returns_none_not_silent():
    s = DesignApprovalStore()
    assert s.approve("dapprv-nope") is None  # loud None, never a silent fabricated approval


def test_store_is_bounded_fifo():
    s = DesignApprovalStore(max_tokens=3)
    tokens = [s.request(query=f"q{i}", protein="p") for i in range(5)]
    assert s.get(tokens[-1]) is not None
    assert s.get(tokens[0]) is None  # oldest FIFO-evicted (long-lived-server leak guard)


def test_rejects_bad_cap():
    with pytest.raises(ValueError):
        DesignApprovalStore(max_tokens=0)


def test_singleton_is_process_wide():
    assert get_design_approval_store() is get_design_approval_store()
