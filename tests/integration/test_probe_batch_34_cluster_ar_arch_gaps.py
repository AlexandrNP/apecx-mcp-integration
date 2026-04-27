"""Probe batch 34 — cluster AR (silent VIOLIN truncation) + harvester
boundary + architectural-gap documentation + composer RAG semantics
(probes 905-929).

User directive 2026-04-27: "Make sure your code paths do not cause
silent failures that would make tests pass but would impede the
actual product use." This batch surfaces three concrete gaps the
previous campaign didn't reach:

  CLUSTER AR — apecx_db_integration.agent.consolidated_synonym_search
  truncates VIOLIN candidate terms to ``[:100]`` per category before
  feeding them to the LLM. Real VIOLIN has 3,507 vaccines / 3,627
  genes / 3,470 vaccine names — 97% of the catalog is invisible to
  every synonym search call. Pre-fix tests passed against synthetic
  small dataframes; production silently mis-matches against the
  alphabetical first 100.

  HARVESTER BOUNDARY — apecx-harvesters is NOT integrated into
  apecx-mcp-integration. The PKG-INFO documents it as
  "publication/metadata loaders (not BV-BRC/VIOLIN)". The user
  directive expects harvester data in the pipeline; the codebase
  has none. Probes lock the boundary so any future integration
  has to come through these.

  ARCHITECTURAL GAP — the violin_bvbrc workflow does synonym
  mapping, NOT response synthesis. Step 7 (result_ranking) is a
  ``ResultCollectionStep`` that dumps JSON files. The user
  expectation that the LLM synthesizes non-trivial responses
  combining BV-BRC + VIOLIN + RAG semantic chunks is NOT met.
  Probes document the absence so any feature build comes through
  these.

  COMPOSER RAG SEMANTICS — the production RAG path
  (``nanobrain.lightweight.component_index.ComponentIndex``) is
  used for COMPOSER component retrieval. Probes 923-929 push
  the semantic stability of that retrieval at adversarial paraphrases.

The probes here document the findings rather than fixing them in
sibling repos (CLAUDE.md scope rule: edits to other repos are
out-of-scope unless explicitly requested). Cluster AR's fix would
need to land in ``apecx-db-integration``; surfacing here so the
user can authorize the cross-repo work.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_VIOLIN_DIR = _WORKSPACE_ROOT / "data" / "violin"


def _require(p: Path) -> Path:
    if not p.is_file():
        pytest.skip(f"real-data file absent: {p}")
    return p


# ---------------------------------------------------------------------------
# Cluster AR: VIOLIN truncation silent-failure documentation — 905-912
# ---------------------------------------------------------------------------


def test_probe_905_real_violin_vaccines_count_exceeds_100() -> None:
    """Real VIOLIN has > 3000 unique vaccines. The truncation cap of
    100 in consolidated_synonym_search would hide 97% of them from
    the LLM. Lock the count so a future VIOLIN snapshot replacement
    that drops below 100 (and thus 'fixes' the symptom) doesn't
    silently mask the bug."""
    import pandas as pd
    p = _require(_VIOLIN_DIR / "Vaccine_Information.csv")
    df = pd.read_csv(p)
    assert df["Vaccine"].nunique(dropna=True) > 100, (
        "PROBE 905: VIOLIN Vaccine.Vaccine count <= 100 — either "
        "snapshot replaced or column shape changed; verify cluster AR "
        "context before declaring fixed."
    )


def test_probe_906_real_violin_genes_count_exceeds_100() -> None:
    import pandas as pd
    p = _require(_VIOLIN_DIR / "Gene_Information.csv")
    df = pd.read_csv(p)
    assert df["Gene_Name"].nunique(dropna=True) > 100, (
        "PROBE 906: VIOLIN Gene_Information.Gene_Name count <= 100"
    )


def test_probe_907_consolidated_synonym_uses_named_constant_post_fix() -> None:
    """Cluster AR fix landed (2026-04-27). Lock the post-fix shape:
    ``consolidated_synonym_search`` references the named
    ``MAX_CANDIDATES_PER_CATEGORY`` constant AND invokes
    ``filter_candidates_by_similarity`` for per-category selection.
    A regression that re-introduces a bare integer literal trips
    this probe."""
    try:
        from apecx_integration.agents.violin_bvbrc import agent as agent_mod
    except ImportError:
        pytest.skip("apecx_db_integration not importable")
    src = inspect.getsource(agent_mod.consolidated_synonym_search)
    assert "MAX_CANDIDATES_PER_CATEGORY" in src, (
        "PROBE 907: post-fix shape regressed — named constant missing"
    )
    assert "filter_candidates_by_similarity" in src, (
        "PROBE 907: post-fix shape regressed — similarity filter missing"
    )


def test_probe_908_get_candidate_terms_returns_full_list_no_truncation() -> None:
    """``get_candidate_terms`` itself does NOT truncate. The bug is
    purely in ``consolidated_synonym_search``'s prompt builder —
    confirm that so any future fix can replace the prompt-side
    truncation with a smarter selector without breaking this helper."""
    try:
        from apecx_integration.agents.violin_bvbrc import get_candidate_terms
    except ImportError:
        pytest.skip("apecx_db_integration not importable")
    import pandas as pd
    # Build a synthetic vaccines dataframe with 250 rows
    df = pd.DataFrame({
        "Vaccine": [f"vaccine_{i:03d}" for i in range(250)],
        "Vaccine_Name": [f"name_{i:03d}" for i in range(250)],
    })
    candidates = get_candidate_terms({"vaccines": df})
    # No truncation here — full set returned (250 vaccine entries)
    assert len(candidates["vaccine"]) >= 250


def test_probe_909_truncation_yields_first_100_alphabetical_or_insertion() -> None:
    """When the LLM sees only the first 100 entries, the bias is
    toward whichever ordering the dataframe column produces. Probe
    documents this — a query for a vaccine alphabetically AFTER
    'a..a' would silently never reach the LLM's candidate list."""
    import pandas as pd
    p = _require(_VIOLIN_DIR / "Vaccine_Information.csv")
    df = pd.read_csv(p)
    vaccines = df["Vaccine"].dropna().astype(str).unique().tolist()
    # First 100 (the only ones the LLM sees) span what range?
    first_100 = vaccines[:100]
    last_100 = vaccines[-100:]
    # Lock the asymmetry as a finding
    assert len(first_100) == 100
    assert len(last_100) == 100
    # The two sets are disjoint — proves the truncation hides
    # genuinely different vaccines
    assert set(first_100).isdisjoint(set(last_100))


