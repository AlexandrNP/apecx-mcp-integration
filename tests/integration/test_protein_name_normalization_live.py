"""Protein-name normalization against REAL BV-BRC (parity for the mocked unit tests).

The unit tests (`test_protein_name_normalization_step.py`, `test_bvbrc_product_exact_query.py`)
monkeypatch the BV-BRC catalog/fetch to pin the token-subset match + the exact-vs-wildcard query.
This module exercises the SAME behavior against the live BV-BRC data API + real MAFFT, per the
unit-mock / integration-test parity rule (closes TODO T-2026-07-01-01):

  * a user name "E2 glycoprotein" (which BV-BRC's wildcard `eq(product,*E2 glycoprotein*)` CANNOT
    retrieve for EEEV — no literal product, and the verbose "E2 envelope glycoprotein" is only
    reachable via an EXACT query) is normalized to "E2 envelope glycoprotein" + product_exact, and
    the fetch then returns REAL E2 mat_peptide sequences with NO substitution to a different protein;
  * a name that already matches ("capsid") passes through unchanged (no regression).

Gated on BV-BRC reachability (all tests) + a local MAFFT binary (the full-cascade test only).
"""

from __future__ import annotations

import asyncio
import shutil

import pytest
import requests

pytestmark = pytest.mark.integration

_EEEV_TAXON = (
    11021  # Eastern equine encephalitis virus — E2/E1 are mat_peptide "…envelope glycoprotein"
)
_CHIKV_TAXON = 37124


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            "https://www.bv-brc.org/api/genome_feature/"
            f"?eq(taxon_id,{_EEEV_TAXON})&limit(1)&http_accept=application/json",
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


needs_bvbrc = pytest.mark.skipif(not _bvbrc_reachable(), reason="BV-BRC not reachable")
needs_mafft_bvbrc = pytest.mark.skipif(
    shutil.which("mafft") is None or not _bvbrc_reachable(),
    reason="needs a local MAFFT binary AND BV-BRC reachable",
)


def _norm_step():
    from apecx_integration.composition.steps.protein_name_normalization_step import (
        ProteinNameNormalizationStep,
    )

    return ProteinNameNormalizationStep.from_config({"name": "norm"})


def _fetch_step():
    from apecx_integration.composition.steps.bvbrc_protein_fasta_step import BvbrcProteinFastaStep

    return BvbrcProteinFastaStep.from_config({"name": "fetch", "max_sequences": 25})


@needs_bvbrc
def test_normalizes_e2_glycoprotein_to_verbose_product_real_bvbrc():
    out = asyncio.run(
        _norm_step().process({"taxon_id": _EEEV_TAXON, "protein": "E2 glycoprotein"})
    )["norm_out"]
    assert out["protein"] == "E2 envelope glycoprotein"
    assert out["feature_type"] == "mat_peptide"
    assert out["match_source"] == "bvbrc_token_subset"
    assert out["product_exact"] is True
    assert out["original_protein"] == "E2 glycoprotein"


@needs_bvbrc
def test_exact_query_fetch_gets_real_e2_without_substitution():
    # Feed the normalized payload to the real fetch; the exact eq(product,"…") query must retrieve
    # the verbose product (the wildcard returns 0 for it) — so NO fallback substitution.
    norm = asyncio.run(
        _norm_step().process({"taxon_id": _EEEV_TAXON, "protein": "E2 glycoprotein"})
    )["norm_out"]
    pf = asyncio.run(_fetch_step().process(norm))["protein_fasta"]
    assert pf["substituted_protein"] is None
    assert pf["protein"] == "E2 envelope glycoprotein"
    assert pf["feature_type"] == "mat_peptide"
    assert pf["n_sequences"] >= 2


@needs_bvbrc
def test_literal_match_passes_through_real_bvbrc():
    # "capsid" already matches a CHIKV product → passthrough (no product_exact, current fetch path).
    out = asyncio.run(_norm_step().process({"taxon_id": _CHIKV_TAXON, "protein": "capsid"}))[
        "norm_out"
    ]
    assert out["protein"] == "capsid"
    assert out["match_source"] == "passthrough"
    assert out["product_exact"] is False


@needs_mafft_bvbrc
def test_conserved_sites_core_normalizes_e2_end_to_end():
    # Full nested cascade (normalize_protein → fetch → align → conserve → report) via Workflow.run.
    # Guards the G127 silent-empty at nesting AND asserts the fetch used the normalized E2 product.
    from apecx_integration.composition.workflows.viral_conserved_sites.builder import (
        build_viral_conserved_sites_core_workflow,
    )

    async def _run():
        wf = build_viral_conserved_sites_core_workflow()
        out = await wf.run(
            {"fetch_in": {"taxon_id": _EEEV_TAXON, "protein": "E2 glycoprotein"}},
            timeout=240,
            settle_ms=1500,
        )
        assert isinstance(out.get("workflow_output"), dict), "G127 silent-empty: workflow_output"
        pf = None
        for _name, du in wf.child_steps["fetch"].step_output_data_units.items():
            v = du.get()
            if asyncio.iscoroutine(v):
                v = await v
            if isinstance(v, dict):
                pf = v.get("protein_fasta", v)
        return out["workflow_output"], pf

    report, pf = asyncio.run(_run())
    assert report.get("markdown", "").strip(), "conservation report markdown is empty"
    assert pf is not None, "fetch produced no protein_fasta output"
    assert pf["substituted_protein"] is None
    assert pf["protein"] == "E2 envelope glycoprotein"
