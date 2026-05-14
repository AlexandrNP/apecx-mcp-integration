"""Framework-level + end-to-end tests for the rhea_muscle_alignment workflow.

Two test surfaces, mirroring test_rag_e2e_workflow_yaml.py:

  1. **Unconditional** — the workflow YAML + the three step YAMLs
     compose cleanly through ``Workflow.from_config`` and
     ``BaseStep.from_config``; ``RheaFileToolStepConfig`` validates;
     ``FastaCollectionStep`` reads the bundled FASTA and reports
     ``n_sequences == 5``; ``AlignmentReportStep`` parses a small
     fixture alignment FASTA correctly. No external services required.

  2. **Gated on $RHEA_MCP_URL** — the full workflow against a live
     Rhea MCP server (mirrors nanobrain's
     tests/integration/test_rhea_mcp_dispatcher.py gating). Asserts
     ``return_code == 0``, the alignment has 5 sequences, the summary
     is non-empty. Runs BOTH the ``Workflow.from_config`` cascade path
     AND the direct-step-chain path.

The gated tests auto-skip cleanly when ``$RHEA_MCP_URL`` is unset, so
this file is safe to run in CI without a Rhea server.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "rhea_muscle_alignment"
)
WORKFLOW_YAML = WORKFLOW_DIR / "workflow.yml"
STEP_COLLECTION_YAML = WORKFLOW_DIR / "steps" / "fasta_collection.yml"
STEP_MUSCLE_YAML = WORKFLOW_DIR / "steps" / "muscle_alignment.yml"
STEP_REPORT_YAML = WORKFLOW_DIR / "steps" / "alignment_report.yml"
BUNDLED_FASTA = WORKFLOW_DIR / "data" / "seqtest.fasta"

_RHEA_URL = os.environ.get("RHEA_MCP_URL")
_rhea_skip = pytest.mark.skipif(
    _RHEA_URL is None,
    reason="RHEA_MCP_URL not set — live Rhea MCP server required",
)


# A tiny hand-built alignment FASTA fixture: 3 sequences, 8 columns,
# with known gap counts (seq A: 1 gap → 0.125, B: 2 → 0.25, C: 0 → 0.0).
_FIXTURE_ALIGNMENT = ">seqA\nACGT-ACG\n>seqB\nAC--TACG\n>seqC\nACGTTACG\n"


# ---------------------------------------------------------------------------
# Unconditional — loadability + pure-transform tests
# ---------------------------------------------------------------------------


def test_bundled_fasta_present():
    """The bundled example FASTA ships with the workflow."""
    assert BUNDLED_FASTA.is_file(), f"bundled FASTA missing at {BUNDLED_FASTA}"
    assert BUNDLED_FASTA.read_bytes().count(b">") == 5


def test_rhea_file_tool_step_config_validates():
    """RheaFileToolStepConfig validates the muscle_alignment.yml shape.

    ConfigBase subclasses are file-only — load via from_config on the
    actual step YAML.
    """
    from nanobrain.library.steps.rhea_file_tool_step import (
        RheaFileToolStepConfig,
    )

    cfg = RheaFileToolStepConfig.from_config(str(STEP_MUSCLE_YAML))
    assert cfg.tool_name == "muscle"
    assert cfg.file_input_arg == "input_seqs"
    assert cfg.find_tools_query
    assert cfg.static_tool_args["outputFormat"] == "fasta"
    assert "out_align" in cfg.output_file_args


def test_rhea_file_tool_step_config_rejects_unknown_field(tmp_path):
    """extra='forbid' — a YAML typo raises at config load."""
    from nanobrain.library.steps.rhea_file_tool_step import (
        RheaFileToolStepConfig,
    )

    bad_yaml = tmp_path / "bad_muscle.yml"
    bad_yaml.write_text(
        "name: x\n"
        "tool_name: muscle\n"
        "find_tools_query: q\n"
        "file_input_arg: input_seqs\n"
        "timeout_secondz: 10.0\n"  # typo — must be rejected
    )
    with pytest.raises(Exception, match="(?i)extra|forbid|timeout_secondz"):
        RheaFileToolStepConfig.from_config(str(bad_yaml))


def test_collection_step_yaml_loads_via_from_config():
    """FastaCollectionStep YAML composes through BaseStep.from_config."""
    from nanobrain.core.step import BaseStep

    step = BaseStep.from_config(str(STEP_COLLECTION_YAML))
    assert step is not None
    assert step.name == "fasta_collection"


def test_muscle_step_yaml_loads_via_from_config():
    """RheaFileToolStep YAML composes through BaseStep.from_config.

    No Rhea / Redis work happens at load time — only process() touches
    the network — so this is an unconditional test.
    """
    from nanobrain.core.step import BaseStep

    step = BaseStep.from_config(str(STEP_MUSCLE_YAML))
    assert step is not None
    assert step.name == "muscle_alignment"


def test_report_step_yaml_loads_via_from_config():
    """AlignmentReportStep YAML composes through BaseStep.from_config."""
    from nanobrain.core.step import BaseStep

    step = BaseStep.from_config(str(STEP_REPORT_YAML))
    assert step is not None
    assert step.name == "alignment_report"


def test_workflow_yaml_loads_via_from_config():
    """Workflow.from_config on workflow.yml succeeds — DAG validates.

    A link source/target typo, a missing step, a self-referencing
    link, or an orphan step would make this raise.
    """
    from nanobrain.core.workflow import Workflow

    workflow = Workflow.from_config(str(WORKFLOW_YAML))
    assert workflow is not None
    assert workflow.name == "rhea_muscle_alignment"


def test_collection_step_reads_bundled_fasta():
    """FastaCollectionStep falls back to the bundled FASTA → n_sequences=5."""
    from nanobrain.core.step import BaseStep

    step = BaseStep.from_config(str(STEP_COLLECTION_YAML))
    result = asyncio.run(step.process({}))
    assert result["fasta_name"] == "seqtest.fasta"
    assert isinstance(result["fasta_bytes"], bytes)
    assert result["n_sequences"] == 5
    assert result["fasta_bytes"].count(b">") == 5


def test_collection_step_accepts_fasta_text():
    """FastaCollectionStep honors an explicit fasta_text payload."""
    from nanobrain.core.step import BaseStep

    step = BaseStep.from_config(str(STEP_COLLECTION_YAML))
    result = asyncio.run(step.process({"fasta_text": _FIXTURE_ALIGNMENT}))
    assert result["n_sequences"] == 3
    assert result["fasta_bytes"] == _FIXTURE_ALIGNMENT.encode("utf-8")


def test_report_step_parses_fixture_alignment():
    """AlignmentReportStep parses a known fixture FASTA correctly."""
    from nanobrain.core.step import BaseStep

    step = BaseStep.from_config(str(STEP_REPORT_YAML))
    upstream = {
        "tool_name": "muscle",
        "return_code": 0,
        "stdout": "",
        "stderr": "",
        "output_files": {"out_align": _FIXTURE_ALIGNMENT},
    }
    result = asyncio.run(step.process(upstream))
    assert result["n_sequences"] == 3
    assert result["alignment_length"] == 8
    assert result["alignment_fasta"] == _FIXTURE_ALIGNMENT
    gaps = {p["id"]: p["gap_fraction"] for p in result["per_sequence"]}
    assert gaps == {"seqA": 0.125, "seqB": 0.25, "seqC": 0.0}
    assert result["summary"]
    assert "muscle alignment report" in result["summary"]
    assert "3" in result["summary"]


def test_report_step_fails_loud_on_missing_out_align():
    """AlignmentReportStep raises when the out_align file is absent."""
    from nanobrain.core.step import BaseStep

    step = BaseStep.from_config(str(STEP_REPORT_YAML))
    upstream = {
        "tool_name": "muscle",
        "return_code": 0,
        "output_files": {"out_align_html": "<html/>"},
    }
    with pytest.raises(ValueError, match="out_align"):
        asyncio.run(step.process(upstream))


def test_report_step_fails_loud_on_empty_alignment():
    """AlignmentReportStep raises when the alignment FASTA has 0 sequences."""
    from nanobrain.core.step import BaseStep

    step = BaseStep.from_config(str(STEP_REPORT_YAML))
    upstream = {
        "tool_name": "muscle",
        "return_code": 0,
        "output_files": {"out_align": "not a fasta — no headers\n"},
    }
    with pytest.raises(ValueError):
        asyncio.run(step.process(upstream))


# ---------------------------------------------------------------------------
# Gated on $RHEA_MCP_URL — full end-to-end against a live Rhea server
# ---------------------------------------------------------------------------


@_rhea_skip
def test_direct_step_chain_against_live_rhea(monkeypatch):
    """Drive the three steps directly (collect → muscle → report) against
    a live Rhea MCP server, using the bundled FASTA.

    Asserts return_code == 0, the alignment has 5 sequences, the
    summary is non-empty.
    """
    monkeypatch.setenv("RHEA_MCP_URL", _RHEA_URL)
    from nanobrain.core.step import BaseStep

    async def _run() -> dict:
        collection = BaseStep.from_config(str(STEP_COLLECTION_YAML))
        muscle = BaseStep.from_config(str(STEP_MUSCLE_YAML))
        report = BaseStep.from_config(str(STEP_REPORT_YAML))
        # Point the muscle step at the configured Rhea URL.
        muscle._rfts_config.mcp_url = _RHEA_URL

        staged = await collection.process({})
        assert staged["n_sequences"] == 5
        tool_result = await muscle.process(staged)
        assert tool_result["return_code"] == 0
        assert tool_result["output_files"], "no output files fetched from Rhea"
        report_result = await report.process(tool_result)
        return report_result

    result = asyncio.run(_run())
    assert result["n_sequences"] == 5, f"expected 5 aligned sequences; got {result['n_sequences']}"
    assert result["alignment_length"] > 0
    assert result["summary"].strip(), "alignment summary is empty"


@_rhea_skip
def test_workflow_from_config_against_live_rhea(monkeypatch):
    """Drive the rhea_muscle_alignment workflow via the FULL trigger
    cascade (Workflow.from_config → initialize → process → wait) against
    a live Rhea MCP server.

    This pins the OTHER half of the contract vs. the direct-step-chain
    test: that the DirectLinks (all auto_transfer: true) actually
    transfer and the trigger graph fires every step.
    """
    monkeypatch.setenv("RHEA_MCP_URL", _RHEA_URL)
    from nanobrain.core.workflow import Workflow

    async def _drive_cascade() -> dict:
        wf = Workflow.from_config(str(WORKFLOW_YAML))
        # Phase 3 — resolve + bind step triggers (see the rag_e2e
        # workflow-yaml test for why this is explicit).
        await wf.initialize()

        children = (
            getattr(wf, "child_steps", None)
            or getattr(wf, "_child_steps", None)
            or getattr(wf, "steps", None)
        )
        muscle = children["muscle_alignment"]
        muscle._rfts_config.mcp_url = _RHEA_URL
        report = children["alignment_report"]

        # Data-driven entry: empty payload → FastaCollectionStep uses
        # the bundled default FASTA.
        init_result = await wf.process({"fasta_collection_input": {}})
        assert init_result is not None

        drained = await wf.wait_for_cascade(timeout=900.0, settle_ms=200)
        assert drained, (
            "trigger cascade did not drain within 900s — either a "
            "DirectLink did not transfer or a step hung. MUSCLE on a "
            "5-sequence FASTA is normally well under a minute once the "
            "conda env is built."
        )

        out_du = report.step_output_data_units["alignment_report_output"]
        return await out_du.get()

    output = asyncio.run(_drive_cascade())
    assert output is not None, (
        "alignment_report_output was not set after the cascade drained "
        "— a DirectLink in workflow.yml may not have transferred. Check "
        "the link wiring and that every DirectLink has auto_transfer: true."
    )
    assert output["n_sequences"] == 5, (
        f"expected 5 aligned sequences; got {output.get('n_sequences')}"
    )
    assert output["summary"].strip(), "alignment summary is empty"
