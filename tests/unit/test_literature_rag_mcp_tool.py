"""Unit tests for the literature_rag MCP tool wiring: the HarvestStampStep
front-end, the LiteratureRagStep desktop-locus inversion, and the lightweight
builder / catalog contract.

Steps are built via ``from_config`` (the framework contract; sets up nb_logger,
data units, triggers) — never a direct constructor.

Mocks here (harvest, gazetteer, locus) have their real-data parity partner in
tests/integration/test_literature_rag_tool_live.py (unit-mock / integration-test
parity rule).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.runtime.execution_locus import ExecutionLocus

_CHIKV = "http://purl.obolibrary.org/obo/NCBITaxon_37124"


def _step_yaml(name: str) -> str:
    """Absolute path to a step YAML, resolved from the installed package (so it
    works regardless of cwd / which worktree the editable install points at)."""
    import apecx_integration.composition.workflows.literature_rag.steps as pkg

    return str(Path(pkg.__file__).resolve().parent / name)


# --- HarvestStampStep ------------------------------------------------------


def _harvest_step():
    from apecx_integration.composition.workflows.literature_rag.steps.harvest_stamp_step import (
        HarvestStampStep,
    )

    return HarvestStampStep.from_config(_step_yaml("harvest_stamp_step.yml"))


def test_harvest_stamp_step_smoke(monkeypatch):
    """Emits the {organism, question, stamped_records} contract the cascade expects;
    stamped records carry the harvest+stamp output (incl. iris)."""
    fake_records = [{"pmid": "1", "title": "Chikungunya E1", "abstract": "x", "iris": [_CHIKV]}]
    monkeypatch.setattr(
        "apecx_integration.agents.literature.pipeline.harvest_and_stamp",
        lambda term, gazetteer, max_papers=20: fake_records,
    )

    step = _harvest_step()
    out = asyncio.run(
        step.process(
            {"literature_rag_input": {"organism": "Chikungunya virus", "question": "E1 epitopes"}}
        )
    )

    assert out["organism"] == "Chikungunya virus"
    assert out["question"] == "E1 epitopes"
    assert out["stamped_records"] == fake_records
    assert out["stamped_records"][0]["iris"] == [_CHIKV]


def test_harvest_stamp_step_accepts_flat_input(monkeypatch):
    """A direct (non-cascade) process(envelope) call works too."""
    monkeypatch.setattr(
        "apecx_integration.agents.literature.pipeline.harvest_and_stamp",
        lambda term, gazetteer, max_papers=20: [],
    )
    step = _harvest_step()
    out = asyncio.run(step.process({"organism": "EEEV", "question": "q"}))
    assert out["stamped_records"] == []


def test_harvest_stamp_step_requires_organism():
    step = _harvest_step()
    with pytest.raises(ValueError, match="organism"):
        asyncio.run(step.process({"literature_rag_input": {"question": "q"}}))


def test_harvest_stamp_step_harvest_failure_degrades(monkeypatch):
    """A harvest exception degrades to 0 records (never crashes the step)."""

    def _boom(term, gazetteer, max_papers=20):
        raise RuntimeError("simulated: PubMed unreachable")

    monkeypatch.setattr("apecx_integration.agents.literature.pipeline.harvest_and_stamp", _boom)
    step = _harvest_step()
    out = asyncio.run(step.process({"organism": "Chikungunya virus", "question": "q"}))
    assert out["stamped_records"] == []


# --- LiteratureRagStep desktop-locus inversion -----------------------------


def _rag_step():
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    return LiteratureRagStep.from_config(_step_yaml("literature_rag_step.yml"))


def test_literature_rag_step_desktop_omits_llm(monkeypatch):
    """In desktop locus the step returns status=host_synthesizes with the filtered
    evidence in `answer` + PMIDs in `citations`, and NEVER calls the apecx LLM (or
    the embedding retrieval)."""
    monkeypatch.setattr(
        "apecx_integration.composition.runtime.execution_locus.get_active_locus",
        lambda: ExecutionLocus.DESKTOP,
    )

    def _no_llm():
        raise AssertionError("build_chat_llm must NOT be called in desktop locus")

    monkeypatch.setattr("apecx_integration.agents._llm_factory.build_chat_llm", _no_llm)

    step = _rag_step()
    # If embedding retrieval were reached it would need the rag extra — make it explode
    # so the test fails loudly if the desktop branch is ever moved back below retrieval.
    step._get_embed_model = lambda: (_ for _ in ()).throw(
        AssertionError("retrieval must be skipped in desktop locus")
    )

    env = {
        "rag_input": {
            "question": "What is known about CHIKV E1 epitopes?",
            "filtered_records": [
                {
                    "pmid": "123",
                    "title": "CHIKV E1 epitope",
                    "abstract": "mapping",
                    "iris": [_CHIKV],
                },
                {
                    "pmid": "456",
                    "title": "CHIKV structural",
                    "abstract": "cryoEM",
                    "iris": [_CHIKV],
                },
            ],
        }
    }
    out = asyncio.run(step.process(env))

    assert out["status"] == "host_synthesizes"
    assert out["citations"] == ["123", "456"]
    assert "[PMID:123]" in out["answer"]
    assert "CHIKV E1 epitope" in out["answer"]


def test_literature_rag_step_desktop_no_evidence(monkeypatch):
    """Desktop locus with no filtered_records still degrades to no_evidence (no LLM)."""
    monkeypatch.setattr(
        "apecx_integration.composition.runtime.execution_locus.get_active_locus",
        lambda: ExecutionLocus.DESKTOP,
    )
    step = _rag_step()
    out = asyncio.run(step.process({"rag_input": {"question": "q", "filtered_records": []}}))
    assert out["status"] == "no_evidence"


def test_literature_rag_step_declares_final_synthesis_role():
    """LLM_ROLE must be 'final_synthesis' so the requires_llm gate does not refuse the
    tool on a desktop with no Ollama."""
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    assert LiteratureRagStep.LLM_ROLE == "final_synthesis"


# --- builder + catalog contract -------------------------------------------


def test_literature_rag_builder_loads():
    """The lightweight builder returns a nanobrain Workflow (topology passes the
    framework's load-time validators)."""
    from nanobrain.core.workflow import Workflow

    from apecx_integration.composition.workflows.literature_rag.builder import (
        build_literature_rag_workflow,
    )

    assert isinstance(build_literature_rag_workflow(), Workflow)


def test_literature_rag_not_refused_in_desktop_locus():
    """The requires_llm gate must NOT demand an apecx LLM for this workflow in desktop
    locus (LiteratureRagStep.LLM_ROLE='final_synthesis' → host synthesizes), while
    agent/headless locus DOES require one. Without the LLM_ROLE fix the desktop user
    with no Ollama is wrongly refused the tool."""
    from apecx_integration.composition.workflows.literature_rag.builder import (
        build_literature_rag_workflow,
    )
    from apecx_integration.mcp_surface.llm_policy import workflow_needs_llm_at_run

    w = build_literature_rag_workflow()
    assert workflow_needs_llm_at_run(w, ExecutionLocus.DESKTOP) is False
    assert workflow_needs_llm_at_run(w, ExecutionLocus.AGENT) is True


def test_literature_rag_catalog_envelope_key_matches_first_step():
    """Guards the input_envelope_key silent-failure: the catalog entry's
    input_envelope_key MUST equal HarvestStampStep's input data-unit name, or
    run_workflow deposits into 0 data units."""
    from apecx_integration.composition.workflows.literature_rag.steps.harvest_stamp_step import (
        HarvestStampStep,
    )
    from apecx_integration.mcp_surface.workflow_registry import load_catalog

    catalog = load_catalog()
    entry = next(w for w in catalog.workflows if w.tool_name == "literature_rag")
    assert entry.input_envelope_key == HarvestStampStep._INPUT_UNIT == "literature_rag_input"
    assert entry.source.kind == "lightweight"
