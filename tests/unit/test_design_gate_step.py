"""Unit tests for DesignGateStep — the fan-in approval gate (no network).

The gate emits ``{markdown, control_transfer?}`` for the terminal EnvelopeStep:
a ``control_transfer`` present means a ``needs_input`` disposition.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.design_gate_step import DesignGateStep


def _stage(tmp_path: Path) -> DesignGateStep:
    p = tmp_path / "gate.yml"
    p.write_text("name: gate_test\n")
    return DesignGateStep.from_config(str(p))


def _inp(requested="evidence_only", approval=None, md="# Evidence\n\nbody [Globus pdb:1I9G]."):
    control = {"query": "chikv E1", "requested_outputs": requested}
    if approval is not None:
        control["design_approval_id"] = approval
    return {"review_in": {"markdown": md}, "control_in": control}


def test_evidence_only_passes_markdown_no_control_transfer(tmp_path):
    out = asyncio.run(_stage(tmp_path).process(_inp()))
    assert out["markdown"].startswith("# Evidence")
    assert "control_transfer" not in out  # ok disposition
    assert "design" not in out["markdown"].lower()


def test_design_without_approval_attaches_needs_prerequisite_keeps_evidence(tmp_path):
    out = asyncio.run(_stage(tmp_path).process(_inp(requested="evidence_plus_design")))
    assert out["control_transfer"]["reason"] == "needs_prerequisite"
    # evidence is NOT discarded on a pause (degrade-loud) + the withholding is named.
    assert "# Evidence" in out["markdown"]
    assert "WITHHELD" in out["markdown"]


def test_design_with_approval_appends_section_with_provenance(tmp_path):
    out = asyncio.run(
        _stage(tmp_path).process(_inp(requested="evidence_plus_design", approval="appr-123"))
    )
    assert "control_transfer" not in out  # ok disposition
    assert "Design / optimization hypotheses (approved)" in out["markdown"]
    assert "appr-123" in out["markdown"]  # approval provenance attached
    assert "# Evidence" in out["markdown"]  # evidence retained


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
