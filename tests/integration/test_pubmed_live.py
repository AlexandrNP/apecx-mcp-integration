"""Live integration tests for PubMed E-utils client (Phase 4B, 2026-05-04).

Exercises search_alphavirus_literature() against the real NCBI Entrez API
to confirm the esearch → efetch → XML-parse pipeline returns structured
LiteratureReference objects with real metadata (title, authors, PMID, etc.).

No full text is fetched — only article metadata and abstract.

Gate: APECX_PUBMED_LIVE=1  (skipped by default to avoid live NCBI traffic in CI)

Optional env vars:
  NCBI_EMAIL    — email sent with requests (default: research@nanobrain.org)
  NCBI_API_KEY  — NCBI API key for 10 req/s limit (default: none → 3 req/s)

Mock parity: closes T-2026-04-23-03 in tests/integration/TODO.md.
The test_pubmed_search_is_implemented smoke test in test_nanobrain_mocks_policy.py
verifies the method is implemented without live traffic; this file verifies
it returns real data.

To run:

    APECX_PUBMED_LIVE=1 \\
        PYTHONPATH=../nanobrain:src .venv/bin/python -m pytest \\
        tests/integration/test_pubmed_live.py -v
"""

from __future__ import annotations

import asyncio
import os

import pytest

_LIVE = os.environ.get("APECX_PUBMED_LIVE", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="Set APECX_PUBMED_LIVE=1 to run live NCBI E-utils integration tests.",
)

NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "research@nanobrain.org")
NCBI_API_KEY = os.environ.get("NCBI_API_KEY") or None


# ---------------------------------------------------------------------------
# Fixtures — module-scoped to avoid back-to-back NCBI 429s
# ---------------------------------------------------------------------------


def _make_client(cache_results: bool = False):
    """Build a minimal PubMedClient without from_config."""
    from nanobrain.library.tools.bioinformatics.pubmed_client import PubMedClient

    client = PubMedClient.__new__(PubMedClient)
    client.logger = __import__("logging").getLogger("test.pubmed")
    client.last_request_time = 0
    client.request_count = 0
    client.search_cache = {}

    class _Cfg:
        base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
        max_results = 5
        api_key = NCBI_API_KEY

    _Cfg.cache_results = cache_results
    client.pubmed_config = _Cfg()
    client.email = NCBI_EMAIL
    client.api_key = NCBI_API_KEY
    client.rate_limit = 10 if NCBI_API_KEY else 3
    return client


@pytest.fixture(scope="module")
def capsid_results():
    """Fetch results for 'capsid protein' once per session; share across tests."""
    client = _make_client(cache_results=False)
    return asyncio.run(client.search_alphavirus_literature("capsid protein"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_search_returns_nonempty_list(capsid_results) -> None:
    """search_alphavirus_literature() returns at least one result for 'capsid protein'."""
    assert isinstance(capsid_results, list), f"Expected list, got {type(capsid_results)}"
    assert (
        len(capsid_results) > 0
    ), "Expected at least one PubMed result for Alphavirus capsid protein."


@pytest.mark.integration
def test_search_results_have_required_fields(capsid_results) -> None:
    """Each LiteratureReference has a non-empty PMID, title, and URL."""
    from nanobrain.library.tools.bioinformatics.pubmed_client import LiteratureReference

    assert len(capsid_results) > 0, "No results to check"
    for ref in capsid_results:
        assert isinstance(ref, LiteratureReference), f"Unexpected type: {type(ref)}"
        assert ref.pmid, f"Reference missing PMID: {ref}"
        assert ref.title, f"Reference missing title (PMID={ref.pmid})"
        assert ref.url.startswith(
            "https://pubmed.ncbi.nlm.nih.gov/"
        ), f"Unexpected URL format: {ref.url}"


@pytest.mark.integration
def test_search_results_have_authors(capsid_results) -> None:
    """At least one result has a non-empty authors list."""
    results_with_authors = [r for r in capsid_results if r.authors]
    assert results_with_authors, (
        "No results had authors — XML parsing may be broken. "
        f"Got {len(capsid_results)} results total."
    )


@pytest.mark.integration
def test_search_results_have_year(capsid_results) -> None:
    """At least one result has a 4-digit publication year."""
    results_with_year = [r for r in capsid_results if r.year and r.year.isdigit()]
    assert results_with_year, (
        "No results had a parseable year. " f"Got {len(capsid_results)} results total."
    )


@pytest.mark.integration
def test_search_abstract_present(capsid_results) -> None:
    """At least one result has a non-empty abstract."""
    results_with_abstract = [r for r in capsid_results if r.abstract]
    assert results_with_abstract, (
        "No results had an abstract — efetch XML parsing may be missing AbstractText. "
        f"Got {len(capsid_results)} results total, none with abstract."
    )


@pytest.mark.integration
def test_search_caching(capsid_results) -> None:
    """Second call with cache_results=True is served from in-memory cache (no network)."""
    import time

    cached_client = _make_client(cache_results=True)
    # Pre-seed cache with already-fetched results to avoid a second NCBI round-trip.
    # The caching logic is purely in-memory — no need to fetch again just to test it.
    cache_key = "alphavirus_capsid_protein"
    cached_client.search_cache[cache_key] = capsid_results

    count_before = cached_client.request_count
    t0 = time.monotonic()
    results2 = asyncio.run(cached_client.search_alphavirus_literature("capsid protein"))
    elapsed = time.monotonic() - t0

    assert results2 == capsid_results, "Cached result differs from seed"
    assert (
        cached_client.request_count == count_before
    ), "request_count incremented on cached call — cache is not being checked"
    assert elapsed < 0.5, f"Cached call took {elapsed:.2f}s — expected <0.5s (no network)"