def test_probe_910_truncation_emits_warning_post_fix() -> None:
    """Cluster AR fix added a ``logger.warning`` at the truncation
    site so an operator knows when the cap is biting. Lock the
    fix shape — a future PR that silences the warning would
    silently re-create the cluster-AR-class diagnostic gap."""
    try:
        from apecx_integration.agents.violin_bvbrc import agent as agent_mod
    except ImportError:
        pytest.skip("apecx_db_integration not importable")
    src = inspect.getsource(agent_mod.consolidated_synonym_search)
    assert "logger.warning" in src
    # The warning message must mention the named constant so the
    # log line is searchable and unambiguous
    assert "MAX_CANDIDATES_PER_CATEGORY" in src


def test_probe_911_consolidated_uses_filtered_candidates_post_fix() -> None:
    """Cluster AR fix structural marker: the prompt is built from
    ``filtered_candidates`` (the similarity-filtered dict), NOT
    from a bare slice of ``all_candidates``."""
    try:
        from apecx_integration.agents.violin_bvbrc import agent as agent_mod
    except ImportError:
        pytest.skip("apecx_db_integration not importable")
    src = inspect.getsource(agent_mod.consolidated_synonym_search)
    # Fix shape: filtered_candidates is the structure fed to the LLM
    assert "filtered_candidates" in src
    # And the prompt actually uses it
    assert "json.dumps(filtered_candidates" in src


def test_probe_912_truncation_cap_constant_named_post_fix() -> None:
    """Cluster AR fix introduced ``MAX_CANDIDATES_PER_CATEGORY``
    as a module-level constant. Lock the value at 100 — operators
    can override by editing this constant intentionally; a silent
    rewrite to ``[:50]`` or ``[:1000]`` would change behavior
    without a single-line review point."""
    try:
        from apecx_integration.agents.violin_bvbrc import agent as agent_mod
    except ImportError:
        pytest.skip("apecx_db_integration not importable")
    assert hasattr(agent_mod, "MAX_CANDIDATES_PER_CATEGORY")
    assert agent_mod.MAX_CANDIDATES_PER_CATEGORY == 100


