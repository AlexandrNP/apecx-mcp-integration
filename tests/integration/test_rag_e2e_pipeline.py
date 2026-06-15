"""End-to-end integration tests for the RAG synthesis pipeline.

Covers:
  - Domain RAG index: real FAISS search with real embeddings
  - SynthesisContextAssemblyStep: full retrieval assembly (incl. the
    VIOLIN + BV-BRC tabular branch), no LLM
  - RagSynthesisStep: full E2E against real Ollama (gated)
  - Workflow YAML loading: rag_e2e_synthesis_workflow.yml

Gates:
  - Most tests run with no external dependencies (local files only).
  - LLM tests auto-skip when Ollama is not reachable or
    APECX_SKIP_LIVE_LLM=1.

Per CLAUDE.md unit-mock/integration-test parity: these tests exercise
the REAL data path that the unit tests mock out. A green CI badge that
relies entirely on mocked unit tests would not catch a broken FAISS
index or missing CSV columns.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
DOMAIN_RAG_INDEX = WORKSPACE_ROOT / "data" / "apecx_domain_rag"
VIOLIN_DIR = WORKSPACE_ROOT / "data" / "violin"
BVBRC_DIR = WORKSPACE_ROOT / "data" / "bvbrc_cache"

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _agent_locus():
    """Exercise the BACKEND internal-synthesis path (real-Ollama E2E + the empty-retrieval
    gate). The default locus is ``desktop`` (host synthesizes → apecx LLM omitted); the local
    FAISS/CSV/assembly tests don't touch RagSynthesisStep so this is a no-op for them.
    Restored after each test."""
    from apecx_integration.composition.runtime.execution_locus import (
        ExecutionLocus,
        get_active_locus,
        set_active_locus,
    )

    prior = get_active_locus()
    set_active_locus(ExecutionLocus.AGENT)
    try:
        yield
    finally:
        set_active_locus(prior)


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("APECX_LLM_MODEL", "mistral-nemo:latest")


def _ollama_reachable() -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return OLLAMA_MODEL in names
    except Exception:
        return False


def _skip_live_llm() -> bool:
    return os.environ.get("APECX_SKIP_LIVE_LLM") == "1"


# ---------------------------------------------------------------------------
# Domain RAG index — no LLM, no network
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rag_index():
    """Load the domain RAG index once per module."""
    # sentence_transformers MUST be imported before faiss on macOS ARM.
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("faiss")
    if not (DOMAIN_RAG_INDEX / "faiss_index.bin").exists():
        pytest.skip(
            f"Domain RAG index not built at {DOMAIN_RAG_INDEX}. "
            "Run: PYTHONPATH=src .venv/bin/python "
            "scripts/build_domain_rag_index.py"
        )
    from apecx_integration.agents.domain_rag import DomainRagIndex

    return DomainRagIndex(index_dir=DOMAIN_RAG_INDEX)


def test_rag_index_returns_results_for_sars_query(rag_index):
    """A query about SARS-CoV-2 returns non-empty results."""
    results = rag_index.search("SARS-CoV-2 coronavirus vaccine", k=5)
    assert len(results) >= 1


def test_rag_index_result_shape(rag_index):
    """Each result has the expected keys."""
    results = rag_index.search("influenza virus pathogenesis", k=3)
    for hit in results:
        assert "id" in hit
        assert "text" in hit
        assert "score" in hit
        assert "source" in hit
        assert isinstance(hit["score"], float)
        assert 0.0 <= hit["score"] <= 1.0


def test_rag_index_results_are_ranked_by_score(rag_index):
    """Results are returned in descending score order."""
    results = rag_index.search("Ebola virus hemorrhagic fever", k=5)
    if len(results) < 2:
        pytest.skip("Too few results to check ordering")
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_rag_index_eeev_query_returns_alphavirus_content(rag_index):
    """Query for EEEV returns chunks mentioning alphavirus/encephalitis."""
    results = rag_index.search("Eastern equine encephalitis alphavirus", k=5)
    combined = " ".join(r["text"].lower() for r in results)
    # VIOLIN Pathogen_Information.csv contains EEEV data; at least one
    # result should mention encephalitis or alphavirus.
    assert "encephalitis" in combined or "alphavirus" in combined or "eeev" in combined


def test_rag_index_empty_query_returns_empty(rag_index):
    """Empty and blank queries return empty lists, not errors."""
    assert rag_index.search("") == []
    assert rag_index.search("   ") == []


def test_rag_index_k_cap_respected(rag_index):
    """Search(k=2) returns at most 2 results."""
    results = rag_index.search("virus infection host", k=2)
    assert len(results) <= 2


def test_rag_index_source_references_violin(rag_index):
    """At least one result references the VIOLIN corpus."""
    results = rag_index.search("pathogen vaccine immunity", k=10)
    sources = {r["source"] for r in results}
    assert any("VIOLIN" in s or "violin" in s for s in sources)


# ---------------------------------------------------------------------------
# SynthesisContextAssemblyStep — real data, no LLM, skip PubMed for offline
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def assembly_step():
    """Load SynthesisContextAssemblyStep with real data, PubMed skipped."""
    if not (DOMAIN_RAG_INDEX / "faiss_index.bin").exists():
        pytest.skip(f"Domain RAG index not found at {DOMAIN_RAG_INDEX}")
    if not (VIOLIN_DIR / "Pathogen_Information.csv").exists():
        pytest.skip(f"VIOLIN data not found at {VIOLIN_DIR}")
    step_yaml = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "rag_e2e_synthesis"
        / "steps"
        / "synthesis_context_assembly.yml"
    )
    from apecx_integration.composition.steps.synthesis_context_assembly_step import (
        SynthesisContextAssemblyStep,
    )

    step = SynthesisContextAssemblyStep.from_config(str(step_yaml))
    # Force-skip PubMed for offline tests
    step._skip_pubmed = True
    return step


def test_assembly_step_returns_full_bundle(assembly_step):
    """The assembly step returns all four retrieval keys."""
    result = asyncio.run(assembly_step.process({"query": "SARS-CoV-2 vaccine immune response"}))
    assert "query" in result
    assert "rag_chunks" in result
    assert "bvbrc_genomes" in result
    assert "violin_mappings" in result
    assert "publications" in result
    assert result["query"] == "SARS-CoV-2 vaccine immune response"


def test_assembly_step_rag_chunks_non_empty(assembly_step):
    """RAG chunks are returned for a domain query."""
    result = asyncio.run(
        assembly_step.process({"query": "Ebola virus pathogenesis hemorrhagic fever"})
    )
    assert len(result["rag_chunks"]) >= 1


def test_assembly_step_rag_chunk_shape(assembly_step):
    """Each RAG chunk has id, text, score, source keys."""
    result = asyncio.run(assembly_step.process({"query": "influenza vaccination protection"}))
    for chunk in result["rag_chunks"]:
        assert "id" in chunk
        assert "text" in chunk
        assert "score" in chunk
        assert isinstance(chunk["text"], str) and len(chunk["text"]) > 0


def test_assembly_step_violin_match_for_herpes(assembly_step):
    """VIOLIN lookup finds Herpes simplex virus data."""
    result = asyncio.run(
        assembly_step.process(
            {
                "query": "Herpes simplex virus latency reactivation",
                "entities": [{"name": "Herpes simplex", "type": "pathogen"}],
            }
        )
    )
    assert len(result["violin_mappings"]) >= 1


def test_assembly_step_empty_query_raises(assembly_step):
    """Empty query raises ValueError before retrieval."""
    with pytest.raises(ValueError, match="[Qq]uery|empty"):
        asyncio.run(assembly_step.process({"query": ""}))


def test_assembly_step_query_preserved_in_output(assembly_step):
    """The output bundle preserves the original query string."""
    q = "   Dengue fever vaccine tropical diseases   "
    result = asyncio.run(assembly_step.process({"query": q}))
    # The step strips whitespace before passing downstream
    assert result["query"] == q.strip()


def test_assembly_step_publications_empty_when_skipped(assembly_step):
    """With skip_pubmed=True the publications list is empty."""
    result = asyncio.run(assembly_step.process({"query": "West Nile virus encephalitis"}))
    assert result["publications"] == []


# ---------------------------------------------------------------------------
# RagSynthesisStep loaded via from_config
# ---------------------------------------------------------------------------


def test_rag_synthesis_step_yaml_loads():
    """RagSynthesisStep.from_config works with the bundled YAML."""
    from apecx_integration.composition.steps.rag_synthesis_step import (
        RagSynthesisStep,
    )

    step_yaml = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "rag_e2e_synthesis"
        / "steps"
        / "rag_synthesis.yml"
    )
    step = RagSynthesisStep.from_config(str(step_yaml))
    assert step is not None
    assert step.name == "rag_synthesis"


def test_rag_synthesis_step_raises_on_empty_retrieval():
    """fail_on_empty_retrieval gate fires without LLM contact."""
    from apecx_integration.composition.steps.rag_synthesis_step import (
        RagSynthesisStep,
    )

    step_yaml = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "rag_e2e_synthesis"
        / "steps"
        / "rag_synthesis.yml"
    )
    step = RagSynthesisStep.from_config(str(step_yaml))
    with pytest.raises(ValueError, match="every retrieval input is empty"):
        asyncio.run(step.process({"query": "any query"}))


# ---------------------------------------------------------------------------
# Workflow YAML loading
# ---------------------------------------------------------------------------


def test_rag_e2e_workflow_yaml_loads():
    """The rag_e2e_synthesis_workflow.yml loads without errors."""
    import yaml

    wf_path = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "rag_e2e_synthesis"
        / "rag_e2e_synthesis_workflow.yml"
    )
    with open(wf_path) as f:
        wf = yaml.safe_load(f)
    assert wf["name"] == "rag_e2e_synthesis_workflow"
    assert "synthesis_context_assembly" in wf["steps"]
    assert "rag_synthesis" in wf["steps"]
    assert "assembly_to_synthesis" in wf["links"]


def test_all_new_step_yamls_reference_valid_classes():
    """Every step YAML in the Day-2 workflow directories has an
    importable ``class:`` field.

    Enumerates the steps directories dynamically rather than hardcoding
    a list — so a future step added to either workflow is auto-covered
    without anyone remembering to update this test.
    """
    import importlib

    import yaml

    workflows_root = REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows"
    # Day-2 workflow step directories. Each contains the YAMLs that
    # were authored or wrapped during the synthesis-pipeline work.
    # (violin_bvbrc retired 2026-06-15; rag_e2e_synthesis survives.)
    day2_step_dirs = [
        workflows_root / "rag_e2e_synthesis" / "steps",
    ]

    checked = 0
    for steps_dir in day2_step_dirs:
        if not steps_dir.is_dir():
            continue
        for path in sorted(steps_dir.glob("*.yml")):
            with open(path) as f:
                cfg = yaml.safe_load(f)
            class_str = cfg.get("class")
            if not class_str:
                # Some YAMLs (e.g. tool wrappers) don't have a top-level
                # ``class:`` — skip them, they're not steps.
                continue
            module_path, class_name = class_str.rsplit(".", 1)
            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                assert cls is not None, f"{path.name}: class {class_str} is None"
            except (ImportError, AttributeError) as exc:
                pytest.fail(f"{path.name}: class {class_str} not importable: {exc}")
            checked += 1

    # Sanity floor — at least 3 surviving Day-2 step YAMLs exist
    # (envelope, synthesis_context_assembly, rag_synthesis). If this
    # ever drops below 3, the test isn't actually checking anything.
    assert checked >= 3, f"only {checked} step YAMLs were checked; expected ≥3"


# ---------------------------------------------------------------------------
# Full E2E pipeline against real Ollama (gated)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _gate_ollama():
    if _skip_live_llm():
        pytest.skip("APECX_SKIP_LIVE_LLM=1")
    if not _ollama_reachable():
        pytest.skip(f"Ollama not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL} not pulled.")


@pytest.mark.usefixtures("_gate_ollama")
def test_full_e2e_pipeline_against_ollama():
    """Full chain: assembly → synthesis → Markdown with citations.

    This is the canonical E2E test. It exercises:
      1. DomainRagIndex FAISS search (real embeddings, real corpus)
      2. VIOLINBVBRCContextStep pandas lookup (real CSVs)
      3. RagSynthesisStep LLM synthesis via local Ollama
      4. Inline citation grounding (synthesizer gates)
    PubMed is skipped (no network dependency in CI).
    """
    if not (DOMAIN_RAG_INDEX / "faiss_index.bin").exists():
        pytest.skip(f"Domain RAG index not found at {DOMAIN_RAG_INDEX}")
    if not (VIOLIN_DIR / "Pathogen_Information.csv").exists():
        pytest.skip(f"VIOLIN data not found at {VIOLIN_DIR}")

    from apecx_integration.composition.steps.rag_synthesis_step import (
        RagSynthesisStep,
    )
    from apecx_integration.composition.steps.synthesis_context_assembly_step import (
        SynthesisContextAssemblyStep,
    )

    assembly_yaml = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "rag_e2e_synthesis"
        / "steps"
        / "synthesis_context_assembly.yml"
    )
    synthesis_yaml = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "rag_e2e_synthesis"
        / "steps"
        / "rag_synthesis.yml"
    )

    assembly_step = SynthesisContextAssemblyStep.from_config(str(assembly_yaml))
    assembly_step._skip_pubmed = True  # offline-friendly

    synthesis_step = RagSynthesisStep.from_config(str(synthesis_yaml))

    query = (
        "What vaccines have been developed for Eastern equine "
        "encephalitis virus (EEEV) and what is known about their "
        "immunological mechanism?"
    )

    # Phase 1: retrieval assembly
    bundle = asyncio.run(
        assembly_step.process(
            {
                "query": query,
                "entities": [
                    {"name": "EEEV", "type": "pathogen"},
                    {"name": "Eastern equine encephalitis", "type": "disease"},
                ],
            }
        )
    )
    assert len(bundle["rag_chunks"]) >= 1, "RAG search produced no chunks"

    # Phase 2: LLM synthesis
    result = asyncio.run(synthesis_step.process(bundle))
    synthesis_text = result["synthesis"]

    assert isinstance(synthesis_text, str)
    assert len(synthesis_text.strip()) >= 200, (
        f"Response too short ({len(synthesis_text)} chars):\n{synthesis_text}"
    )
    # At least one inline citation should appear (the synthesizer's
    # citation validation gates would have raised before this point
    # if none were present — this assertion is a sanity check).
    citation_patterns = [
        "[RAG chunk #",
        "[BV-BRC genome",
        "[VIOLIN",
        "[10.",  # DOI prefix
    ]
    assert any(p in synthesis_text for p in citation_patterns), (
        f"No inline citation found in response:\n{synthesis_text[:500]}"
    )


@pytest.mark.usefixtures("_gate_ollama")
def test_e2e_pipeline_sars_cov2_query():
    """SARS-CoV-2 query produces a non-trivial synthesis."""
    if not (DOMAIN_RAG_INDEX / "faiss_index.bin").exists():
        pytest.skip(f"Domain RAG index not found at {DOMAIN_RAG_INDEX}")
    if not (VIOLIN_DIR / "Pathogen_Information.csv").exists():
        pytest.skip(f"VIOLIN data not found at {VIOLIN_DIR}")

    from apecx_integration.composition.steps.rag_synthesis_step import (
        RagSynthesisStep,
    )
    from apecx_integration.composition.steps.synthesis_context_assembly_step import (
        SynthesisContextAssemblyStep,
    )

    assembly_yaml = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "rag_e2e_synthesis"
        / "steps"
        / "synthesis_context_assembly.yml"
    )
    synthesis_yaml = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "rag_e2e_synthesis"
        / "steps"
        / "rag_synthesis.yml"
    )

    assembly_step = SynthesisContextAssemblyStep.from_config(str(assembly_yaml))
    assembly_step._skip_pubmed = True
    synthesis_step = RagSynthesisStep.from_config(str(synthesis_yaml))

    bundle = asyncio.run(
        assembly_step.process(
            {"query": ("What is known about SARS-CoV-2 immunity and host protective response?")}
        )
    )
    # VIOLIN has SARS pathogen data — the RAG and VIOLIN branches
    # should both contribute.
    assert len(bundle["rag_chunks"]) >= 1

    result = asyncio.run(synthesis_step.process(bundle))
    body = result["synthesis"]
    assert len(body.strip()) >= 200
