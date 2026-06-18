"""Unit tests for WorkflowResult (EO-10)."""

import pytest
from pydantic import ValidationError

from apecx_integration.composition.schemas.workflow_result import WorkflowResult


def test_ok_result_minimal():
    r = WorkflowResult(markdown="# answer")
    assert r.status == "ok"
    assert r.data_handle is None
    assert r.error is None


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        WorkflowResult(markdown="x", notafield=1)


def test_error_status_requires_message():
    with pytest.raises(ValidationError):
        WorkflowResult(markdown="", status="error")
    with pytest.raises(ValidationError):
        WorkflowResult(markdown="", status="error", error="   ")


def test_error_message_forbidden_on_non_error():
    with pytest.raises(ValidationError):
        WorkflowResult(markdown="x", status="ok", error="boom")


def test_preview_requires_handle():
    with pytest.raises(ValidationError):
        WorkflowResult(markdown="x", data_preview={"n": 1})


def test_preview_with_handle_ok():
    r = WorkflowResult(markdown="x", data_handle="h1", data_preview={"n": 1})
    assert r.data_handle == "h1"
    assert r.data_preview == {"n": 1}


def test_failed_constructor():
    r = WorkflowResult.failed("synthesis gate: empty retrieval", run_id="run-7")
    assert r.status == "error"
    assert "empty retrieval" in (r.error or "")
    assert r.run_id == "run-7"


def test_partial_status_allowed():
    r = WorkflowResult(markdown="partial answer", status="partial")
    assert r.status == "partial"
    assert r.error is None


def test_roundtrip_serialization():
    r = WorkflowResult(markdown="# a", data_handle="h", data_preview={"k": "v"}, run_id="r1")
    dumped = r.model_dump(mode="json")
    assert dumped["status"] == "ok"
    r2 = WorkflowResult.model_validate(dumped)
    assert r2 == r


# --------------------------------------------------------------------------- #
# RoC-1a — needs_input / control_transfer invariants
# --------------------------------------------------------------------------- #
def _ct():
    from apecx_integration.composition.schemas.control_transfer import (
        ParamNeed,
        missing_param_transfer,
    )

    return missing_param_transfer(
        [ParamNeed(param_name="taxon_id", obtain_via="harmonized_search")]
    )


def test_needs_input_requires_control_transfer():
    with pytest.raises(ValidationError):
        WorkflowResult(markdown="", status="needs_input")  # no control_transfer


def test_control_transfer_forbidden_when_not_needs_input():
    with pytest.raises(ValidationError):
        WorkflowResult(markdown="x", status="ok", control_transfer=_ct())


def test_needs_input_constructor_and_roundtrip():
    r = WorkflowResult.needs_input(_ct(), run_id="r9")
    assert r.status == "needs_input"
    assert r.control_transfer.reason == "missing_param"
    assert r.run_id == "r9"
    # JSON round-trips through the MCP surface.
    r2 = WorkflowResult.model_validate(r.model_dump(mode="json"))
    assert r2 == r


def test_needs_input_constructor_allows_data_handle():
    r = WorkflowResult.needs_input(
        _ct(),
        markdown="approval required",
        data_handle="h1",
        data_preview={"kind": "bundle"},
    )
    assert r.status == "needs_input"
    assert r.data_handle == "h1"
    assert r.data_preview == {"kind": "bundle"}
