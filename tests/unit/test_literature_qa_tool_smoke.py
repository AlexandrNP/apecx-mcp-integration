"""Smoke + gated-integration tests for the ``literature_qa`` MCP tool.

OFFLINE (always runs, no network/LLM/faiss):
  - the tool module imports without the optional ``rag`` extra at module scope;
  - the server registers ``literature_qa`` (it appears in ``list_tools()``);
  - with ``harvest_and_stamp`` + ``resolve_organism_to_iri`` + the reader seam
    monkeypatched, the tool returns the documented dict shape with the correct
    ``n_papers_*`` counts and status for the ok / no_evidence / unresolved paths.

GATED INTEGRATION (``@pytest.mark.slow``; skips when PubMed OR Ollama unreachable):
  - a real ``literature_qa("Chikungunya virus", ...)`` returns a grounded answer
    with real numeric PMIDs, OR honestly degrades — asserted against the degrade
    contract, never faked to "ok".
"""

from __future__ import annotations

import asyncio
import os
import sys
import urllib.error
import urllib.request

import pytest

from apecx_integration.mcp_surface.tools import literature_qa as tool

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_EUTILS_PROBE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"
_OLLAMA_PROBE = "http://localhost:11434/api/tags"


# --------------------------------------------------------------------------- #
# OFFLINE                                                                      #
# --------------------------------------------------------------------------- #
def test_module_imports_without_rag_extra():
    """Importing the tool must not have pulled in faiss / sentence_transformers."""
    # The module is already imported at the top of this file; assert the heavy
    # extras were NOT dragged in as a side effect of that import.
    assert "faiss" not in sys.modules
    assert "sentence_transformers" not in sys.modules


def test_server_registers_literature_qa():
    """The FastMCP server exposes ``literature_qa`` in its tool list."""
    from apecx_integration.mcp_surface.server import build_server

    server = build_server()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "literature_qa" in names


def _stamped(pmid: str, iris: list[str]) -> dict:
    return {
        "pmid": pmid,
        "title": f"paper {pmid}",
        "abstract": f"abstract {pmid}",
        "iris": iris,
    }


def test_literature_qa_ok_shape_with_fakes(monkeypatch):
    """Two stamped fixtures (one matching the resolved IRI) + a fake reader ->
    the documented dict shape, correct counts, status=ok, NO network/LLM."""
    fixtures = [
        _stamped("111", [_CHIKV_IRI]),  # matches -> kept
        _stamped(
            "222", ["http://purl.obolibrary.org/obo/NCBITaxon_11036"]
        ),  # look-alike -> dropped
    ]
    monkeypatch.setattr(tool, "resolve_organism_to_iri", lambda term, *a, **k: _CHIKV_IRI)
    monkeypatch.setattr(tool, "_get_gazetteer", lambda: object())  # never really used
    monkeypatch.setattr(tool, "harvest_and_stamp", lambda term, gaz, *, max_papers: list(fixtures))

    async def _fake_reader(question, filtered_records):
        # Only the matching paper should reach the reader.
        assert [r["pmid"] for r in filtered_records] == ["111"]
        return {"answer": "CHIKV is neutralized. [PMID:111]", "citations": ["111"], "status": "ok"}

    monkeypatch.setattr(tool, "_read_filtered", _fake_reader)

    result = asyncio.run(tool.literature_qa("Chikungunya virus", "What neutralizes CHIKV?", 20))

    assert result == {
        "answer": "CHIKV is neutralized. [PMID:111]",
        "citations": ["111"],
        "n_papers_considered": 2,
        "n_papers_after_filter": 1,
        "organism_iri": _CHIKV_IRI,
        "status": "ok",
    }


def test_literature_qa_no_evidence_when_filter_empties(monkeypatch):
    """Papers harvested but none carry the resolved IRI -> no_evidence, no reader call."""
    fixtures = [_stamped("222", ["http://purl.obolibrary.org/obo/NCBITaxon_11036"])]
    monkeypatch.setattr(tool, "resolve_organism_to_iri", lambda term, *a, **k: _CHIKV_IRI)
    monkeypatch.setattr(tool, "_get_gazetteer", lambda: object())
    monkeypatch.setattr(tool, "harvest_and_stamp", lambda term, gaz, *, max_papers: list(fixtures))

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("reader must not run when the filter empties")

    monkeypatch.setattr(tool, "_read_filtered", _boom)

    result = asyncio.run(tool.literature_qa("Chikungunya virus", "q?", 8))
    assert result["status"] == "no_evidence"
    assert result["n_papers_considered"] == 1
    assert result["n_papers_after_filter"] == 0
    assert result["organism_iri"] == _CHIKV_IRI
    assert result["answer"] == "" and result["citations"] == []


def test_literature_qa_unresolved_organism(monkeypatch):
    """Organism that does not resolve -> unresolved_organism, no harvest."""
    monkeypatch.setattr(tool, "resolve_organism_to_iri", lambda term, *a, **k: None)

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("harvest must not run when the organism is unresolved")

    monkeypatch.setattr(tool, "harvest_and_stamp", _boom)

    result = asyncio.run(tool.literature_qa("Nonexistent blahvirus", "q?", 8))
    assert result == {
        "answer": "",
        "citations": [],
        "n_papers_considered": 0,
        "n_papers_after_filter": 0,
        "organism_iri": None,
        "status": "unresolved_organism",
    }


def test_literature_qa_empty_question_raises():
    """Empty question is a wiring bug (mirrors the reader step's own contract)."""
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(tool.literature_qa("Chikungunya virus", "   ", 8))


# --------------------------------------------------------------------------- #
# GATED INTEGRATION                                                            #
# --------------------------------------------------------------------------- #
def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(
    not os.path.exists(os.path.expanduser("~/.apecx/dictionary/dictionary.sqlite")),
    reason="synonym dictionary not present",
)
def test_literature_qa_real_chikv():
    if not _reachable(_EUTILS_PROBE):
        pytest.skip("PubMed eUtils unreachable")
    if not _reachable(_OLLAMA_PROBE):
        pytest.skip("Ollama unreachable")

    result = asyncio.run(
        tool.literature_qa(
            "Chikungunya virus", "What antibodies neutralize Chikungunya virus?", max_papers=8
        )
    )

    assert result["organism_iri"] == _CHIKV_IRI
    assert result["status"] in {"ok", "no_evidence", "synthesis_failed"}

    if result["status"] == "ok":
        # Grounded answer with real numeric PMIDs.
        assert result["answer"].strip()
        assert result["citations"]
        assert all(str(pmid).isdigit() for pmid in result["citations"])
        assert result["n_papers_after_filter"] >= 1
    else:
        # Honest degrade — assert the contract, do NOT fake an ok.
        assert result["answer"] == ""
        assert result["citations"] == []
