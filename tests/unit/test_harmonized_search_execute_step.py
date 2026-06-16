"""Unit tests for HarmonizedSearchExecuteStep.

The step has three branches:
- ambiguous resolution → paused envelope, NO Globus queries run
- miss → miss envelope
- resolved → both Globus queries run via globus_sdk

The unit tests cover the first two branches deterministically. The
third branch is exercised by the live-Globus integration test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.harmonized_search_execute_step import (
    HarmonizedSearchExecuteStep,
)


def _stage(tmp_path: Path) -> HarmonizedSearchExecuteStep:
    p = tmp_path / "harmonized_search_execute.yml"
    p.write_text("name: harmonized_search_execute_test\n")
    return HarmonizedSearchExecuteStep.from_config(str(p))


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "harmonized_search_execute_test"


def test_ambiguous_emits_paused_envelope_without_globus(tmp_path):
    """Ambiguous resolution path: NO Globus queries, only the candidate
    list goes in the envelope."""
    step = _stage(tmp_path)
    plan = {
        "term": "RSV",
        "index": "bvbrc_genome",
        "resolution_path": "ambiguous",
        "canonical_iri": None,
        "canonical_label": None,
        "canonical_ontology": None,
        "confidence": 0.0,
        "resolution_status": "ambiguous",
        "synonyms": [],
        "candidates": [
            {
                "canonical_iri": "http://x/A",
                "canonical_label": "Cand A",
                "canonical_ontology": "ncbitaxon",
                "confidence": 1.0,
            },
            {
                "canonical_iri": "http://x/B",
                "canonical_label": "Cand B",
                "canonical_ontology": "ncbitaxon",
                "confidence": 1.0,
            },
        ],
        "needs_disambiguation": True,
        "evidence": "ambiguous resolution",
    }
    out = asyncio.run(step.process(plan))
    env_in = out["envelope_input"]
    assert "data" in env_in
    bundle = env_in["data"]
    assert bundle["kind"] == "bundle"
    parts = bundle["parts"]
    assert parts["status"] == "paused_awaiting_disambiguation"
    assert parts["resolution"]["candidate_count"] == 2
    assert "next_action" in parts
    assert parts["next_action"]["kind"] == "re-invoke_with_chosen_iri"
    # Critical: no raw_query / harmonized_query keys — Globus was NOT run.
    assert "raw_query" not in parts
    assert "harmonized_query" not in parts
    assert "divergence" not in parts
    # Markdown surfaces all candidates for human display.
    md = env_in["markdown"]
    assert "RSV" in md
    assert "http://x/A" in md
    assert "http://x/B" in md


def test_miss_emits_miss_envelope(tmp_path):
    step = _stage(tmp_path)
    plan = {
        "term": "totally-made-up-term",
        "index": "bvbrc_genome",
        "resolution_path": "miss",
        "canonical_iri": None,
        "canonical_label": None,
        "canonical_ontology": None,
        "confidence": 0.0,
        "resolution_status": "unresolved",
        "synonyms": [],
        "candidates": [],
        "needs_disambiguation": False,
        "evidence": "no match",
    }
    out = asyncio.run(step.process(plan))
    env_in = out["envelope_input"]
    parts = env_in["data"]["parts"]
    # No network in the unit env → the raw fallback can't run → genuine miss (path 'miss'),
    # but it is HONEST that a raw query was attempted (raw_total present).
    assert parts["resolution"]["path"] == "miss"
    assert parts["status"] == "ok"
    assert parts["raw_total"] == 0


def test_miss_falls_back_to_raw_query_and_pulls_present_records(monkeypatch):
    """THE FIX: a term the dictionary cannot resolve must STILL pull the records that are present
    in the index via a raw full-text query — not return nothing. 'Empty output for data present
    in the index but not pulled is a failure' (the bvbrc/violin 0-except-PDB report)."""
    import apecx_integration.composition.steps.harmonized_search_execute_step as mod

    monkeypatch.setattr(
        mod,
        "_raw_query",
        lambda index, term, limit=200: (
            6687,
            [{"Genome_Name": "Chikungunya virus strain S27", "Species": "Chikungunya virus"}],
            None,
        ),
    )
    out = mod._run_miss_envelope(
        {"term": "Chikungunya virus", "index": "bvbrc_genome", "evidence": "no dict entry"}
    )
    env = out["envelope_input"]
    md = env["markdown"]
    parts = env["data"]["parts"]
    assert "6687 raw full-text match" in md  # the present data is PULLED, not dropped
    assert "did NOT resolve to a taxon" in md  # honest it is unharmonized
    assert "Chikungunya virus strain S27" in md  # the record is actually rendered
    assert parts["resolution"]["path"] == "miss_raw_fallback"
    assert parts["raw_total"] == 6687
    assert parts["raw_sample"][0]["Genome_Name"] == "Chikungunya virus strain S27"


