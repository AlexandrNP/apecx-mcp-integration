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

    # The destination (harmonized) index returns DataCite-shaped records:
    # the title lives at titles[0].title, not a flat Genome_Name column.
    monkeypatch.setattr(
        mod,
        "_raw_query",
        lambda index, term, limit=200: (
            6687,
            [
                {
                    "titles": [{"title": "Chikungunya virus strain S27"}],
                    "subjects": [{"subject": "Chikungunya virus"}],
                }
            ],
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
    assert parts["raw_sample"][0]["title"] == "Chikungunya virus strain S27"


def test_summarize_record_preserves_object_identifiers():
    """P0 fix: _summarize_record must carry a citation token (`subject`) + typed object
    IDs (`identifiers`) from the DataCite shape. The old code read a top-level `identifier`
    key DataCite records lack, so every harmonized record was projected with NO id and then
    silently dropped by the renderer (zero Globus records in the evidence ledger)."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _summarize_record,
    )

    rec = {
        "titles": [{"title": "Chikungunya virus strain X"}],
        "alternateIdentifiers": [
            {"alternateIdentifier": "37124", "alternateIdentifierType": "NCBI-Taxonomy"},
            {"alternateIdentifier": "KY703959", "alternateIdentifierType": "GenBank"},
            {"alternateIdentifier": "37124.51", "alternateIdentifierType": "BVBRC-Genome"},
        ],
        "subjects": [
            {
                "subject": "Chikungunya virus",
                "valueUri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
            },
        ],
    }
    out = _summarize_record(rec)
    assert out["title"] == "Chikungunya virus strain X"
    assert out["subject"] == "GenBank:KY703959"  # primary citation token (not the taxon)
    assert out["identifiers"]["GenBank"] == ["KY703959"]
    assert out["identifiers"]["BVBRC-Genome"] == ["37124.51"]
    assert out["taxon_iris"] == ["http://purl.obolibrary.org/obo/NCBITaxon_37124"]


class _FakeResp:
    def __init__(self, total, n_returned):
        self.data = {
            "total": total,
            "gmeta": [
                {"entries": [{"content": {"titles": [{"title": f"rec {i}"}]}}]}
                for i in range(n_returned)
            ],
        }


class _FakeClient:
    """Records the limit requested and returns ``min(total, limit)`` records — mirrors
    Globus Search's single-call behavior (a large limit returns the whole set)."""

    def __init__(self, total):
        self._total = total
        self.last_limit = None

    def post_search(self, uuid, body):
        self.last_limit = body["limit"]
        return _FakeResp(self._total, min(self._total, body["limit"]))


def test_fetch_records_pulls_full_set_in_one_call():
    """The retrieval cap fix: a single large-limit request pulls the WHOLE matched set
    (6,684 records), not a 200-record page — and is not flagged capped."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _MAX_RECORDS,
        _fetch_records,
    )

    client = _FakeClient(total=6684)
    total, records, capped = _fetch_records(client, "uuid", {"q": "chikungunya"})
    assert total == 6684
    assert len(records) == 6684  # the FULL set, not 200
    assert capped is False
    assert client.last_limit == _MAX_RECORDS  # asked for everything up to the ceiling


def test_fetch_records_flags_capped_above_ceiling():
    """A result set larger than the Globus offset ceiling is carried up to the cap and
    flagged capped=True (honest: this is the first N, not the whole set)."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _MAX_RECORDS,
        _fetch_records,
    )

    client = _FakeClient(total=_MAX_RECORDS + 5000)
    total, records, capped = _fetch_records(client, "uuid", {"q": "influenza"})
    assert total == _MAX_RECORDS + 5000
    assert len(records) == _MAX_RECORDS
    assert capped is True


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


# ---------------------------------------------------------------------------
# Destination-index contract pin (taxon-based search regression guard)
# ---------------------------------------------------------------------------

# The harmonized DESTINATION index UUIDs (the SC-D harmonized output). The
# step MUST query these — never the raw scrape-input *source* UUIDs. Pointing
# the step at the sources silently broke taxon-based search: the sources have
# no canonical taxon IRI, so the filter fell back to raw name-string matching
# (e.g. violin_vaccine returned 0 for a pathogen the index actually carries).
_DESTINATION_UUIDS = {
    "violin_pathogen": "b4965a61-e6de-4e8b-b312-7ab37c7c39d3",
    "violin_vaccine": "12dfce07-0b4a-40b9-8890-48c3e943f9a1",
    "violin_gene": "667dc223-55ba-423a-b116-3bb434813238",
    "bvbrc_genome": "dfefcd85-d130-4dd1-b37a-4bc05f3bcdc8",
    "bvbrc_protein": "826e5d28-c906-4f74-816c-9b37b6ef0a7b",
    "bvbrc_protein_structure": "96fbabbb-06b2-4ea3-91f9-8510bfabb52a",
    "bvbrc_epitope": "4c0b4e3d-1d9d-40be-8cbc-d0f2601e44bf",
    "antiviraldb": "23a7bffd-10b7-4d40-9cec-1a435f32b04e",
    "protabank": "be999b57-88c4-4aff-a883-4b96c57b66cc",
}

# The raw scrape-input SOURCE UUIDs — the step must NOT use any of these.
_SOURCE_UUIDS_FORBIDDEN = {
    "a67c7310-5115-446f-bfb6-d889bc4efa06",
    "c5ff64fd-5e78-4cf0-848a-2788a78e71cd",
    "205c1a5b-c9bd-4137-8ac6-ca879c9a4f9c",
    "b676edbe-3286-4514-bc13-5cbe891c4bb1",
    "249efe96-14d2-443d-ad47-5621ed43a343",
    "439f2b66-09d4-4141-8c3d-b4dc18ef8a07",
    "f873c7d5-8652-466d-806b-b5da46f0f786",
    "e8097a7b-a280-4031-9df1-1e837193494f",
    "9e902471-9c77-49d3-a12c-516cc0808c3b",
}


def test_index_uuids_are_destination_not_source():
    """Pin every index to its HARMONIZED destination UUID; forbid the sources."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _INDEX_UUIDS,
    )

    assert _INDEX_UUIDS == _DESTINATION_UUIDS
    assert not (set(_INDEX_UUIDS.values()) & _SOURCE_UUIDS_FORBIDDEN), (
        "step is querying a raw SOURCE scrape index — taxon search will silently "
        "degrade to name-string matching. Use the harmonized destination UUID."
    )


def test_harmonized_filter_is_uniform_taxon_iri():
    """The harmonized filter collapsed to the uniform canonical-taxon-IRI field
    on every index — a single IRI value returns the whole strain rollup."""
    from apecx_integration.composition.steps.harmonized_search_execute_step import (
        _HARMONIZED_FILTER,
        _build_filter_values,
    )

    for index, spec in _HARMONIZED_FILTER.items():
        assert spec == {"field": "subjects.valueUri", "shape": "iri"}, index

    # _build_filter_values returns the single canonical IRI (not a strain-name
    # enumeration) — that single value is what rolls up all strains.
    plan = {
        "index": "bvbrc_genome",
        "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
        "canonical_label": "Chikungunya virus",
        "synonyms": ["CHIKV", "Chikungunya fever virus"],
    }
    assert _build_filter_values(plan) == ["http://purl.obolibrary.org/obo/NCBITaxon_37124"]
