"""Live (real BV-BRC) tests for the virus-name -> taxon resolver.

Proves against the real BV-BRC taxonomy index that an ARBITRARY virus name (SARS-CoV-2 /
influenza / HIV — none of which are in the curated ``_TAXON_SPECIES`` map) resolves to a
real NCBI taxon_id whose taxon ACTUALLY has BV-BRC sequence coverage (the property that
guarantees the conservation leg can fetch sequences). Mock/integration parity for the
behaviors unit-tested in ``tests/unit/test_taxonomy_resolver.py``.

Gated on BV-BRC reachability (auto-skip, honest). CC-1: every happy path asserts NON-EMPTY
real values; the unresolvable case asserts a real ``None`` degrade.
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.integration

_BVBRC_API = "https://www.bv-brc.org/api"


def _bvbrc_reachable() -> bool:
    try:
        r = requests.get(
            f"{_BVBRC_API}/taxonomy/?eq(taxon_id,2697049)&select(taxon_id)&limit(1)"
            "&http_accept=application/json",
            timeout=8,
        )
        return r.ok and isinstance(r.json(), list)
    except Exception:
        return False


needs_bvbrc = pytest.mark.skipif(not _bvbrc_reachable(), reason="BV-BRC taxonomy API unreachable")


def _bvbrc_has_features(taxon_id: int) -> int:
    """Count BV-BRC CDS features at the (species) taxon node — proves sequence coverage."""
    r = requests.get(
        f"{_BVBRC_API}/genome_feature/?eq(taxon_id,{taxon_id})&eq(feature_type,CDS)"
        f"&select(patric_id)&limit(5)&http_accept=application/json",
        timeout=20,
    )
    r.raise_for_status()
    return len(r.json())


@pytest.fixture(autouse=True)
def _clear_cache():
    from apecx_integration.agents.globus_search import taxonomy_resolver as tr

    tr._clear_cache()
    yield
    tr._clear_cache()


@needs_bvbrc
@pytest.mark.parametrize(
    "query,expected_taxon_id",
    [
        ("SARS-CoV-2 spike glycoprotein conserved epitopes", 2697049),
        ("influenza hemagglutinin broadly neutralizing epitopes", 11320),
        ("HIV-1 envelope gp120 epitopes", 11676),
        ("chikungunya envelope epitopes", 37124),
        ("dengue virus envelope domain III", 12637),
    ],
)
def test_resolve_query_to_real_taxon_with_coverage(query, expected_taxon_id):
    from apecx_integration.agents.globus_search import taxonomy_resolver as tr

    res = tr.resolve_query_to_taxon(query)
    assert res is not None, f"CC-1: {query!r} must resolve to a real taxon"
    assert res.taxon_id == expected_taxon_id, (query, res)
    assert res.genomes > 0, ("resolved taxon must report BV-BRC genome coverage", res)
    # The resolved taxon ACTUALLY has fetchable CDS features (the conservation-leg guarantee).
    assert _bvbrc_has_features(res.taxon_id) >= 1, (
        "resolved taxon must have BV-BRC CDS features for the sequence fetch",
        res,
    )


@needs_bvbrc
def test_unresolvable_name_returns_none_live():
    from apecx_integration.agents.globus_search import taxonomy_resolver as tr

    assert tr.resolve_query_to_taxon("Unobtainium virus glycoprotein") is None


@needs_bvbrc
def test_no_virus_name_returns_none_live():
    from apecx_integration.agents.globus_search import taxonomy_resolver as tr

    assert tr.resolve_query_to_taxon("envelope glycoprotein structure") is None