# ---------------------------------------------------------------------------
# Harvester boundary — 913-917
# ---------------------------------------------------------------------------


def test_probe_913_apecx_harvesters_not_imported_by_integration() -> None:
    """No file under apecx-mcp-integration/src imports apecx_harvesters.
    Lock the boundary — a future integration must come through here."""
    src_root = (
        _WORKSPACE_ROOT / "apecx-mcp-integration" / "src" / "apecx_integration"
    )
    if not src_root.is_dir():
        pytest.skip("apecx-mcp-integration src not present")
    offenders = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if (
            "from apecx_harvesters" in text
            or "import apecx_harvesters" in text
        ):
            offenders.append(str(py.relative_to(src_root)))
    assert not offenders, (
        f"PROBE 913: apecx_harvesters imported from {offenders} — verify "
        f"the integration is intentional and update probe 917."
    )


def test_probe_914_no_harvester_step_in_violin_workflow() -> None:
    """The violin_bvbrc workflow has no step that pulls harvester
    publication/metadata data. Lock — a future enrichment step
    that uses harvester output must come through here."""
    import yaml
    p = (
        _WORKSPACE_ROOT / "apecx-mcp-integration" / "src" / "apecx_integration"
        / "composition" / "workflows" / "violin_bvbrc"
        / "violin_bvbrc_workflow.yml"
    )
    if not p.is_file():
        pytest.skip("workflow yaml not present")
    wf = yaml.safe_load(p.read_text(encoding="utf-8"))
    classes = []
    for step in (wf.get("steps") or {}).values():
        cls = step.get("class", "")
        classes.append(cls)
    harvester_classes = [c for c in classes if "harvester" in c.lower()]
    assert not harvester_classes, (
        f"PROBE 914: harvester step found in violin_bvbrc workflow: "
        f"{harvester_classes}"
    )


def test_probe_915_result_collection_input_has_no_harvester_data() -> None:
    """The result_ranking step's input DataUnit is wired only to
    verified_synonym_writeback's output. No harvester data flows
    through. Lock the wiring."""
    import yaml
    wf_dir = (
        _WORKSPACE_ROOT / "apecx-mcp-integration" / "src" / "apecx_integration"
        / "composition" / "workflows" / "violin_bvbrc"
    )
    if not wf_dir.is_dir():
        pytest.skip("workflow dir not present")
    wf = yaml.safe_load((wf_dir / "violin_bvbrc_workflow.yml").read_text())
    # Find the link landing on result_ranking.enriched_results_input
    incoming = []
    for link_id, link in (wf.get("links") or {}).items():
        cfg = (link.get("config") or {})
        tgt = cfg.get("target", "")
        if tgt.startswith("result_ranking."):
            src = cfg.get("source", "")
            incoming.append((link_id, src))
    # Exactly one incoming link, from verified_synonym_writeback
    assert len(incoming) == 1
    assert "verified_synonym_writeback" in incoming[0][1]


def test_probe_916_datacite_references_only_in_rag_synthesis() -> None:
    """Boundary invariant for DataCite-shaped publication metadata.

    Originally written (2026-04-26) as a "no DataCite anywhere" lock:
    the harvester emits DataCite records but no production code path
    consumed them, so the lock forced any future integration to come
    through this test deliberately.

    2026-04-27: rag_synthesis legitimately integrates DataCite-shaped
    publications via the ``_render_publications`` pathway (DOIs are
    the only stable citation token DataCite carries). The lock has
    served its purpose; converting it to a BOUNDARY invariant is more
    useful long-term: DataCite references are now allowed ONLY inside
    ``agents/rag_synthesis/``. This prevents the metadata shape from
    sprawling into ``db_integration`` / ``control_plane`` / etc., where
    a leak would mean DataCite is being treated as a first-class
    type rather than a renderer-local concept.

    If a future commit needs DataCite outside ``agents/rag_synthesis/``,
    update this allowlist deliberately and document why in the commit.
    """
    src_root = (
        _WORKSPACE_ROOT / "apecx-mcp-integration" / "src" / "apecx_integration"
    )
    if not src_root.is_dir():
        pytest.skip("src not present")
    allowed_prefixes = ("agents/rag_synthesis/",)
    offenders = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "DataCite" in text or "datacite" in text:
            rel = str(py.relative_to(src_root))
            if not any(rel.startswith(p) for p in allowed_prefixes):
                offenders.append(rel)
    assert not offenders, (
        f"PROBE 916: DataCite reference leaked outside the allowlist "
        f"{allowed_prefixes!r} — found in {offenders}. The DataCite "
        f"shape is renderer-local to ``agents/rag_synthesis/`` (it is "
        f"NOT a first-class type in this package). If you intentionally "
        f"need DataCite knowledge elsewhere, extend the allowlist in "
        f"this probe with a justification in the commit message."
    )


