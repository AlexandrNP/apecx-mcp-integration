"""Unit tests for :class:`OLSClient` with mocked HTTP transport.

Real-OLS coverage lives in ``tests/integration/test_real_ols.py``
(env-gated).  These tests pin the client's parsing, caching, and retry
behaviour against synthesized responses.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from apecx_integration.synonym_dictionary.enums import OntologyName
from apecx_integration.synonym_dictionary.ols_client import (
    OLSClient,
    OLSError,
)


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> OLSClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5.0)
    return OLSClient(client=http, retry_attempts=2, backoff_base=0.0)


# ---------- search ----------


@pytest.mark.asyncio
async def test_search_returns_docs_array() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/search")
        assert req.url.params.get("q") == "EEEV"
        body = {
            "response": {
                "docs": [
                    {
                        "iri": "http://purl.obolibrary.org/obo/NCBITaxon_11021",
                        "label": "Eastern equine encephalitis virus",
                    }
                ]
            }
        }
        return httpx.Response(200, content=json.dumps(body))

    async with _make_client(handler) as client:
        docs = await client.search("EEEV", OntologyName.NCBITAXON)
    assert len(docs) == 1
    assert docs[0]["label"] == "Eastern equine encephalitis virus"


@pytest.mark.asyncio
async def test_search_caches_per_query_and_ontology() -> None:
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, content=json.dumps({"response": {"docs": []}}))

    async with _make_client(handler) as client:
        await client.search("foo", OntologyName.NCBITAXON)
        await client.search("foo", OntologyName.NCBITAXON)  # cache hit
        await client.search("foo", OntologyName.VO)  # different ontology = miss

    assert call_count == 2  # one per (query, ontology), not three


# ---------- get_term ----------


@pytest.mark.asyncio
async def test_get_term_returns_first_terms_entry() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = {
            "_embedded": {
                "terms": [
                    {
                        "iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
                        "label": "Chikungunya virus",
                        "synonyms": ["CHIKV", "Chikungunya"],
                    }
                ]
            }
        }
        return httpx.Response(200, content=json.dumps(body))

    async with _make_client(handler) as client:
        term = await client.get_term(
            OntologyName.NCBITAXON,
            "http://purl.obolibrary.org/obo/NCBITaxon_37124",
        )
    assert term is not None
    assert term["label"] == "Chikungunya virus"


@pytest.mark.asyncio
async def test_get_term_404_returns_none_not_error() -> None:
    """Deprecated IRIs from older snapshots should return None, not raise."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content="not found")

    async with _make_client(handler) as client:
        term = await client.get_term(
            OntologyName.NCBITAXON,
            "http://purl.obolibrary.org/obo/NCBITaxon_999999999",
        )
    assert term is None


# ---------- retry / 429 ----------


@pytest.mark.asyncio
async def test_429_retries_then_succeeds() -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, content=json.dumps({"response": {"docs": []}}))

    async with _make_client(handler) as client:
        await client.search("anything", OntologyName.NCBITAXON)
    assert attempts == 2


@pytest.mark.asyncio
async def test_503_exhausts_retries_then_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _make_client(handler) as client:
        with pytest.raises(OLSError):
            await client.search("x", OntologyName.NCBITAXON)


# ---------- helpers ----------


def test_extract_synonyms_unions_keys() -> None:
    term = {
        "synonyms": ["A", "B"],
        "annotation": {
            "has_exact_synonym": ["B", "C"],  # dedup against "synonyms"
            "has_related_synonym": ["D"],
            "alternative_term": ["E"],
        },
    }
    out = OLSClient.extract_synonyms(term)
    assert out == ("A", "B", "C", "D", "E")


def test_extract_synonyms_handles_none_and_empty() -> None:
    assert OLSClient.extract_synonyms(None) == ()
    assert OLSClient.extract_synonyms({}) == ()


def test_extract_label_handles_missing() -> None:
    assert OLSClient.extract_label({"label": "X"}) == "X"
    assert OLSClient.extract_label({}) is None
    assert OLSClient.extract_label(None) is None
