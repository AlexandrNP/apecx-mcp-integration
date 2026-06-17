"""Regression tests for SynthesisContextAssemblyStep branch-failure behavior.

The assembly step runs three retrieval branches via ``asyncio.gather``.
A prior version called gather WITHOUT ``return_exceptions=True``, so a
single branch raising (e.g. corrupted FAISS index, missing CSV column,
network timeout) crashed the entire synthesis call — turning what
should be a graceful-degradation event into a workflow-killing crash.

The fix is in ``synthesis_context_assembly_step.py``: gather is called
with ``return_exceptions=True`` and each branch's result is unwrapped
via an ``isinstance(BaseException)`` guard, falling back to an empty
list (or empty pair) and emitting a WARNING log line. This file pins
that behavior so a future refactor can't silently regress.

Why a unit test (not integration)
---------------------------------
We only want to verify the gather/unwrap logic — actual FAISS, CSV,
and PubMed I/O is irrelevant to this contract. Mocking the three
helper methods keeps the test fast (<100ms) and deterministic
regardless of host environment.
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.steps.synthesis_context_assembly_step import (
    SynthesisContextAssemblyStep,
)


def _make_bare_step() -> SynthesisContextAssemblyStep:
    """Construct a SynthesisContextAssemblyStep without going through
    from_config — same shortcut the step itself uses for its internal
    helpers (documented in the step source). The only state we need is
    the attributes process() reads, all set explicitly here.
    """
    step = object.__new__(SynthesisContextAssemblyStep)
    step.name = "test_assembly"
    step._k_rag = 5
    step._max_publications = 5
    step._query_template = "{query}"
    step._skip_pubmed = True  # short-circuit the PubMed branch by default
    step._violin_data_dir = None
    step._bvbrc_cache_dir = None
    step._max_violin = 10
    step._max_bvbrc = 10
    return step


@pytest.fixture
def assembly_step():
    return _make_bare_step()


async def test_rag_branch_exception_degrades_to_empty_chunks(assembly_step):
    """RAG branch raising → rag_chunks=[], other branches unaffected.

    Detection signal: a future commit removes ``return_exceptions=True``
    from gather → process() re-raises FileNotFoundError → this test
    fails with the original exception, surfacing the regression.
    """

    def _exploding_rag(query: str):
        raise FileNotFoundError("simulated: faiss_index.bin missing")

    def _ok_violin(terms):
        return ([{"synonym_id": "VIOLIN_pathogen_1"}], [{"genome_id": "11036.7"}])

    assembly_step._rag_search = _exploding_rag
    assembly_step._violin_bvbrc_lookup = _ok_violin

    out = await assembly_step.process({"query": "EEEV vaccine"})

    assert out["rag_chunks"] == []
    assert out["violin_mappings"] == [{"synonym_id": "VIOLIN_pathogen_1"}]
    assert out["bvbrc_genomes"] == [{"genome_id": "11036.7"}]
    assert out["publications"] == []  # skip_pubmed=True
    assert out["query"] == "EEEV vaccine"


async def test_violin_branch_exception_degrades_to_empty_bundles(assembly_step):
    """VIOLIN/BV-BRC branch raising → both bundles=[], other branches unaffected."""

    def _ok_rag(query: str):
        return [{"id": 1, "text": "domain chunk"}]

    def _exploding_violin(terms):
        raise KeyError("simulated: 'NCBI_Taxonomy_ID' column missing")

    assembly_step._rag_search = _ok_rag
    assembly_step._violin_bvbrc_lookup = _exploding_violin

    out = await assembly_step.process({"query": "EEEV vaccine"})

    assert out["rag_chunks"] == [{"id": 1, "text": "domain chunk"}]
    assert out["violin_mappings"] == []
    assert out["bvbrc_genomes"] == []
    assert out["publications"] == []


async def test_both_branches_failing_returns_empty_bundles_not_crash(assembly_step):
    """All retrieval branches failing → empty bundles, no crash.

    The synthesizer's ``fail_on_empty_retrieval`` gate is responsible
    for surfacing "everything was empty" — this step's job is to
    deliver a consistent shape regardless of branch outcomes.
    """

    def _fail_rag(query: str):
        raise RuntimeError("rag corruption")

    def _fail_violin(terms):
        raise RuntimeError("violin corruption")

    assembly_step._rag_search = _fail_rag
    assembly_step._violin_bvbrc_lookup = _fail_violin

    out = await assembly_step.process({"query": "EEEV"})

    assert out["rag_chunks"] == []
    assert out["violin_mappings"] == []
    assert out["bvbrc_genomes"] == []
    assert out["publications"] == []
    assert out["query"] == "EEEV"


async def test_wrong_shape_entities_raises_clearly(assembly_step):
    """``entities`` passed as a non-list raises ValueError with the
    actual shape echoed back. Detection signal: an upstream step that
    accidentally packs entities as a JSON-encoded string would otherwise
    silently fall through to whitespace tokenization, producing an
    empty / wrong VIOLIN lookup with no surface error.
    """
    import pytest

    with pytest.raises(ValueError, match="'entities' must be a list or None"):
        await assembly_step.process({"query": "ok", "entities": "EEEV,WEEV"})


async def test_wrong_shape_query_terms_raises_clearly(assembly_step):
    import pytest

    with pytest.raises(ValueError, match="'query_terms' must be a list or None"):
        await assembly_step.process({"query": "ok", "query_terms": "EEEV"})


async def test_all_branches_succeeding_returns_full_bundle(assembly_step):
    """Sanity: when nothing fails, the original happy path still works."""

    def _ok_rag(query: str):
        return [{"text": "alphavirus chunk", "score": 0.92}]

    def _ok_violin(terms):
        return (
            [{"synonym_id": "VIOLIN_pathogen_5", "canonical_term": "11036"}],
            [{"genome_id": "11036.7", "genome_name": "EEEV strain"}],
        )

    assembly_step._rag_search = _ok_rag
    assembly_step._violin_bvbrc_lookup = _ok_violin

    out = await assembly_step.process({"query": "EEEV vaccine"})

    assert out["rag_chunks"] == [{"text": "alphavirus chunk", "score": 0.92}]
    assert out["violin_mappings"][0]["canonical_term"] == "11036"
    assert out["bvbrc_genomes"][0]["genome_id"] == "11036.7"
    assert out["publications"] == []


async def test_pre_assemble_stage_report_survives_the_rebuild(assembly_step):
    """A ``stage_reports`` entry present on the INPUT (e.g. ``resolve`` appends one at
    order -2 BEFORE this step) must survive assemble's bundle rebuild and still be in
    the output, with assemble's own ``context_assembly`` report appended after it.

    Detection signal: ``synthesis_context_assembly_step.py`` rebuilds the bundle as a
    fresh ``out`` dict and threads only a whitelist of keys through; if ``stage_reports``
    is dropped from that whitelist, the resolve line never reaches the terminal
    ``## Analysis steps`` render. This pins the carry-through.

    Real-data coverage: ``tests/integration/test_viral_epitope_analysis.py`` drives the
    full resolve → map → assemble → hmerge chain; the resolve line appears in the
    rendered ``## Analysis steps`` (verified on a live CHIKV run, 2026-06-17).
    """
    assembly_step._rag_search = lambda query: []
    assembly_step._violin_bvbrc_lookup = lambda terms: ([], [])
    assembly_step._skip_globus = True
    assembly_step._max_globus = 0

    resolve_report = {
        "stage": "resolve",
        "order": -2,
        "markdown": "Resolved 'chikungunya virus' → 'Chikungunya virus' (NCBITaxon_37124).",
        "data": {"resolution_path": "fast"},
    }
    out = await assembly_step.process(
        {"query": "chikungunya E1 epitopes", "stage_reports": [resolve_report]}
    )

    stages = [r["stage"] for r in out["stage_reports"]]
    assert "resolve" in stages, "pre-assemble resolve report was dropped by the rebuild"
    assert "context_assembly" in stages
    # resolve (order -2) must sort ahead of context_assembly (order 1) at render time.
    assert out["stage_reports"][0]["stage"] == "resolve"


def test_skip_violin_and_bvbrc_does_no_tabular_lookups(monkeypatch):
    """skip_violin + skip_bvbrc → ``_violin_bvbrc_lookup`` returns ([], []) and
    NEVER calls the local CSV/TSV lookup functions.

    This is the harmonized epitope path's contract: it retrieves VIOLIN +
    BV-BRC evidence via Globus search, so both local tabular branches are off.
    Detection signal: a regression that drops the skip short-circuit would call
    the patched lookups, which raise — surfacing the failure here.
    """
    from apecx_integration.composition.steps import _violin_bvbrc_lookup as lookup_mod

    def _explode_violin(*a, **k):
        raise AssertionError("lookup_violin must not be called when skip_violin=True")

    def _explode_bvbrc(*a, **k):
        raise AssertionError("lookup_bvbrc must not be called when skip_bvbrc=True")

    monkeypatch.setattr(lookup_mod, "lookup_violin", _explode_violin)
    monkeypatch.setattr(lookup_mod, "lookup_bvbrc", _explode_bvbrc)

    step = _make_bare_step()
    step._skip_violin = True
    step._skip_bvbrc = True

    violin_mappings, bvbrc_genomes = step._violin_bvbrc_lookup([("EEEV", "pathogen")])
    assert violin_mappings == []
    assert bvbrc_genomes == []
