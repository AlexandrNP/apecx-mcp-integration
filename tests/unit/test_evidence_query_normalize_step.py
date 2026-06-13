"""Unit tests for EvidenceQueryNormalizeStep — the deposit-point passthrough."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.evidence_query_normalize_step import (
    EvidenceQueryNormalizeStep,
)


def _stage(tmp_path: Path) -> EvidenceQueryNormalizeStep:
    p = tmp_path / "normalize.yml"
    p.write_text("name: normalize_test\n")
    return EvidenceQueryNormalizeStep.from_config(str(p))


def test_passthrough_preserves_all_fields(tmp_path):
    """The full param dict must pass through unchanged so ONE output feeds both
    assemble (query) and the gate (control fields)."""
    params = {
        "query": "chikv E1",
        "taxon_id": 37124,
        "requested_outputs": "evidence_plus_design",
        "design_approval_id": "appr-1",
    }
    out = asyncio.run(_stage(tmp_path).process(dict(params)))
    assert out == params


def test_unwraps_framework_envelope(tmp_path):
    out = asyncio.run(_stage(tmp_path).process({"normalize_input": {"query": "q", "x": 1}}))
    assert out == {"query": "q", "x": 1}


def test_missing_query_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"requested_outputs": "evidence_only"}))


def test_blank_query_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process({"query": "   "}))
