"""Unit tests for relevance-ordered PubMed eSearch in the harvest helper.

The harvest used to collect PMIDs in eSearch's DEFAULT order (most-recent-first)
and keep the first ``max_papers`` — so for a high-volume term, older-but-relevant
records were cut before fetch. The fix issues a single eSearch with
``sort=relevance`` (Best Match). These tests pin that contract with SYNTHETIC
fixtures only: the eSearch HTTP call is mocked (no network, no benchmark content).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import apecx_integration.composition.steps._pubmed_helpers as ph


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    @property
    def content(self) -> bytes:
        return json.dumps(self._payload).encode()

    def raise_for_status(self) -> None:
        pass


class _FakeAsyncClient:
    """Captures the eSearch request params and returns a canned idlist."""

    captured: dict = {}

    def __init__(self, idlist: list[str]):
        self._idlist = idlist

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        type(self).captured = {"url": url, "params": params}
        return _FakeResponse({"esearchresult": {"idlist": self._idlist}})


def _install_fake_esearch(monkeypatch, idlist: list[str]):
    """Patch httpx.AsyncClient to a fake yielding ``idlist``; silence the sleep."""
    _FakeAsyncClient.captured = {}

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(idlist))

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(ph.asyncio, "sleep", _no_sleep)


# ---------------------------------------------------------------------
# _esearch_relevance_pmids — request params
# ---------------------------------------------------------------------


def test_esearch_request_uses_sort_relevance_and_retmax_equals_max_papers(monkeypatch):
    _install_fake_esearch(monkeypatch, ["1", "2", "3"])
    asyncio.run(ph._esearch_relevance_pmids("synthetic term", 3))
    params = _FakeAsyncClient.captured["params"]
    assert params["sort"] == "relevance"
    assert params["retmax"] == 3
    assert params["db"] == "pubmed"
    assert params["term"] == "synthetic term"
    assert params["retmode"] == "json"


def test_esearch_returns_idlist_in_mocked_order(monkeypatch):
    ordered = ["30", "10", "20", "40"]
    _install_fake_esearch(monkeypatch, ordered)
    out = asyncio.run(ph._esearch_relevance_pmids("term", 10))
    # Order follows the (relevance-ranked) idlist verbatim.
    assert out == ordered


def test_esearch_caps_result_at_max_papers(monkeypatch):
    _install_fake_esearch(monkeypatch, ["a", "b", "c", "d", "e"])
    out = asyncio.run(ph._esearch_relevance_pmids("term", 2))
    assert out == ["a", "b"]


def test_esearch_no_cap_uses_ceiling_retmax(monkeypatch):
    """max_papers <= 0 → retmax is the eSearch single-query ceiling, no slice."""
    _install_fake_esearch(monkeypatch, ["a", "b", "c"])
    out = asyncio.run(ph._esearch_relevance_pmids("term", 0))
    params = _FakeAsyncClient.captured["params"]
    assert params["retmax"] == ph._ESEARCH_MAX_RETMAX
    assert out == ["a", "b", "c"]


def test_esearch_raises_on_esearch_error(monkeypatch):
    import httpx

    class _ErrClient(_FakeAsyncClient):
        async def get(self, url, params=None):
            return _FakeResponse({"esearchresult": {"ERROR": "bad request"}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _ErrClient([]))

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(ph.asyncio, "sleep", _no_sleep)
    try:
        asyncio.run(ph._esearch_relevance_pmids("term", 5))
    except ValueError as exc:
        assert "PubMed eSearch error" in str(exc)
    else:
        raise AssertionError("expected ValueError on eSearch ERROR")


# ---------------------------------------------------------------------
# harvest — pmids flow through to container_to_dict in relevance order + cap
# ---------------------------------------------------------------------


def _fake_container(pmid: str):
    return SimpleNamespace(
        titles=[SimpleNamespace(title=f"title-{pmid}")],
        creators=[],
        publicationYear=None,
        dates=[],
        publisher=None,
        identifier=None,
        alternateIdentifiers=[
            SimpleNamespace(alternateIdentifierType="PMID", alternateIdentifier=pmid)
        ],
        descriptions=[],
    )


def test_harvest_preserves_relevance_order_and_cap(monkeypatch):
    _install_fake_esearch(monkeypatch, ["7", "3", "9", "1"])

    class _FakeHarvester:
        async def iter_results(self, pmids):
            for pmid in pmids:
                yield SimpleNamespace(ok=True, record=_fake_container(pmid), error=None)

    import apecx_harvesters.loaders.pubmed.retrieve as retrieve

    monkeypatch.setattr(retrieve, "PubMedHarvester", _FakeHarvester)

    out = asyncio.run(ph.harvest("term", max_papers=3))
    # Capped to 3, and in the eSearch (relevance) order.
    assert [r["pmid"] for r in out] == ["7", "3", "9"]
