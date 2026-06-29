"""Unit tests for TaxonCandidateReviewStep — LLM same-virus filter + max-CDS selection (step 3).

The LLM returns the SET of candidates that are the same virus as the query; the step then picks
the highest-CDS member of that set (coverage-maximizing) and re-verifies its CDS >= min_cds.

LLM mocked by monkeypatching ``build_chat_llm`` / ``preflight_llm_model`` on the step's module;
the Content-Range CDS count mocked by monkeypatching the step's ``_cds_count`` (no unittest.mock).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import apecx_integration.composition.steps.taxon_candidate_review_step as mod
from apecx_integration.composition.steps.taxon_candidate_review_step import TaxonCandidateReviewStep


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, messages):  # noqa: D401 - mirrors langchain ChatModel.invoke
        return _Resp(self._content)


def _boom(*a, **k):
    pytest.fail("path must not be reached")


def _no_llm(*a, **k):
    raise RuntimeError("no LLM reachable")


@pytest.fixture(autouse=True)
def _reset_cache():
    mod._clear_cache()
    yield
    mod._clear_cache()


def _stage(tmp_path: Path) -> TaxonCandidateReviewStep:
    p = tmp_path / "review.yml"
    p.write_text("name: review_test\n")
    return TaxonCandidateReviewStep.from_config(str(p))


def _candidates():
    return [
        {"taxon_id": 37124, "taxon_name": "Chikungunya virus", "genomes": 200, "cds": 50},
        {"taxon_id": 9999, "taxon_name": "Other virus", "genomes": 5, "cds": 3},
    ]


def test_from_config_constructs(tmp_path):
    step = _stage(tmp_path)
    assert step.COMPONENT_TYPE == "taxon_candidate_review_step"
    assert step.name == "review_test"


def test_short_circuit_finalizes_taxon_id_from_iri(tmp_path, monkeypatch):
    """Dict resolver already won -> only set the int taxon_id; no LLM / no CDS call."""
    monkeypatch.setattr(mod, "build_chat_llm", _boom)
    monkeypatch.setattr(mod, "preflight_llm_model", _boom)
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_cds_count", _boom)
    bundle = {"query": "chikv", "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124"}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert out["taxon_id"] == 37124


def test_pick_winner_with_cds_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(mod, "build_chat_llm", lambda **k: _FakeLLM("the answer is 37124"))
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_cds_count", lambda tid: 50)
    bundle = {"query": "chikungunya virus", "taxon_candidates": _candidates()}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert out["taxon_id"] == 37124
    assert out["canonical_iri"].endswith("NCBITaxon_37124")
    assert out["resolved_species_name"] == "Chikungunya virus"
    assert out["resolution_status"] == "llm_fallback"
    res = out["taxon_resolution"]
    assert res["source"] == "llm-fallback"
    assert res["taxon_id"] == 37124
    assert res["genomes"] == 200
    assert res["cds"] == 50
    assert mod._REVIEW_CACHE["chikungunya virus"] == 37124


def test_selects_max_cds_among_matched(tmp_path, monkeypatch):
    """COVERAGE-MAXIMIZING: the LLM confirms BOTH a thin genus and a covered clade are the same
    virus; the step picks the higher-CDS clade deterministically — NOT the LLM's first-listed id."""
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)
    # LLM lists the genus FIRST, the clade SECOND — listing order must not decide the winner.
    monkeypatch.setattr(mod, "build_chat_llm", lambda **k: _FakeLLM("142786\n122929"))
    step = _stage(tmp_path)
    # The genus is an ANCESTOR of the clade (142786 ∈ 122929's lineage) — so the ambiguity guard
    # collapses them (NESTED, not sibling species) and the coverage-max pick still wins.
    cands = [
        {
            "taxon_id": 142786,
            "taxon_name": "Norovirus",
            "genomes": 9000,
            "cds": 0,
            "species_taxon_id": 142786,
            "lineage_ids": [10239, 142786],
        },
        {
            "taxon_id": 122929,
            "taxon_name": "Norovirus GII",
            "genomes": 4000,
            "cds": 112058,
            "species_taxon_id": 122929,
            "lineage_ids": [10239, 142786, 122929],
        },
    ]
    monkeypatch.setattr(step, "_cds_count", lambda tid: {142786: 0, 122929: 112058}[tid])
    bundle = {"query": "norovirus", "taxon_candidates": cands}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert out["taxon_id"] == 122929
    assert out["resolved_species_name"] == "Norovirus GII"
    assert out["taxon_resolution"]["cds"] == 112058
    assert out["taxon_resolution"]["genomes"] == 4000
    assert mod._REVIEW_CACHE["norovirus"] == 122929


