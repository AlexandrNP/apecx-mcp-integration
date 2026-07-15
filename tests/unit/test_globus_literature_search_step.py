"""Unit tests for GlobusLiteratureSearchStep — the degrade-loud Globus literature (synonym) leg.

The step searches the aggregate APECx Globus Search index for JOURNAL papers about the
resolved virus by TEXTUAL synonym match (literature records carry no taxon IRI, so an
IRI filter finds zero) and folds them into the bundle's ``publications``. Its absence
DEGRADES LOUD (named note + empty list + stage report), never a raise.

These tests monkeypatch ``globus_client.search`` with realistic hit dicts matching the
real interface (subject + DataCite ``content``) — an allowed unit mock of an external
dependency. The REAL Globus path is exercised end-to-end by the viral_epitope_analysis
workflow integration run (the workflow builder wires this step into the DAG).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.agents.globus_search import client as globus_client
from apecx_integration.composition.steps.globus_literature_search_step import (
    GlobusLiteratureSearchStep,
)


def _stage(tmp_path: Path, **cfg) -> GlobusLiteratureSearchStep:
    p = tmp_path / "globus_lit.yml"
    body = "name: globus_lit_test\n" + "".join(f"{k}: {v}\n" for k, v in cfg.items())
    p.write_text(body)
    return GlobusLiteratureSearchStep.from_config(str(p))


def _paper_hit(
    subject: str, title: str, *, publisher: str, doi: str = "", pmid: str = "", year: str = "2021"
) -> dict:
    """A realistic literature hit dict shaped like a real Globus DataCite record."""
    content: dict = {
        "titles": [{"title": title}],
        "publisher": {"name": publisher},
        "publicationYear": year,
        "subjects": [{"subject": "chikungunya"}],  # NB: no valueUri — papers have no taxon IRI
    }
    if doi:
        content["relatedIdentifiers"] = [{"relatedIdentifier": doi, "relatedIdentifierType": "DOI"}]
    if pmid:
        content["alternateIdentifiers"] = [
            {"alternateIdentifier": pmid, "alternateIdentifierType": "PMID"}
        ]
    return {"subject": subject, "content": content, "score": None}


def _structural_hit(subject: str, publisher: str) -> dict:
    return {
        "subject": subject,
        "content": {"titles": [{"title": "some structure"}], "publisher": {"name": publisher}},
        "score": None,
    }


# --------------------------------------------------------------- config / from_config


def test_loads_via_from_config_and_max_records(tmp_path):
    step = _stage(tmp_path, max_records=7)
    assert step.name == "globus_lit_test"
    assert step._max_records == 7


def test_default_max_records(tmp_path):
    assert _stage(tmp_path)._max_records == 25


def test_max_records_ge_1_rejects_zero(tmp_path):
    with pytest.raises(ValueError):  # config load surfaces the pydantic ge=1 violation
        _stage(tmp_path, max_records=0)


# --------------------------------------------------------------- synonym OR-query building


def test_builds_synonym_or_query_not_iri(tmp_path, monkeypatch):
    captured = {}

    def _fake_search(q, *, max_results=20, filters=None, advanced=False):
        captured["q"] = q
        captured["advanced"] = advanced
        captured["max_results"] = max_results
        return []

    monkeypatch.setattr(globus_client, "search", _fake_search)
    monkeypatch.delenv("APECX_GLOBUS_SEARCH_DISABLED", raising=False)

    asyncio.run(
        _stage(tmp_path, max_records=9).process(
            {
                "query": "chikungunya E1 epitopes",
                "synonyms": ["Chikungunya virus", "CHIKV"],
                "canonical_label": "Chikungunya virus",
            }
        )
    )
    q = captured["q"]
    assert '"Chikungunya virus"' in q
    assert '"CHIKV"' in q
    assert " OR " in q
    # Search by TEXT synonyms, NOT by taxon IRI.
    assert "valueUri" not in q
    assert "purl.obolibrary.org" not in q
    assert "NCBITaxon" not in q
    assert captured["max_results"] == 9
    # Runs in advanced (Lucene) mode so the phrases + boolean are honored (simple mode
    # matches the whole corpus off-topic).
    assert captured["advanced"] is True
    # AND-ed with a topical clause so papers are epitope-relevant, not generic virus mentions.
    assert " AND (" in q
    assert '"epitope"' in q


def test_synonyms_from_resolution_plan_when_top_level_absent(tmp_path, monkeypatch):
    """The common real case: bundle carries NO top-level 'synonyms'; the resolve step's
    bundle['resolution_plan']['synonyms'] is the reliable source and MUST be picked up."""
    captured = {}

    def _fake_search(q, *, max_results=20, filters=None, advanced=False):
        captured["q"] = q
        captured["advanced"] = advanced
        return []

    monkeypatch.setattr(globus_client, "search", _fake_search)
    monkeypatch.delenv("APECX_GLOBUS_SEARCH_DISABLED", raising=False)

    asyncio.run(
        _stage(tmp_path).process(
            {
                "query": "chikv",
                # no top-level "synonyms" key at all
                "resolution_plan": {
                    "synonyms": ["Chikungunya virus", "o'nyong-nyong-adjacent alias"],
                    "canonical_label": "Chikungunya virus",
                },
            }
        )
    )
    assert '"Chikungunya virus"' in captured["q"]
    assert '"o\'nyong-nyong-adjacent alias"' in captured["q"]


def test_query_falls_back_to_tokens_when_no_synonyms(tmp_path, monkeypatch):
    captured = {}

    def _fake_search(q, *, max_results=20, filters=None, advanced=False):
        captured["q"] = q
        captured["advanced"] = advanced
        return []

    monkeypatch.setattr(globus_client, "search", _fake_search)
    monkeypatch.delenv("APECX_GLOBUS_SEARCH_DISABLED", raising=False)

    asyncio.run(_stage(tmp_path).process({"query": "zika dengue"}))
    assert '"zika"' in captured["q"]
    assert '"dengue"' in captured["q"]


def test_synonym_cap_bounds_query_and_drops_strain_names(tmp_path, monkeypatch):
    """Regression: a virus can carry thousands of BV-BRC strain-isolate synonyms (CHIKV: 6653).
    OR-ing them all built a 271 KB query and Globus 400s past ~10 multi-word phrases. The builder
    must keep only a SMALL set of short general names/acronyms and DROP '<canonical> <suffix>'
    strain names, so the query stays bounded."""
    from apecx_integration.composition.steps.globus_literature_search_step import (
        _MAX_SYNONYMS,
        GlobusLiteratureSearchStep,
    )

    synonyms = ["Chikungunya virus", "CHIKV", "chikungunya"] + [
        f"Chikungunya virus CKV_PHL_2013_CK13-{i:04d}" for i in range(5000)
    ]
    q = GlobusLiteratureSearchStep._build_synonym_query(
        synonyms, "Chikungunya virus", "chikungunya E1", "E1"
    )
    virus_clause = q.split(" AND ")[0]
    n_virus_terms = virus_clause.count(" OR ") + 1
    assert n_virus_terms <= _MAX_SYNONYMS  # bounded phrase count (Globus 400s past ~10)
    assert len(q) < 2000  # nowhere near the 271 KB blow-up
    # strain-isolate names ("<canonical> <suffix>") are dropped; the general names survive
    assert "CK13-" not in q
    assert '"CHIKV"' in q and '"Chikungunya virus"' in q


# --------------------------------------------------------------- literature filter


def test_filter_drops_structural_keeps_journals(tmp_path, monkeypatch):
    hits = [
        _paper_hit("pubmed:12345", "CHIKV E1 antibody epitope", publisher="Journal of virology"),
        _structural_hit("pdb:1I9G", "RCSB PDB"),
        _structural_hit("emdb:EMD-34119", "Electron Microscopy Data Bank"),
        # A journal paper whose SUBJECT isn't pdb:/emdb: but publisher is structural → drop.
        _paper_hit("doi:10.x/y", "structure paper", publisher="RCSB PDB"),
        _paper_hit("pubmed:67890", "Second CHIKV paper", publisher="PLoS one"),
    ]

    monkeypatch.setattr(globus_client, "search", lambda q, **k: list(hits))
    monkeypatch.delenv("APECX_GLOBUS_SEARCH_DISABLED", raising=False)

    out = asyncio.run(
        _stage(tmp_path).process({"query": "chikv", "synonyms": ["Chikungunya virus"]})
    )
    lit = out["globus_literature"]
    assert out["globus_literature_count"] == 2
    journals = {p["journal"] for p in lit}
    assert journals == {"Journal of virology", "PLoS one"}
    for p in lit:
        assert p["provenance"] == "globus_literature"
        assert p["abstract"] == ""


# --------------------------------------------------------------- fold + dedup


def test_folds_into_publications_and_dedups_by_doi(tmp_path, monkeypatch):
    existing = [
        {"title": "Existing paper", "doi": "10.1/AbC", "pmid": "", "journal": "PubMed"},
    ]
    hits = [
        # same DOI (different case) as an existing pub → must NOT be re-added
        _paper_hit(
            "pubmed:1",
            "Existing paper (globus copy)",
            publisher="Journal of virology",
            doi="10.1/abc",
        ),
        # a genuinely new paper → added
        _paper_hit("pubmed:2", "Brand new CHIKV paper", publisher="PLoS one", doi="10.2/new"),
    ]

    monkeypatch.setattr(globus_client, "search", lambda q, **k: list(hits))
    monkeypatch.delenv("APECX_GLOBUS_SEARCH_DISABLED", raising=False)

    out = asyncio.run(
        _stage(tmp_path).process(
            {"query": "chikv", "synonyms": ["Chikungunya virus"], "publications": existing}
        )
    )
    pubs = out["publications"]
    dois = [str(p.get("doi", "")).lower() for p in pubs]
    assert dois.count("10.1/abc") == 1  # deduped, not doubled
    assert "10.2/new" in dois  # new one folded in
    assert len(pubs) == 2  # 1 existing + 1 new
    # globus_literature still records BOTH literature hits (the leg's own output)
    assert out["globus_literature_count"] == 2
    assert out["globus_literature_note"] is None


# --------------------------------------------------------------- degrade-loud paths


def _assert_degraded(out: dict, needle: str) -> None:
    assert out["globus_literature"] == []
    assert out["globus_literature_count"] == 0
    note = out["globus_literature_note"]
    assert note and needle in note, note
    # stage report was appended
    reports = out.get("stage_reports") or []
    assert any(r.get("stage") == "globus_literature" for r in reports), reports


def test_degrade_loud_when_search_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("APECX_GLOBUS_SEARCH_DISABLED", "1")

    def _should_not_run(*a, **k):  # search must NOT be called on the disabled path
        raise AssertionError("globus_client.search should not run when disabled")

    monkeypatch.setattr(globus_client, "search", _should_not_run)

    out = asyncio.run(
        _stage(tmp_path).process(
            {"query": "chikv", "synonyms": ["Chikungunya virus"], "other": "kept"}
        )
    )
    _assert_degraded(out, "DISABLED")
    assert out["other"] == "kept"  # bundle passes through


def test_degrade_loud_when_search_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("APECX_GLOBUS_SEARCH_DISABLED", raising=False)

    def _boom(q, **k):
        raise globus_client.GlobusSearchUnavailableError("Globus outage")

    monkeypatch.setattr(globus_client, "search", _boom)

    out = asyncio.run(
        _stage(tmp_path).process({"query": "chikv", "synonyms": ["Chikungunya virus"]})
    )
    _assert_degraded(out, "FAILED")
    assert "GlobusSearchUnavailableError" in out["globus_literature_note"]


def test_degrade_loud_when_no_usable_terms(tmp_path, monkeypatch):
    monkeypatch.delenv("APECX_GLOBUS_SEARCH_DISABLED", raising=False)

    def _should_not_run(*a, **k):
        raise AssertionError("search should not run with no usable terms")

    monkeypatch.setattr(globus_client, "search", _should_not_run)

    out = asyncio.run(_stage(tmp_path).process({"query": ""}))
    _assert_degraded(out, "SKIPPED")


# --------------------------------------------------------------- input shape


def test_bad_input_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process(["not", "a", "dict"]))


def test_envelope_unwrap(tmp_path, monkeypatch):
    captured = {}

    def _fake_search(q, *, max_results=20, filters=None, advanced=False):
        captured["q"] = q
        captured["advanced"] = advanced
        return []

    monkeypatch.setattr(globus_client, "search", _fake_search)
    monkeypatch.delenv("APECX_GLOBUS_SEARCH_DISABLED", raising=False)

    out = asyncio.run(
        _stage(tmp_path).process(
            {"lit_input": {"query": "chikv", "synonyms": ["Chikungunya virus"]}}
        )
    )
    assert '"Chikungunya virus"' in captured["q"]  # processed the unwrapped bundle
    assert "globus_literature" in out
