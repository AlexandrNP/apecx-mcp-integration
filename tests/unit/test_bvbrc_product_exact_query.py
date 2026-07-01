"""Exact-vs-wildcard BV-BRC product query (2026-07-01 protein-name-normalization finding).

BV-BRC's Solr ``product`` field returns 0 for a WILDCARDED multi-word phrase (e.g.
``eq(product,*E2 envelope glycoprotein*)``) even when that exact product exists (real: EEEV E2 has
517 mat_peptide features, wildcard → 0, exact → 75). So when ProteinNameNormalizationStep resolves a
user name to a VERBATIM catalog product it sets ``product_exact=True``, and BvbrcProteinFastaStep must
query with an EXACT ``eq(product,"…")`` clause instead of the wildcard. Without the flag the wildcard
is kept verbatim — zero change to every existing (unresolved-substring) call path.

Network is stubbed (``_get_json`` / ``_fetch`` monkeypatched) so the query-clause + flag threading are
pinned deterministically.

Covered against real BV-BRC by
``tests/integration/test_protein_name_normalization_live.py`` (per the parity rule).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apecx_integration.composition.steps.bvbrc_protein_fasta_step import BvbrcProteinFastaStep


def _stage(tmp_path: Path) -> BvbrcProteinFastaStep:
    p = tmp_path / "fasta.yml"
    p.write_text("name: fasta_test\n")
    return BvbrcProteinFastaStep.from_config(str(p))


def _recs(n: int) -> list[dict]:
    return [
        {
            "id": f"f{i}",
            "product": "E2 envelope glycoprotein",
            "genome_name": "g",
            "sequence": "MKAAVT",
        }
        for i in range(n)
    ]


def test_query_features_exact_builds_quoted_clause(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    captured: dict[str, str] = {}

    def fake_get_json(path, query):
        captured["query"] = query
        return []

    monkeypatch.setattr(step, "_get_json", fake_get_json)
    step._query_features(11021, "E2 envelope glycoprotein", "mat_peptide", exact=True)
    assert 'eq(product,"E2 envelope glycoprotein")' in captured["query"]
    assert "eq(product,*" not in captured["query"]  # NOT the wildcard form


def test_query_features_default_is_wildcard(tmp_path, monkeypatch):
    step = _stage(tmp_path)
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        step, "_get_json", lambda path, query: captured.setdefault("query", query) or []
    )
    step._query_features(11021, "E1", "CDS")  # exact defaults to False
    assert "eq(product,*E1*)" in captured["query"]
    assert 'eq(product,"' not in captured["query"]


def test_process_threads_product_exact_to_fetch(tmp_path, monkeypatch):
    # A normalized payload (product_exact=True) must reach _fetch as exact=True for the SAME protein,
    # and produce a non-substituted result.
    step = _stage(tmp_path)
    calls: list[tuple[str, str, bool]] = []

    def fake_fetch(taxon_id, protein, feature_type, exact=False):
        calls.append((protein, feature_type, exact))
        return _recs(3), 3, 0

    monkeypatch.setattr(step, "_fetch", fake_fetch)
    out = asyncio.run(
        step.process(
            {
                "taxon_id": 11021,
                "protein": "E2 envelope glycoprotein",
                "feature_type": "mat_peptide",
                "product_exact": True,
            }
        )
    )
    assert ("E2 envelope glycoprotein", "mat_peptide", True) in calls
    assert out["protein_fasta"]["substituted_protein"] is None


def test_process_without_flag_uses_wildcard(tmp_path, monkeypatch):
    # No product_exact on the payload → _fetch is called with exact=False (current behavior).
    step = _stage(tmp_path)
    calls: list[tuple[str, str, bool]] = []

    def fake_fetch(taxon_id, protein, feature_type, exact=False):
        calls.append((protein, feature_type, exact))
        return _recs(3), 3, 0

    monkeypatch.setattr(step, "_fetch", fake_fetch)
    asyncio.run(step.process({"taxon_id": 11021, "protein": "E1"}))
    assert calls and all(exact is False for (_p, _ft, exact) in calls)