def test_multiple_distinct_species_requests_clarification(tmp_path, monkeypatch):
    """Broadened ambiguity (2026-06-27): when the LLM confirms candidates spanning MULTIPLE distinct
    viral SPECIES that are SIBLINGS (neither in the other's lineage — HSV-1 vs HSV-2), the query is
    ambiguous → clarify listing the species, NOT a silent coverage-max pick. (Contrast the nested
    genus+clade case above, which collapses and picks normally.)"""
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(mod, "build_chat_llm", lambda **k: _FakeLLM("10298\n10310"))
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_cds_count", lambda tid: 80)
    cands = [
        {
            "taxon_id": 10298,
            "taxon_name": "Human alphaherpesvirus 1",
            "genomes": 60,
            "cds": 80,
            "species_taxon_id": 3050292,
            "lineage_ids": [10239, 10294, 3050292, 10298],
        },
        {
            "taxon_id": 10310,
            "taxon_name": "Human alphaherpesvirus 2",
            "genomes": 40,
            "cds": 80,
            "species_taxon_id": 3050293,
            "lineage_ids": [10239, 10294, 3050293, 10310],
        },
    ]
    out = asyncio.run(
        step.process(
            {"taxon_review_input": {"query": "herpes simplex virus", "taxon_candidates": cands}}
        )
    )
    assert out["taxon_resolution"]["taxon_id"] is None  # MISS → legs fast-degrade
    ct = out["control_transfer"]
    assert ct["reason"] == "ambiguous_entity"
    assert "ambiguous" in ct["message"].lower()
    labels = {c["label"] for c in ct["next_action"]["candidates"]}
    assert {"Human alphaherpesvirus 1", "Human alphaherpesvirus 2"} <= labels


def test_underspecified_taxon_requests_clarification(tmp_path, monkeypatch):
    """An ambiguous query whose only same-virus candidate is a non-specific UMBRELLA ("...unknown
    type") must NOT be silently analyzed — the step sets a control_transfer (ambiguous_entity) for
    the gate to surface as needs_input, and marks the resolution a MISS so the analysis legs
    fast-degrade rather than run on a poorly-defined taxon (the HSV-1/HSV-2 case, 2026-06-27)."""
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(mod, "build_chat_llm", lambda **k: _FakeLLM("126283"))
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_cds_count", lambda tid: 50)  # passes min_cds → reaches finalize
    cands = [
        {
            "taxon_id": 126283,
            "taxon_name": "Herpes simplex virus unknown type",
            "genomes": 338,
            "cds": 50,
        },
    ]
    bundle = {"query": "herpes simplex virus thymidine kinase", "taxon_candidates": cands}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert out["taxon_resolution"]["taxon_id"] is None  # MISS → legs fast-degrade
    assert "taxon_id" not in out or out.get("taxon_id") is None  # not promoted
    ct = out["control_transfer"]
    assert ct["reason"] == "ambiguous_entity"
    assert "under-specified" in ct["message"].lower()


def test_bare_syndrome_term_requests_clarification(tmp_path):
    """A bare DISEASE/SYNDROME term ("hepatitis virus") is NOT a single virus — clarify instead of the
    LLM fallback silently picking one member (the synonym step collapses it to HBV). Fires before any
    LLM call. 2026-06-28 (the SAFE syndrome path; the family-spread discriminator was UNSAFE)."""
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process(
            {"taxon_review_input": {"query": "epitopes on the hepatitis virus surface antigen"}}
        )
    )
    assert out["taxon_resolution"]["taxon_id"] is None
    ct = out["control_transfer"]
    assert ct["reason"] == "ambiguous_entity"
    assert "disease" in ct["message"].lower() or "syndrome" in ct["message"].lower()