@pytest.mark.parametrize("missing_key", ["term", "index", "resolution_path"])
def test_missing_required_keys_raises(tmp_path, missing_key):
    step = _stage(tmp_path)
    plan = {"term": "X", "index": "bvbrc_genome", "resolution_path": "miss"}
    plan.pop(missing_key)
    with pytest.raises(ValueError, match=missing_key):
        asyncio.run(step.process(plan))


def test_unknown_index_loud(tmp_path):
    step = _stage(tmp_path)
    plan = {
        "term": "X",
        "index": "not_a_real_index",
        "resolution_path": "miss",
        "candidates": [],
        "synonyms": [],
    }
    with pytest.raises(ValueError, match="unknown index"):
        asyncio.run(step.process(plan))


def test_unwraps_trigger_envelope(tmp_path):
    """Framework wraps input as {du_name: payload}; the step must unwrap."""
    step = _stage(tmp_path)
    plan = {
        "term": "X",
        "index": "bvbrc_genome",
        "resolution_path": "miss",
        "candidates": [],
        "synonyms": [],
    }
    out = asyncio.run(step.process({"plan": plan}))
    assert "envelope_input" in out


def test_paused_bundle_validates_as_data_shape(tmp_path):
    """The Bundle the paused envelope emits MUST parse via parse_data_shape
    (EnvelopeStep will raise if it doesn't)."""
    from apecx_integration.composition.schemas.data_shapes import (
        parse_data_shape,
    )

    step = _stage(tmp_path)
    plan = {
        "term": "RSV",
        "index": "bvbrc_genome",
        "resolution_path": "ambiguous",
        "candidates": [
            {
                "canonical_iri": "http://x/A",
                "canonical_label": "A",
                "canonical_ontology": "n",
                "confidence": 1.0,
            },
        ],
        "synonyms": [],
    }
    out = asyncio.run(step.process(plan))
    shape = parse_data_shape(out["envelope_input"]["data"])
    assert shape.kind == "bundle"


def test_quote_raw_term_helper():
    """Multi-token / non-alphanumeric raw terms get quoted for phrase match."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _quote_raw_term,
    )

    assert _quote_raw_term("CHIKV") == ("CHIKV", False)
    q, was = _quote_raw_term("Sindbis virus")
    assert q == '"Sindbis virus"' and was is True
    q, was = _quote_raw_term("HSV-2")
    assert q == '"HSV-2"' and was is True


def test_iri_to_taxon_id_helper():
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _iri_to_taxon_id,
    )

    assert _iri_to_taxon_id("http://purl.obolibrary.org/obo/NCBITaxon_37124") == 37124
    assert _iri_to_taxon_id("not-a-real-iri") is None


# ─────────────────────────────────────────────────────────────────────────
# Harmonization-health verdict matrix
#
# Real findings from the 2026-06-09 cross-index probe drove this classifier:
# - Yellow fever virus → harm=0, raw=1828 (BV-BRC indexed under the new ICTV
#   binomial 'Orthoflavivirus flavi'; dict still has 'Yellow fever virus').
# - CHIKV → harm=6684, raw=1162 (synonym expansion to ~6.6k strain names).
# - Sindbis virus → harm=612, raw=612 (canonical label IS the index value).
# The verdict matrix is what the LLM consumes to know which number to trust.
# ─────────────────────────────────────────────────────────────────────────


def test_harm_health_broken_yellow_fever_shape():
    """The exact failure shape that drove this classifier into existence."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, reason = _compute_harmonization_health(
        raw_total=1828,
        harm_total=0,
        filter_field="Species",
        filter_values_count=4,
        index="bvbrc_genome",
        canonical_label="Yellow fever virus",
        raw_error=None,
        harm_error=None,
    )
    assert verdict == "broken"
    assert "Species" in reason
    assert "1828" in reason
    assert "stale ICTV taxonomy rename" in reason


