"""Unit tests for KeywordWorkflowMatcher (EO-20 matcher impl)."""

from __future__ import annotations

import asyncio

from apecx_integration.composition.decomposition.local_decomposer import Task
from apecx_integration.composition.decomposition.matchers import KeywordWorkflowMatcher

_CATALOG = {
    "protein_align": "multiple sequence alignment of protein sequences",
    "pubmed_search": "search literature publications by keyword",
}


def test_matches_relevant_workflow():
    m = KeywordWorkflowMatcher(_CATALOG)
    r = asyncio.run(m.match(Task("align these protein sequences")))
    assert r is not None
    assert r.workflow_name == "protein_align"
    assert r.score > 0


def test_picks_higher_overlap():
    m = KeywordWorkflowMatcher(_CATALOG)
    r = asyncio.run(m.match(Task("search publications about influenza")))
    assert r is not None
    assert r.workflow_name == "pubmed_search"


def test_no_overlap_returns_none():
    m = KeywordWorkflowMatcher(_CATALOG)
    assert asyncio.run(m.match(Task("xyzzy frobnicate quux"))) is None


def test_empty_description_returns_none():
    m = KeywordWorkflowMatcher(_CATALOG)
    assert asyncio.run(m.match(Task(""))) is None


def test_deterministic():
    m = KeywordWorkflowMatcher(_CATALOG)
    r1 = asyncio.run(m.match(Task("align protein sequences")))
    r2 = asyncio.run(m.match(Task("align protein sequences")))
    assert r1 == r2
