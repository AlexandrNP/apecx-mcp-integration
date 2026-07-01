"""Protein-name normalization (2026-07-01): resolve a user protein name to BV-BRC's product term.

The sequence-conservation fetch uses a SUBSTRING product filter, so a name like "E2 glycoprotein"
never matches BV-BRC's verbose "E2 envelope glycoprotein" ("envelope" is interleaved between the two
tokens) and the leg silently substitutes a DIFFERENT protein. ProteinNameNormalizationStep rewrites
the name to the taxon's real product term BEFORE the fetch — but ONLY when the literal name would
match nothing (fallback-only, so queries that already fetch are never second-guessed), and only via
TOKEN-SUBSET (v1 does not bridge true synonyms like "spike"→"surface glycoprotein"). It NEVER raises:
missing params / empty catalog / BV-BRC errors all pass the original name through.

``_query_catalog`` is monkeypatched (no network) so the match/passthrough control-flow is pinned
deterministically.

Covered against real BV-BRC by
``tests/integration/test_protein_name_normalization_live.py`` (per the parity rule).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.composition.steps.protein_name_normalization_step import (
    ProteinNameNormalizationStep,
)


def _stage(tmp_path: Path) -> ProteinNameNormalizationStep:
    p = tmp_path / "norm.yml"
    p.write_text("name: norm_test\n")
    return ProteinNameNormalizationStep.from_config(str(p))


def _out(step: ProteinNameNormalizationStep, payload: dict) -> dict:
    return asyncio.run(step.process(payload))["norm_out"]


def test_interleaved_modifier_normalizes(tmp_path, monkeypatch):
    # "E2 glycoprotein" can't substring-match "E2 envelope glycoprotein" (envelope interleaved), but
    # {e2, glycoprotein} ⊆ {e2, envelope, glycoprotein} → normalize to the real product term.
    step = _stage(tmp_path)
    monkeypatch.setattr(
        step,
        "_query_catalog",
        lambda taxon_id: {
            ("E2 envelope glycoprotein", "CDS"): 50,
            ("E1 envelope glycoprotein", "CDS"): 48,
            ("capsid protein", "mat_peptide"): 40,
        },
    )
    out = _out(step, {"taxon_id": 11021, "protein": "E2 glycoprotein"})
    assert out["protein"] == "E2 envelope glycoprotein"
    assert out["feature_type"] == "CDS"
    assert out["match_source"] == "bvbrc_token_subset"
    assert out["original_protein"] == "E2 glycoprotein"


def test_literal_substring_match_passes_through(tmp_path, monkeypatch):
    # "capsid" already word-boundary-matches "capsid protein" → the substring fetch works → do NOT
    # second-guess (fallback gate). Generic names must not be silently pinned to one product.
    step = _stage(tmp_path)
    monkeypatch.setattr(
        step,
        "_query_catalog",
        lambda taxon_id: {
            ("capsid protein", "CDS"): 30,
            ("E2 envelope glycoprotein", "CDS"): 50,
        },
    )
    out = _out(step, {"taxon_id": 11021, "protein": "capsid"})
    assert out["protein"] == "capsid"
    assert out["match_source"] == "passthrough"
    # feature_type was absent on input and must NOT be injected (fetch keeps its own default).
    assert "feature_type" not in out


def test_mat_peptide_product_sets_feature_type(tmp_path, monkeypatch):
    # No literal match; the best token-subset product is annotated mat_peptide → feature_type follows
    # it, so the fetch goes straight to the mature-peptide feature (not CDS).
    step = _stage(tmp_path)
    monkeypatch.setattr(
        step,
        "_query_catalog",
        lambda taxon_id: {
            ("E2 envelope glycoprotein", "mat_peptide"): 900,
            ("structural polyprotein", "CDS"): 200,
        },
    )
    out = _out(step, {"taxon_id": 11021, "protein": "E2 glycoprotein"})
    assert out["protein"] == "E2 envelope glycoprotein"
    assert out["feature_type"] == "mat_peptide"
    assert out["match_source"] == "bvbrc_token_subset"


def test_true_synonym_passes_through(tmp_path, monkeypatch):
    # "NS3" is a true synonym of "nonstructural protein 3" — NOT a token subset ("ns3" is not a token
    # in the annotation) — so v1 correctly passes it through (alias resolution is a deferred follow-up).
    step = _stage(tmp_path)
    monkeypatch.setattr(
        step,
        "_query_catalog",
        lambda taxon_id: {
            ("nonstructural protein 3", "mat_peptide"): 100,
            ("nonstructural polyprotein", "CDS"): 300,
        },
    )
    out = _out(step, {"taxon_id": 11021, "protein": "NS3"})
    assert out["protein"] == "NS3"
    assert out["match_source"] == "passthrough"


def test_below_threshold_passes_through(tmp_path, monkeypatch):
    # Only 1 of the 2 user tokens is present (score 0.5 < min_match_score 1.0) → passthrough.
    step = _stage(tmp_path)
    monkeypatch.setattr(
        step,
        "_query_catalog",
        lambda taxon_id: {("surface glycoprotein", "CDS"): 100},
    )
    out = _out(step, {"taxon_id": 11021, "protein": "membrane glycoprotein"})
    assert out["protein"] == "membrane glycoprotein"
    assert out["match_source"] == "passthrough"


def test_network_error_degrades_gracefully(tmp_path, monkeypatch):
    # A BV-BRC failure must NEVER break the fetch — pass the original name through.
    def boom(taxon_id):
        raise RuntimeError("BV-BRC unreachable")

    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_query_catalog", boom)
    out = _out(step, {"taxon_id": 11021, "protein": "E2 glycoprotein"})
    assert out["protein"] == "E2 glycoprotein"
    assert out["match_source"] == "passthrough"


def test_no_taxon_id_passes_through_without_querying(tmp_path, monkeypatch):
    # No usable taxon_id → passthrough WITHOUT even touching the network.
    def must_not_call(taxon_id):
        raise AssertionError("must not query the catalog without a taxon_id")

    step = _stage(tmp_path)
    monkeypatch.setattr(step, "_query_catalog", must_not_call)
    out = _out(step, {"protein": "E2 glycoprotein"})
    assert out["protein"] == "E2 glycoprotein"
    assert out["match_source"] == "passthrough"