def test_probe_917_pkg_info_documents_harvester_as_unintegrated() -> None:
    """The PKG-INFO carries the documented boundary: 'publication/
    metadata loaders (not BV-BRC/VIOLIN)'. Lock the documentation
    so a future integration must update it."""
    pkg = (
        _WORKSPACE_ROOT / "apecx-mcp-integration" / "src"
        / "apecx_integration.egg-info" / "PKG-INFO"
    )
    if not pkg.is_file():
        pytest.skip("PKG-INFO not present (build artifact)")
    text = pkg.read_text(encoding="utf-8")
    assert "apecx-harvesters" in text
    assert "not BV-BRC/VIOLIN" in text or "publication/metadata" in text


# ---------------------------------------------------------------------------
# Architectural-gap documentation — 918-922
# ---------------------------------------------------------------------------


def test_probe_918_step_7_is_result_collection_not_synthesizer() -> None:
    """Step 7 (result_ranking) reuses ``ResultCollectionStep`` — a
    file collector, not an LLM synthesizer. Lock the structural
    fact: the workflow's terminal step does NOT synthesize a
    natural-language response."""
    import yaml
    p = (
        _WORKSPACE_ROOT / "apecx-mcp-integration" / "src" / "apecx_integration"
        / "composition" / "workflows" / "violin_bvbrc"
        / "steps" / "result_ranking.yml"
    )
    if not p.is_file():
        pytest.skip("step yaml not present")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert cfg["class"].endswith("ResultCollectionStep")


def test_probe_919_result_collection_step_calls_no_llm() -> None:
    """ResultCollectionStep.execute does NOT call any LLM —
    confirms that workflow output is purely file aggregation,
    not synthesis."""
    try:
        from nanobrain.library.workflows.viral_protein_analysis.steps import (
            result_collection_step as rcs_mod,
        )
    except ImportError:
        pytest.skip("nanobrain ResultCollectionStep not importable")
    src = inspect.getsource(rcs_mod)
    # No LLM-related imports / calls
    assert "ChatOpenAI" not in src
    assert "llm.invoke" not in src
    assert "HumanMessage" not in src
    assert "SystemMessage" not in src


def test_probe_920_workflow_has_no_synthesis_step_with_llm_and_rag() -> None:
    """The user expectation: an end-to-end step that combines
    BV-BRC genome data + VIOLIN vaccine data + RAG semantic chunks
    via LLM synthesis. Probe walks every step config; none has
    that shape."""
    import yaml
    wf_dir = (
        _WORKSPACE_ROOT / "apecx-mcp-integration" / "src" / "apecx_integration"
        / "composition" / "workflows" / "violin_bvbrc"
    )
    if not wf_dir.is_dir():
        pytest.skip("workflow dir absent")
    wf = yaml.safe_load((wf_dir / "violin_bvbrc_workflow.yml").read_text())
    synthesis_step = None
    for step_id, step in wf["steps"].items():
        cls = step["class"]
        # A synthesis step would have "synthesis" or "rag" or
        # "summarize" or "answer" in its name/class
        if any(kw in cls.lower() for kw in ("synthes", "summari", "answer")):
            synthesis_step = (step_id, cls)
            break
    assert synthesis_step is None, (
        f"PROBE 920: synthesis step found {synthesis_step} — verify "
        "and remove this probe."
    )


