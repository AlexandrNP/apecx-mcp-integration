"""Full-text retrieval (Europe PMC) for grounded literature records.

Proves the open-access full-text path end to end at the unit/seam level:
  * the Europe PMC client fetches + extracts real body text for an OA PMCID (live),
  * the pipeline's _attach_fulltext populates record["full_text"] for OA records (live),
  * a systemic Europe PMC outage degrades LOUD to abstracts (no-network monkeypatch),
  * the literature step grounds on full_text when present, else abstract (no-network).

Live tests auto-skip when Europe PMC is unreachable. CC-1: the happy path asserts real,
non-empty body text. (The broader literature_qa end-to-end — harvest -> stamp -> fetch ->
host evidence — is exercised by run_grounded.py against real PubMed.)
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.integration

# A stable, large open-access PMC article (Nipah review) — verified to carry full-text body.
_OA_PMCID = "PMC13134746"


def _europepmc_reachable() -> bool:
    from apecx_integration.agents.literature.fulltext import (
        FulltextUnavailableError,
        fetch_fulltext,
    )

    try:
        return bool(asyncio.run(fetch_fulltext(_OA_PMCID, char_cap=2000)))
    except FulltextUnavailableError:
        return False
    except Exception:
        return False


needs_europepmc = pytest.mark.skipif(
    not _europepmc_reachable(), reason="Europe PMC full-text API unreachable"
)


@needs_europepmc
def test_fetch_fulltext_returns_capped_body_for_oa_pmcid():
    """CC-1: a real OA PMCID yields non-empty body text, capped to char_cap."""
    from apecx_integration.agents.literature.fulltext import fetch_fulltext_many

    out = asyncio.run(fetch_fulltext_many([_OA_PMCID], char_cap=5000))
    assert _OA_PMCID in out, "expected full text for a known open-access PMCID"
    body = out[_OA_PMCID]
    assert 0 < len(body) <= 5000, len(body)
    # Real body text, not XML tags left behind.
    assert "<" not in body and ">" not in body, body[:120]


def test_fetch_fulltext_empty_input_is_empty_no_network():
    from apecx_integration.agents.literature.fulltext import fetch_fulltext_many

    assert asyncio.run(fetch_fulltext_many([])) == {}
    assert asyncio.run(fetch_fulltext_many(["", "  "])) == {}


@needs_europepmc
def test_attach_fulltext_populates_record_from_pmcid():
    """_attach_fulltext sets record["full_text"] for records carrying an OA PMCID, and
    leaves PMCID-less records untouched."""
    from apecx_integration.agents.literature.pipeline import _attach_fulltext

    records = [
        {"pmid": "1", "pmcid": _OA_PMCID, "title": "t", "abstract": "a"},
        {"pmid": "2", "pmcid": "", "title": "t2", "abstract": "a2"},
    ]
    _attach_fulltext(records)
    assert records[0].get("full_text"), "OA record should gain full_text"
    assert "full_text" not in records[1], "PMCID-less record must be untouched"


def test_attach_fulltext_degrades_loud_on_outage(monkeypatch, caplog):
    """A systemic Europe PMC outage must NOT crash or strand the harvest: the records keep
    their abstracts (no full_text) and a WARNING is logged."""
    from apecx_integration.agents.literature import pipeline
    from apecx_integration.agents.literature.fulltext import FulltextUnavailableError

    async def _boom(*_a, **_k):
        raise FulltextUnavailableError("simulated Europe PMC outage")

    monkeypatch.setattr("apecx_integration.agents.literature.fulltext.fetch_fulltext_many", _boom)
    records = [{"pmid": "1", "pmcid": "PMC1", "title": "t", "abstract": "a"}]
    with caplog.at_level("WARNING"):
        pipeline._attach_fulltext(records)
    assert "full_text" not in records[0]  # abstract preserved, nothing stranded
    assert records[0]["abstract"] == "a"
    assert any("full-text retrieval unavailable" in r.message for r in caplog.records)


def test_evidence_text_prefers_fulltext_then_abstract_no_network():
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        _evidence_text,
    )

    assert _evidence_text({"full_text": "X" * 100, "abstract": "abs"}, char_cap=10) == "X" * 10
    assert _evidence_text({"abstract": "abs only"}, char_cap=10) == "abs only"
    assert _evidence_text({}, char_cap=10) == ""


def test_render_host_evidence_uses_fulltext_when_present_no_network():
    from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
        LiteratureRagStep,
    )

    records = [
        {
            "pmid": "111",
            "title": "Full paper",
            "abstract": "abs1",
            "full_text": "BODY-EPITOPE-DETAIL",
        },
        {"pmid": "222", "title": "Abstract only", "abstract": "abs2"},
    ]
    md = LiteratureRagStep._render_host_evidence("q?", records)
    assert "BODY-EPITOPE-DETAIL" in md  # full-text body grounds the first record
    assert "open-access full text" in md  # the evidence-tier marker is shown
    assert "abs2" in md  # the abstract-only record still contributes its abstract
    assert "[PMID:111]" in md and "[PMID:222]" in md
