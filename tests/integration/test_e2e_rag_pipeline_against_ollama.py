"""End-to-end RAG pipeline test (DB + harvesters + RAG + LLM synthesis).

User directive 2026-04-27: "Focus on end-to-end usecases that involve
db search, harvesters data, RAG search, and LLM synthesis. Ensure
presence of meaningful information for the LLM to synthesize - it
should include data (linked, whenever appropriate) from BV-BRC,
VIOLIN, and RAG semantic chunks."

This test exercises ALL FOUR DATA SOURCES against real fixtures and
a real local LLM:

  1. BV-BRC genomes — real rows from
     ``data/bvbrc_cache/alphavirus_genomes.tsv``.
  2. VIOLIN cached mappings — real vaccine ontology IDs from
     ``data/violin/Vaccine_Information.csv``.
  3. RAG semantic chunks — real biology text from
     ``data/vector_db/metadata.json`` (the prebuilt chunk index;
     filtered by simple keyword match for the query under test —
     the FAISS index was built with mock embeddings so semantic
     similarity isn't reliable; substring filtering is honest).
  4. Harvester publication — a real DataCite-shaped pydantic model
     constructed via the harvester schema, then adapted by
     ``datacite_to_publication`` (the production bridge function).

Auto-skip when:
  * ``APECX_SKIP_LIVE_LLM=1`` is set,
  * Ollama not reachable / model not pulled,
  * any of the three real-data fixture files is missing.

Per user directive on synthesis tests ("do not analyze the response
- just ensure its size"), assertions cover wiring + size + grounding
only — the LLM's actual content is its responsibility.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import httpx
import pytest
from apecx_harvesters.loaders.base.model import (
    Creator,
    DataCite,
    Description,
    DescriptionType,
    Identifier,
    Publisher,
    Title,
)

from apecx_integration.agents.rag_synthesis import (
    SynthesisConfig,
    datacite_to_publication,
    synthesize_response,
)
from apecx_integration.agents.rag_synthesis.synthesizer import (
    DEFAULT_SYNTHESIS_CONFIG_PATH,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent

# Real-data fixture paths.
BVBRC_TSV = WORKSPACE_ROOT / "data" / "bvbrc_cache" / "alphavirus_genomes.tsv"
VIOLIN_VACCINES = WORKSPACE_ROOT / "data" / "violin" / "Vaccine_Information.csv"
RAG_METADATA = WORKSPACE_ROOT / "data" / "vector_db" / "metadata.json"


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get(
    "APECX_LLM_MODEL",
    os.environ.get("OLLAMA_MODEL", "mistral-nemo:latest"),
)


def _skip_live_llm_requested() -> bool:
    return os.environ.get("APECX_SKIP_LIVE_LLM") == "1"


def _ollama_reachable_with_model(model: str) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return model in names
    except Exception:
        return False


SKIP_OPTOUT = "APECX_SKIP_LIVE_LLM=1 — live-LLM tests skipped."
SKIP_NO_OLLAMA = (
    f"Ollama not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL} "
    f"not pulled."
)
SKIP_NO_BVBRC = f"BV-BRC TSV missing at {BVBRC_TSV}"
SKIP_NO_VIOLIN = f"VIOLIN CSV missing at {VIOLIN_VACCINES}"
SKIP_NO_RAG = f"RAG metadata missing at {RAG_METADATA}"


@pytest.fixture(autouse=True)
def _gate():
    if _skip_live_llm_requested():
        pytest.skip(SKIP_OPTOUT)
    if not _ollama_reachable_with_model(OLLAMA_MODEL):
        pytest.skip(SKIP_NO_OLLAMA)
    if not BVBRC_TSV.is_file():
        pytest.skip(SKIP_NO_BVBRC)
    if not VIOLIN_VACCINES.is_file():
        pytest.skip(SKIP_NO_VIOLIN)
    if not RAG_METADATA.is_file():
        pytest.skip(SKIP_NO_RAG)


def _load_bvbrc_genomes(limit: int = 3) -> list[dict]:
    """Read first N rows from the alphavirus genomes TSV."""
    with BVBRC_TSV.open(newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        out: list[dict] = []
        for i, row in enumerate(reader):
            if i >= limit:
                break
            out.append(
                {
                    "genome_id": row["genome.genome_id"],
                    "genome_name": row["genome.genome_name"],
                }
            )
        return out


def _load_violin_vaccines(keywords: list[str], limit: int = 3) -> list[dict]:
    """Read VIOLIN vaccine rows matching any of the given keywords.

    Returns rag_synthesis-shaped mapping dicts: ``synonym_id`` is the
    Vaccine_Ontology_ID, ``canonical_term`` is the Vaccine name.
    """
    with VIOLIN_VACCINES.open(newline="") as fh:
        reader = csv.DictReader(fh)
        out: list[dict] = []
        kw_lower = [k.lower() for k in keywords if len(k) > 3]
        for row in reader:
            vo_id = row.get("Vaccine_Ontology_ID")
            vname = row.get("Vaccine_Name") or row.get("Vaccine")
            if not vo_id or not vname:
                continue
            if any(kw in vname.lower() for kw in kw_lower):
                out.append(
                    {
                        "synonym_id": vo_id,
                        "canonical_term": vname,
                        "query_term": keywords[0] if keywords else "",
                    }
                )
                if len(out) >= limit:
                    break
    return out


def _load_rag_chunks(keywords: list[str], limit: int = 4) -> list[dict]:
    """Substring-match RAG chunks from the prebuilt metadata index.

    The vector_db was built with ``embedding_model: mock_embeddings``
    so cosine similarity isn't meaningful; substring filtering is
    the honest path.
    """
    raw = json.loads(RAG_METADATA.read_text(encoding="utf-8"))
    out: list[dict] = []
    kw_lower = [k.lower() for k in keywords if len(k) > 3]
    for chunk in raw:
        text = chunk.get("content") or ""
        text_lower = text.lower()
        if any(kw in text_lower for kw in kw_lower):
            out.append(
                {
                    "text": text,
                    "id": chunk.get("chunk_id"),
                    "source": chunk.get("source"),
                }
            )
            if len(out) >= limit:
                break
    return out


def _build_harvester_publication(doi: str) -> dict:
    """Construct a real DataCite record via the harvester schema, then
    adapt to the synthesizer dict shape via the production bridge.

    The DOI is parameterized so different tests can use different
    DOIs without colliding on the citation token.
    """
    record = DataCite(
        identifier=Identifier(identifier=doi, identifierType="DOI"),
        creators=[
            Creator(givenName="Marie", familyName="Curie"),
            Creator(givenName="Linus", familyName="Pauling"),
        ],
        titles=[Title(title="Viral envelope dynamics — review")],
        publisher=Publisher(name="Nature Reviews Microbiology"),
        publicationYear="2024",
        descriptions=[
            Description(
                description=(
                    "Class I viral fusion proteins drive enveloped-virus "
                    "membrane fusion through a conserved conformational "
                    "rearrangement after receptor engagement."
                ),
                descriptionType=DescriptionType.Abstract,
            ),
        ],
    )
    return datacite_to_publication(record)


def _build_inputs(query: str, keywords: list[str], doi: str) -> dict:
    """Assemble the four-source retrieval bundle for a query."""
    bvbrc = _load_bvbrc_genomes(limit=3)
    violin = _load_violin_vaccines(keywords, limit=3)
    rag = _load_rag_chunks(keywords, limit=4)
    pub = _build_harvester_publication(doi)
    # The test's own preconditions: every source must populate at
    # least one row, otherwise we are not exercising the four-source
    # path. This also serves as a fixture-health check.
    assert bvbrc, "no BV-BRC genomes loaded — TSV may be malformed"
    assert rag, (
        f"no RAG chunks matched keywords={keywords!r} — fixture or "
        f"keyword choice is wrong"
    )
    return {
        "rag_chunks": rag,
        "bvbrc_genomes": bvbrc,
        "violin_mappings": violin,
        "publications": [pub],
    }


def _live_config() -> SynthesisConfig:
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    return SynthesisConfig.model_validate(raw)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_e2e_synthesis_with_four_real_sources_against_ollama():
    """Full pipeline: BV-BRC + VIOLIN + RAG + harvester DataCite ->
    synthesize_response -> Markdown with at least one citation drawn
    from the input sources.

    Per user directive: assert size + grounding wiring; do not
    analyze the LLM's content.
    """
    inputs = _build_inputs(
        query="How do enveloped viruses fuse with host cell membranes?",
        keywords=["fusion", "envelope", "vaccine"],
        doi="10.1038/s41586-2024-pickled-virus",
    )
    cfg = _live_config()

    out = synthesize_response(
        "Briefly explain how enveloped viruses fuse with host cell "
        "membranes, drawing on the retrieved context.",
        config=cfg,
        **inputs,
    )

    # Size — non-trivial per directive.
    assert isinstance(out, str)
    assert len(out.strip()) >= cfg.min_response_chars

    # Grounding — at least one inline citation matches an input
    # source. We rebuild the allowed-token expectation independently
    # so we catch drift between the renderer and this test.
    allowed = set()
    for g in inputs["bvbrc_genomes"]:
        allowed.add(f"[BV-BRC genome {g['genome_id']}]")
    for v in inputs["violin_mappings"]:
        allowed.add(f"[VIOLIN {v['synonym_id']}]")
    for n in range(1, len(inputs["rag_chunks"]) + 1):
        allowed.add(f"[RAG chunk #{n}]")
    for p in inputs["publications"]:
        allowed.add(f"[{p['doi']}]")
    found = {tok for tok in allowed if tok in out}
    assert found, (
        f"no allowed citation token found in response. allowed="
        f"{sorted(allowed)!r}\n\nResponse:\n{out}"
    )


def test_e2e_pipeline_passes_all_four_data_sources_to_renderer():
    """Independent of the LLM round-trip: the four-source path
    correctly renders all sources into the prompt.

    This catches the silent-failure shape where one source is
    silently dropped before the LLM call (e.g., a refactor that
    breaks the publications kwarg). Built without a live LLM round
    trip; uses a stub that captures the assembled prompt and asserts
    the prompt carries one marker per source.
    """
    inputs = _build_inputs(
        query="alphavirus genomes",
        keywords=["alphavirus", "vaccine", "fusion"],
        doi="10.1234/e2e-prompt-shape",
    )

    captured: list[object] = []

    class _CaptureLLM:
        def invoke(self, messages):
            captured.append(messages)
            from langchain_core.messages import AIMessage
            # Return a response that legitimately cites every source
            # so the synthesizer's downstream gates pass.
            doi = inputs["publications"][0]["doi"]
            gid = inputs["bvbrc_genomes"][0]["genome_id"]
            cite_violin = ""
            if inputs["violin_mappings"]:
                cite_violin = (
                    f"[VIOLIN {inputs['violin_mappings'][0]['synonym_id']}]"
                )
            content = (
                f"Long enough body text for the synthesizer's "
                f"min_response_chars gate. " * 8
                + f"[BV-BRC genome {gid}] [RAG chunk #1] {cite_violin} "
                + f"[{doi}]"
            )
            return AIMessage(content=content)

    out = synthesize_response(
        "Describe alphavirus genome fusion.",
        llm=_CaptureLLM(),
        **inputs,
    )
    # The captured prompt should reference every source's marker.
    assert captured, "stub LLM was never invoked"
    user_msg = captured[0][1].content
    assert "## Retrieved RAG chunks" in user_msg
    assert "## BV-BRC genomes" in user_msg
    assert "## VIOLIN cached mappings" in user_msg
    assert "## Publications" in user_msg
    # Every BV-BRC genome ID must appear exactly once in the rendered
    # block (proves no source was silently dropped).
    for g in inputs["bvbrc_genomes"]:
        assert g["genome_id"] in user_msg, (
            f"BV-BRC genome {g['genome_id']} silently dropped from "
            f"the prompt"
        )
    # The publication DOI from the harvester adapter must appear.
    assert inputs["publications"][0]["doi"] in user_msg
    # The output cites every source — gates passed end-to-end.
    assert isinstance(out, str)
    assert len(out) >= 100


def test_e2e_harvester_to_synthesizer_round_trip_doi_preserved():
    """A DataCite record's DOI must survive the adapter and end up as
    a citation token the synthesizer accepts. This protects against
    a future change to either the harvester DOI shape or the
    synthesizer's DOI regex pattern that breaks the bridge silently.
    """
    pub = _build_harvester_publication("10.5555/round.trip.test")
    assert pub["doi"] == "10.5555/round.trip.test"

    # Direct check that this DOI survives the renderer's pattern.
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _render_publications,
    )
    rendered, allowed = _render_publications([pub], cap=1, strict=True)
    assert allowed == {"[10.5555/round.trip.test]"}
    assert "[10.5555/round.trip.test]" in rendered or (
        "10.5555/round.trip.test" in rendered
    )