def test_probe_921_composer_rag_path_is_for_component_retrieval_only() -> None:
    """The production RAG path (``ComponentIndex``) is for COMPOSER
    component retrieval — picking which library steps to wire when
    generating a workflow YAML. It is NOT a user-response synthesis
    path. Lock the documentation."""
    try:
        from nanobrain.lightweight import component_index as ci_mod
    except ImportError:
        pytest.skip("ComponentIndex not importable")
    docstr = ci_mod.__doc__ or ""
    # Module docstring describes its role
    assert "composer" in docstr.lower() or "component" in docstr.lower()
    # NOT described as user-response synthesis
    assert "user response" not in docstr.lower()
    assert "answer synthes" not in docstr.lower()


def test_probe_922_apecx_rag_prototype_not_integrated() -> None:
    """The apecx-rag/ sibling repo has a real LangGraph RAG agent
    team but it is NOT integrated into apecx-mcp-integration. Lock
    the boundary — a future integration must come through here."""
    rag_dir = _WORKSPACE_ROOT / "apecx-rag"
    if not rag_dir.is_dir():
        pytest.skip("apecx-rag prototype absent")
    src_root = (
        _WORKSPACE_ROOT / "apecx-mcp-integration" / "src" / "apecx_integration"
    )
    if not src_root.is_dir():
        pytest.skip("apecx-mcp-integration src absent")
    offenders = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "from apecx_rag" in text or "import apecx_rag" in text:
            offenders.append(str(py.relative_to(src_root)))
        if "RAGAgentTeam" in text:
            offenders.append(str(py.relative_to(src_root)))
    assert not offenders, (
        f"PROBE 922: apecx-rag integrated via {offenders} — verify "
        "and update this probe."
    )


# ---------------------------------------------------------------------------
# Composer RAG semantic stability — 923-929
# ---------------------------------------------------------------------------


def _model_cached() -> bool:
    """Has the sentence-transformers all-mpnet-base-v2 model been
    downloaded? If not, the probes that need it skip."""
    try:
        from huggingface_hub import scan_cache_dir
        scan = scan_cache_dir()
        return any(
            "all-mpnet-base-v2" in repo.repo_id
            for repo in scan.repos
        )
    except Exception:
        return False


_HEAVY_RAG_PROBES = pytest.mark.skipif(
    not _model_cached(),
    reason="sentence-transformers/all-mpnet-base-v2 not cached",
)


def _build_synth_index(tmp_path, entries):
    import yaml as _yaml
    from nanobrain.lightweight.component_index import ComponentIndex
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "manifest.yml"
    manifest = {
        "workflow": {"name": "synth", "spec": "test"},
        "components": entries,
    }
    manifest_path.write_text(_yaml.safe_dump(manifest), encoding="utf-8")
    idx = ComponentIndex()
    idx.rebuild(manifest_paths=[manifest_path], library_version="0.1.0-test")
    return idx


def _entry(step_id, name, desc, examples):
    return {
        "step_id": step_id,
        "step_name": name,
        "class": f"test.{name}",
        "yaml": f"steps/{name}.yml",
        "rag_description": desc,
        "rag_examples": list(examples),
    }


@_HEAVY_RAG_PROBES
def test_probe_923_paraphrase_stability(tmp_path) -> None:
    """A paraphrased query should hit the same top-1 component as
    the canonical phrasing — semantic stability under paraphrase
    is the whole point of an embedding-based retriever."""
    idx = _build_synth_index(tmp_path / "p923", [
        _entry("1", "fasta_reader",
               "Reads FASTA-format protein sequence files.",
               ["read fasta", "load proteins"]),
        _entry("2", "csv_reader",
               "Reads CSV files with row-per-record structure.",
               ["read csv", "load tabular data"]),
        _entry("3", "synonym_lookup",
               "Resolves alternative names for biological entities.",
               ["synonyms", "alternative names"]),
    ])
    canonical = idx.search("read FASTA file", k=1)[0]
    paraphrase_a = idx.search("load a protein FASTA", k=1)[0]
    paraphrase_b = idx.search("parse FASTA format", k=1)[0]
    assert canonical.id == paraphrase_a.id == paraphrase_b.id


