"""BvbrcTaxonomySearchStep — real BV-BRC parity for parenthetical synonyms.

Regression for the 2026-08-12 recall hole: an LLM-proposed synonym carrying a parenthetical
acronym ("Zika virus (ZIKV)") broke BV-BRC RQL — the endpoint percent-DECODES the value then
RQL-parses it, so the literal '(' inside eq(taxon_name,...) returns HTTP 400 and the synonym
is dropped (logged as a warning then skipped — a recall hole, not a fully silent drop). The
fix (`_rql_safe_name`) strips the parenthetical before querying; the
stripped form ("Zika virus") also matches the real taxon_name. This live test hits the real
BV-BRC data API (no mocks) and asserts the parenthetical synonym now yields real candidates.
Auto-skips when the API is unreachable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import requests

from apecx_integration.composition.steps.bvbrc_taxonomy_search_step import BvbrcTaxonomySearchStep

pytestmark = pytest.mark.integration

_POWASSAN_TAXON = 11083  # confirmed-valid taxon id, used only for the reachability probe


def _stage(tmp_path: Path) -> BvbrcTaxonomySearchStep:
    p = tmp_path / "bvbrc_search.yml"
    p.write_text("name: bvbrc_search_paren_test\n")
    return BvbrcTaxonomySearchStep.from_config(str(p))


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            "https://www.bv-brc.org/api/taxonomy/"
            f"?eq(taxon_id,{_POWASSAN_TAXON})&limit(1)&http_accept=application/json",
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


needs_bvbrc = pytest.mark.skipif(not _bvbrc_reachable(), reason="BV-BRC taxonomy API not reachable")


@needs_bvbrc
def test_parenthetical_synonym_resolves(tmp_path):
    """A parenthetical synonym must resolve, not 400-and-drop.

    Before the fix the sole synonym "Zika virus (ZIKV)" 400'd server-side and was skipped,
    leaving zero candidates. After the fix the paren-stripped "Zika virus" returns real rows.
    """
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process(
            {"bvbrc_search_input": {"query": "zika", "taxon_synonyms": ["Zika virus (ZIKV)"]}}
        )
    )
    cands = out.get("taxon_candidates") or []
    assert cands, "parenthetical synonym must yield >=1 candidate (pre-fix: 400 -> warn+skip -> 0)"
    assert any("zika" in (c.get("taxon_name") or "").casefold() for c in cands), (
        f"expected a Zika taxon among candidates, got {[c.get('taxon_name') for c in cands]}"
    )
    assert all(isinstance(c.get("taxon_id"), int) for c in cands)