def test_qualified_disease_name_not_flagged_as_syndrome():
    """A QUALIFIED name must NOT trip the syndrome check (those dict-resolve + short-circuit in
    production); only a bare category does."""
    assert mod._syndrome_category("on the Japanese encephalitis virus E protein") is None
    assert (
        mod._syndrome_category("on the Crimean-Congo hemorrhagic fever virus nucleoprotein") is None
    )
    assert mod._syndrome_category("on the Hepatitis B virus surface antigen") is None
    assert mod._syndrome_category("on the hepatitis virus antigen") == "hepatitis"
    assert mod._syndrome_category("on the encephalitis virus envelope") == "encephalitis"
    assert (
        mod._syndrome_category("on the hemorrhagic fever virus glycoprotein") == "hemorrhagic fever"
    )


def test_reject_all_is_a_named_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(mod, "build_chat_llm", lambda **k: _FakeLLM("NONE"))
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_cds_count", _boom)  # never verify a NONE
    bundle = {"query": "chikungunya virus", "taxon_candidates": _candidates()}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert "taxon_id" not in out
    assert out["taxon_resolution"]["taxon_id"] is None
    assert "no candidate matched" in out["taxon_resolution"]["note"]
    assert mod._REVIEW_CACHE["chikungunya virus"] is None


def test_below_min_cds_is_a_named_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(mod, "build_chat_llm", lambda **k: _FakeLLM("37124"))
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_cds_count", lambda tid: 1)  # < default min_cds (2)
    bundle = {"query": "chikungunya virus", "taxon_candidates": _candidates()}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert "taxon_id" not in out
    assert out["taxon_resolution"]["taxon_id"] is None
    assert "min_cds" in out["taxon_resolution"]["note"]
    assert mod._REVIEW_CACHE["chikungunya virus"] is None


def test_no_candidates_is_a_named_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "build_chat_llm", _boom)  # candidates checked before any LLM
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"taxon_review_input": {"query": "q", "taxon_candidates": []}}))
    assert "taxon_id" not in out
    assert "no BV-BRC taxonomy candidates" in out["taxon_resolution"]["note"]


def test_no_llm_is_a_named_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "preflight_llm_model", _no_llm)
    monkeypatch.setattr(mod, "build_chat_llm", _boom)
    step = _stage(tmp_path)
    bundle = {"query": "chikungunya virus", "taxon_candidates": _candidates()}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert out["taxon_resolution"]["note"] == "LLM unavailable for candidate review"


def test_cache_hit_reuses_verdict_without_llm(tmp_path, monkeypatch):
    mod._REVIEW_CACHE["chikungunya virus"] = 37124
    monkeypatch.setattr(mod, "build_chat_llm", _boom)
    monkeypatch.setattr(mod, "preflight_llm_model", _boom)
    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_cds_count", _boom)
    bundle = {"query": "Chikungunya Virus", "taxon_candidates": _candidates()}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert out["taxon_id"] == 37124
    assert out["canonical_iri"].endswith("NCBITaxon_37124")
    assert out["resolved_species_name"] == "Chikungunya virus"


def test_cache_hit_remembers_a_miss(tmp_path, monkeypatch):
    mod._REVIEW_CACHE["chikungunya virus"] = None
    monkeypatch.setattr(mod, "build_chat_llm", _boom)
    monkeypatch.setattr(mod, "preflight_llm_model", _boom)
    step = _stage(tmp_path)
    bundle = {"query": "chikungunya virus", "taxon_candidates": _candidates()}
    out = asyncio.run(step.process({"taxon_review_input": bundle}))
    assert "taxon_id" not in out
    assert "(cached)" in out["taxon_resolution"]["note"]


def test_non_dict_input_raises(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(_stage(tmp_path).process("not a dict"))
