"""B3 — class-name substring re-rank tests.

The re-ranker is a pure function over retrieval hits; tests cover:

  1. Hits whose class_name matches a prompt token are boosted to the
     top of the list.
  2. CamelCase decomposition works (``EntityExtractionStep`` matches
     ``entity`` OR ``extraction``).
  3. Short tokens (< 3 chars) are filtered so common words don't
     bias the ranking.
  4. Ties broken by original retrieval score (FAISS / substring
     stays authoritative for components without explicit naming).
  5. A prompt with no useful tokens returns hits in their original
     order (no-op safety).
"""

from __future__ import annotations

from apecx_integration.composition.component_catalog import (
    CatalogComponent,
    SearchHit,
)
from apecx_integration.composition.composer import _rerank_by_class_name_match


def _hit(class_name: str, score: int = 0) -> SearchHit:
    return SearchHit(
        component=CatalogComponent(
            id=class_name.lower(),
            name=class_name,
            description="fixture",
            class_path=f"pkg.lib.{class_name}",
            yaml_path=f"steps/{class_name.lower()}.yml",
            examples=(),
        ),
        score=score,
    )


def test_rerank_promotes_class_named_in_prompt():
    hits = [
        _hit("GenericSearch", score=900),
        _hit("EntityExtractionStep", score=500),
        _hit("Misc", score=100),
    ]
    out = _rerank_by_class_name_match(hits, "extract entities from text")
    assert out[0].component.name == "EntityExtractionStep", (
        "the prompt named the component class via the token "
        "'extract'; it must surface first regardless of retrieval "
        "score order"
    )


def test_rerank_splits_camelcase_for_token_matching():
    hits = [
        _hit("GenericSearch", score=900),
        _hit("EntityExtractionStep", score=500),
    ]
    out = _rerank_by_class_name_match(hits, "I want extraction logic")
    assert out[0].component.name == "EntityExtractionStep"


def test_rerank_filters_short_tokens():
    """Tokens like 'a' / 'of' / 'to' must not match — they'd boost
    everything."""
    hits = [
        _hit("AlphaStep", score=100),
        _hit("BetaStep", score=200),
    ]
    out = _rerank_by_class_name_match(hits, "a step to perform")
    # Original score wins (BetaStep > AlphaStep) — no token boost
    # would apply for either.
    assert out[0].component.name == "BetaStep"


def test_rerank_ties_broken_by_original_score():
    """Same match_count (0 here) → original score determines order."""
    hits = [
        _hit("AlphaStep", score=100),
        _hit("BetaStep", score=900),
        _hit("GammaStep", score=500),
    ]
    out = _rerank_by_class_name_match(hits, "describe something general")
    names = [h.component.name for h in out]
    assert names == ["BetaStep", "GammaStep", "AlphaStep"]


def test_rerank_no_tokens_in_prompt_returns_original_order():
    """An empty / token-less prompt must NOT reorder hits — the
    contract is "boost only when there's a signal."""
    hits = [
        _hit("AlphaStep", score=100),
        _hit("BetaStep", score=900),
    ]
    out = _rerank_by_class_name_match(hits, "?? !!")
    names = [h.component.name for h in out]
    assert names == ["AlphaStep", "BetaStep"]


def test_rerank_with_multiple_matches_uses_match_count():
    """A class whose name shares MORE tokens with the prompt wins
    over one that shares fewer, even when the latter has higher
    base score."""
    hits = [
        _hit("GenericSearchStep", score=1000),
        _hit("EntityExtractionStep", score=100),
    ]
    # Two tokens of EntityExtractionStep match the prompt
    # ('entity' and 'extraction'); GenericSearchStep matches zero.
    out = _rerank_by_class_name_match(hits, "entity extraction please")
    assert out[0].component.name == "EntityExtractionStep"
