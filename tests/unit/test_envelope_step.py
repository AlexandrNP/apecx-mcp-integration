"""Unit tests for EnvelopeStep (EO-13a)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from apecx_integration.composition.handles.store import default_handle_store
from apecx_integration.composition.schemas.data_shapes import RecordSet
from apecx_integration.composition.steps.envelope_step import EnvelopeStep


def _stage(tmp_path: Path) -> EnvelopeStep:
    p = tmp_path / "envelope.yml"
    p.write_text("name: envelope_test\n")
    return EnvelopeStep.from_config(str(p))


@pytest.fixture(autouse=True)
def _clear_store():
    default_handle_store().clear()
    yield
    default_handle_store().clear()


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "envelope_test"


def test_markdown_only(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"markdown": "# answer"}))
    wr = out["workflow_result"]
    assert wr["status"] == "ok"
    assert wr["markdown"] == "# answer"
    assert wr["data_handle"] is None
    assert wr["data_preview"] is None


def test_missing_markdown_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process({"data": {"kind": "record_set", "records": []}}))


def test_data_is_stashed_and_kept_out_of_markdown_channel(tmp_path):
    step = _stage(tmp_path)
    secret_value = "SENSITIVE_PROTEIN_SEQUENCE_XYZ"
    rs = RecordSet(records=[{"seq": secret_value} for _ in range(50)], columns=["seq"])
    out = asyncio.run(
        step.process({"markdown": "found 50 records", "data": rs.model_dump(mode="json")})
    )
    wr = out["workflow_result"]
    # Channel separation: the structured payload is NOT in the markdown the LLM sees.
    assert secret_value not in wr["markdown"]
    # A handle + a small preview are attached instead.
    assert isinstance(wr["data_handle"], str) and wr["data_handle"]
    assert wr["data_preview"]["kind"] == "record_set"
    assert wr["data_preview"]["count"] == 50
    # The full payload is retrievable from the store by the handle (chaining).
    got = default_handle_store().get(wr["data_handle"])
    assert isinstance(got, RecordSet)
    assert len(got.records) == 50
    assert got.records[0]["seq"] == secret_value


def test_bad_data_not_dict_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(step.process({"markdown": "x", "data": "not-a-dict"}))


def test_bad_data_unknown_kind_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValidationError):
        asyncio.run(step.process({"markdown": "x", "data": {"kind": "nope"}}))


def test_framework_envelope_unwrap(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"envelope_input": {"markdown": "# wrapped"}}))
    assert out["workflow_result"]["markdown"] == "# wrapped"


def _stage_keyed(tmp_path: Path, **cfg) -> EnvelopeStep:
    p = tmp_path / "envelope_keyed.yml"
    lines = ["name: envelope_keyed"] + [f"{k}: {v}" for k, v in cfg.items()]
    p.write_text("\n".join(lines) + "\n")
    return EnvelopeStep.from_config(str(p))


def test_configurable_markdown_key_reads_alternate_key(tmp_path):
    # EO-13c: append after rag_synthesis (which emits {"synthesis": "<md>"}).
    step = _stage_keyed(tmp_path, markdown_input_key="synthesis")
    out = asyncio.run(step.process({"synthesis": "# answer"}))
    assert out["workflow_result"]["markdown"] == "# answer"
    assert out["workflow_result"]["status"] == "ok"


def test_configurable_key_unwraps_single_key_link_envelope(tmp_path):
    # The real rag_e2e shape: a DirectLink delivers the upstream step's WHOLE output dict
    # ({"synthesis": "<md>"}) into this step's single input DU, so process() sees
    # {<du_name>: {"synthesis": "<md>"}}. The generic single-key unwrap must descend.
    step = _stage_keyed(tmp_path, markdown_input_key="synthesis")
    out = asyncio.run(step.process({"workflow_output": {"synthesis": "# md body"}}))
    assert out["workflow_result"]["markdown"] == "# md body"


def test_configurable_key_missing_is_loud(tmp_path):
    step = _stage_keyed(tmp_path, markdown_input_key="synthesis")
    with pytest.raises(ValueError, match="synthesis"):
        asyncio.run(step.process({"not_synthesis": "x"}))


def test_default_markdown_key_unchanged(tmp_path):
    step = _stage_keyed(tmp_path)  # no markdown_input_key → default "markdown"
    out = asyncio.run(step.process({"markdown": "x"}))
    assert out["workflow_result"]["markdown"] == "x"