def test_harm_health_helped_chikv_shape():
    """The canonical win case: synonym expansion reaches 6,684 records."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, reason = _compute_harmonization_health(
        raw_total=1162,
        harm_total=6684,
        filter_field="Species",
        filter_values_count=6653,
        index="bvbrc_genome",
        canonical_label="Chikungunya virus",
        raw_error=None,
        harm_error=None,
    )
    assert verdict == "harmonization_helped"
    assert "5522" in reason  # 6684-1162


def test_harm_health_healthy_parity():
    """Within noise floor: |Δ| < 5 AND fraction < 5%."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, _ = _compute_harmonization_health(
        raw_total=612,
        harm_total=612,
        filter_field="Species",
        filter_values_count=10,
        index="bvbrc_genome",
        canonical_label="Sindbis virus",
        raw_error=None,
        harm_error=None,
    )
    assert verdict == "healthy_parity"


def test_harm_health_degraded_real_case():
    """The CHIKUNGUNYA VIRUS case: raw=6687, harm=6684 — within 5 records
    but the absolute_diff>=5 OR fraction>=5% threshold has 3<5 AND 0.04%<5%,
    so it lands in healthy_parity, NOT degraded. This pins the boundary."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, _ = _compute_harmonization_health(
        raw_total=6687,
        harm_total=6684,
        filter_field="Species",
        filter_values_count=6653,
        index="bvbrc_genome",
        canonical_label="Chikungunya virus",
        raw_error=None,
        harm_error=None,
    )
    # 3 fewer is below the 5-record threshold AND 0.04% < 5% → healthy_parity
    assert verdict == "healthy_parity"


def test_harm_health_degraded_above_threshold():
    """harm < raw with |Δ| >= 5 → degraded."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, reason = _compute_harmonization_health(
        raw_total=100,
        harm_total=80,
        filter_field="Species",
        filter_values_count=3,
        index="bvbrc_genome",
        canonical_label="Some virus",
        raw_error=None,
        harm_error=None,
    )
    assert verdict == "degraded"
    assert "20 additional" in reason


def test_harm_health_errored_short_circuits():
    """A Globus query error short-circuits the verdict to 'errored'."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, reason = _compute_harmonization_health(
        raw_total=0,
        harm_total=0,
        filter_field="Species",
        filter_values_count=4,
        index="bvbrc_genome",
        canonical_label="X",
        raw_error="GlobusAPIError: 503",
        harm_error=None,
    )
    assert verdict == "errored"
    assert "503" in reason


def test_harm_health_zero_both_with_filter_is_zero_floor_unclear():
    """raw=0 AND harm=0 with filter_values_count>=1 must classify as
    'zero_floor_unclear', NOT 'healthy_parity'.

    The prior verdict 'healthy_parity' was misleadingly confident — it
    couldn't distinguish a genuine miss from a broken filter at floor.
    Discovered during the 2026-06-09 Option-A walkthrough: round-2 RSV
    re-call with the chosen human-RSV IRI returned 0/0 (BV-BRC indexes
    human RSV under 'Orthopneumovirus hominis', dict has 'human
    respiratory syncytial virus') and the LLM got a false-confident
    'parity' signal."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, reason = _compute_harmonization_health(
        raw_total=0,
        harm_total=0,
        filter_field="Species",
        filter_values_count=1,
        index="protabank",
        canonical_label="Rare entity",
        raw_error=None,
        harm_error=None,
    )
    assert verdict == "zero_floor_unclear"
    assert "0 records" in reason
    assert "do not assert" in reason.lower()