@_HEAVY_RAG_PROBES
def test_probe_924_domain_separation(tmp_path) -> None:
    """A vaccine query should NOT match a genomic-annotation
    component, even though both are in the biomedical domain.
    Locks adversarial robustness against domain blur."""
    idx = _build_synth_index(tmp_path / "p924", [
        _entry("1", "vaccine_lookup",
               "Look up vaccine information from VIOLIN.",
               ["find vaccine info", "vaccine details"]),
        _entry("2", "genome_annotation",
               "Annotate genomic features in BV-BRC.",
               ["annotate genome", "genomic features"]),
    ])
    vaccine_top = idx.search("find vaccine information", k=1)[0]
    genome_top = idx.search("annotate genome features", k=1)[0]
    assert vaccine_top.name == "vaccine_lookup"
    assert genome_top.name == "genome_annotation"


@_HEAVY_RAG_PROBES
def test_probe_925_capitalization_invariant(tmp_path) -> None:
    """Capitalization should not change top-1 selection — the
    embedder is case-insensitive in practice."""
    idx = _build_synth_index(tmp_path / "p925", [
        _entry("1", "fasta_reader",
               "Reads FASTA files.", ["read fasta"]),
        _entry("2", "json_reader",
               "Reads JSON files.", ["read json"]),
    ])
    lower = idx.search("read fasta file", k=1)[0]
    upper = idx.search("READ FASTA FILE", k=1)[0]
    mixed = idx.search("Read FASTA File", k=1)[0]
    assert lower.id == upper.id == mixed.id


@_HEAVY_RAG_PROBES
def test_probe_926_punctuation_invariant(tmp_path) -> None:
    """Punctuation should not flip top-1 selection."""
    idx = _build_synth_index(tmp_path / "p926", [
        _entry("1", "fasta_reader",
               "Reads FASTA files.", ["read fasta"]),
        _entry("2", "csv_reader",
               "Reads CSV files.", ["read csv"]),
    ])
    plain = idx.search("read FASTA file", k=1)[0]
    punct = idx.search("read FASTA file?!", k=1)[0]
    assert plain.id == punct.id


@_HEAVY_RAG_PROBES
def test_probe_927_similarity_score_well_calibrated(tmp_path) -> None:
    """For a clear-match query, similarity > 0.5. For an unrelated
    query against the same corpus, similarity < clear-match score
    by a meaningful margin (> 0.05)."""
    idx = _build_synth_index(tmp_path / "p927", [
        _entry("1", "vaccine_lookup",
               "Look up vaccine information from VIOLIN.",
               ["find vaccine info"]),
    ])
    clear = idx.search("vaccine information lookup", k=1)[0]
    unrelated = idx.search("compute fluid dynamics", k=1)[0]
    assert clear.similarity > 0.5
    assert clear.similarity > unrelated.similarity + 0.05


@_HEAVY_RAG_PROBES
def test_probe_928_save_load_preserves_search_results(tmp_path) -> None:
    """A persisted-then-reloaded index must produce identical
    top-K results — the FAISS file + metadata.json round-trip
    is load-bearing for the operator workflow."""
    from nanobrain.lightweight.component_index import ComponentIndex
    idx = _build_synth_index(tmp_path / "p928", [
        _entry("1", "fasta_reader", "Reads FASTA files.", ["read fasta"]),
        _entry("2", "csv_reader", "Reads CSV files.", ["read csv"]),
        _entry("3", "json_reader", "Reads JSON files.", ["read json"]),
    ])
    persisted = tmp_path / "p928_saved"
    idx.save(persisted)
    loaded = ComponentIndex.load(persisted)
    a = [(h.name, round(h.similarity, 6)) for h in idx.search("read FASTA", k=3)]
    b = [(h.name, round(h.similarity, 6)) for h in loaded.search("read FASTA", k=3)]
    assert a == b


@_HEAVY_RAG_PROBES
def test_probe_929_rag_index_top1_robust_under_long_query(tmp_path) -> None:
    """A query padded with unrelated text must still rank the
    correct component as top-1. This is what happens in real use:
    a scientist's query has a clear intent buried in extra
    context."""
    idx = _build_synth_index(tmp_path / "p929", [
        _entry("1", "fasta_reader",
               "Reads FASTA files.", ["read fasta"]),
        _entry("2", "csv_reader",
               "Reads CSV files.", ["read csv"]),
    ])
    short = idx.search("read FASTA file", k=1)[0]
    padded = idx.search(
        "I am working on a research paper about alphavirus genomes "
        "and I need to read FASTA file content for protein sequence "
        "analysis. Please help me find the right component.",
        k=1,
    )[0]
    assert short.id == padded.id