def test_harm_health_zero_floor_unclear_round2_iri_shape():
    """The exact failure shape that drove this verdict into existence:
    user disambiguated to human RSV (NCBITaxon_11250), workflow re-called
    with the IRI, both raw + harm returned 0 on bvbrc_genome."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, reason = _compute_harmonization_health(
        raw_total=0,
        harm_total=0,
        filter_field="Species",
        filter_values_count=6,
        index="bvbrc_genome",
        canonical_label="human respiratory syncytial virus",
        raw_error=None,
        harm_error=None,
    )
    assert verdict == "zero_floor_unclear"
    assert "broader query" in reason.lower()
    assert "human respiratory syncytial virus" in reason


def test_harm_health_zero_zero_no_filter_still_handled_safely():
    """raw=0, harm=0, filter_values_count=0 — this path is normally
    short-circuited by harm_error='no filter values built' BEFORE
    _compute_harmonization_health is called, but the classifier should
    still cope deterministically if it ever sees this shape directly.
    Falls through to healthy_parity (no filter was attempted)."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _compute_harmonization_health,
    )

    verdict, _ = _compute_harmonization_health(
        raw_total=0,
        harm_total=0,
        filter_field="Species",
        filter_values_count=0,
        index="protabank",
        canonical_label="Unobtanium",
        raw_error=None,
        harm_error=None,
    )
    assert verdict == "healthy_parity"


# ─────────────────────────────────────────────────────────────────────────
# IRI-input raw query substitution (fix for the round-2 disambiguation
# path where term=<IRI> made the raw leg structurally meaningless).
# ─────────────────────────────────────────────────────────────────────────


def test_is_iri_input_helper():
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _is_iri_input,
    )

    assert _is_iri_input("http://purl.obolibrary.org/obo/NCBITaxon_11250") is True
    assert _is_iri_input("https://example.org/x") is True
    assert _is_iri_input("CHIKV") is False
    assert _is_iri_input("Chikungunya virus") is False
    assert _is_iri_input("") is False


def test_select_raw_query_term_passthrough_for_non_iri():
    """Plain surface forms pass through unchanged with no substitution."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _select_raw_query_term,
    )

    q, reason = _select_raw_query_term("CHIKV", "Chikungunya virus")
    assert q == "CHIKV"
    assert reason is None

    q, reason = _select_raw_query_term("yellow fever virus", "Yellow fever virus")
    assert q == "yellow fever virus"
    assert reason is None


def test_select_raw_query_term_iri_substitutes_label():
    """When term is an IRI AND canonical_label is available, the raw
    query uses the label instead — searching Globus text for the literal
    IRI string matches nothing in any APECx-published index."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _select_raw_query_term,
    )

    q, reason = _select_raw_query_term(
        "http://purl.obolibrary.org/obo/NCBITaxon_11250",
        "human respiratory syncytial virus",
    )
    assert q == "human respiratory syncytial virus"
    assert reason is not None
    assert "IRI" in reason
    assert "human respiratory syncytial virus" in reason


def test_select_raw_query_term_iri_without_label_falls_through():
    """If the resolver gave us an IRI but no canonical_label, fall
    through to the IRI itself and record the limitation honestly."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _select_raw_query_term,
    )

    iri = "http://purl.obolibrary.org/obo/NCBITaxon_99999999"
    q, reason = _select_raw_query_term(iri, None)
    assert q == iri
    assert reason is not None
    assert "no canonical_label" in reason
    assert "not-applicable" in reason
