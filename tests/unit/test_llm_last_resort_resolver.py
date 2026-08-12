"""Unit tests for the I7 last-resort LLM taxon resolver.

The resolver REUSES the existing 3-step chain (synonym_gen -> bvbrc_search -> taxon_review),
so these tests monkeypatch only the LEAF I/O boundaries (LLM factory + preflight, BV-BRC HTTP,
CDS count) and let the REAL steps run — in particular the REAL CDS-coverage gate inside
TaxonCandidateReviewStep decides success vs miss.

Mock/integration parity: the same chain is exercised against a real LLM + real BV-BRC in
tests/integration/test_taxon_resolution_fallback.py (auto-skipped when either is unavailable).
"""

from __future__ import annotations

import pytest

import apecx_integration.agents._llm_config as llm_config
import apecx_integration.composition.steps.bvbrc_taxonomy_search_step as bvbrc_mod
import apecx_integration.composition.steps.taxon_candidate_review_step as review_mod
import apecx_integration.composition.steps.taxon_synonym_generation_step as syn_mod
from apecx_integration.composition.steps import _llm_last_resort_resolver as resolver


class _FakeLLM:
    """Minimal chat-LLM stand-in: ``.invoke(messages)`` returns an object with ``.content``."""

    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, messages):  # noqa: ANN001, ARG002 - signature parity with build_chat_llm()
        return type("_Resp", (), {"content": self._content})()


# A single BV-BRC taxonomy row for "Lassa mammarenavirus" (species-rank), well-covered.
_LASSA_ROW = {
    "taxon_id": 11620,
    "taxon_name": "Lassa mammarenavirus",
    "genomes": 500,
    "lineage_ids": [11617, 11620],
    "lineage_ranks": ["genus", "species"],
}


@pytest.fixture(autouse=True)
def _clear_caches():
    """Every test starts on a fresh resolver cache + step singletons + review verdict cache."""
    resolver._clear_cache()
    review_mod._clear_cache()
    yield
    resolver._clear_cache()
    review_mod._clear_cache()


def _wire_happy_chain(monkeypatch, *, cds: int, chosen: str = "11620") -> None:
    """Patch every leaf boundary so the REAL chain runs deterministically.

    ``cds`` is what the CDS gate sees for the winner (>= min_cds promotes; < min_cds is a miss).
    """
    # preflight is a no-op everywhere it is called (resolver gate + both LLM steps), so the test is
    # deterministic regardless of whether the dev machine has Ollama / the model pulled.
    monkeypatch.setattr(llm_config, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(llm_config, "llm_model_available", lambda *a, **k: True)
    monkeypatch.setattr(syn_mod, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(review_mod, "preflight_llm_model", lambda *a, **k: None)
    # synonym step LLM -> a couple of candidate spellings; review step LLM -> the chosen taxon_id.
    monkeypatch.setattr(
        syn_mod, "build_chat_llm", lambda **k: _FakeLLM("Lassa virus\nLassa mammarenavirus")
    )
    monkeypatch.setattr(review_mod, "build_chat_llm", lambda **k: _FakeLLM(chosen))
    # BV-BRC taxonomy HTTP -> one covered species candidate; CDS probe -> controlled count.
    monkeypatch.setattr(
        bvbrc_mod.BvbrcTaxonomySearchStep, "_get_json", lambda self, p, q: [_LASSA_ROW]
    )
    monkeypatch.setattr(bvbrc_mod, "cds_count", lambda *a, **k: cds)
    monkeypatch.setattr(review_mod, "cds_count", lambda *a, **k: cds)


def test_degrade_loud_preflight_raises_returns_none(monkeypatch):
    """The single most important guard: if the LLM preflight raises, the resolver returns None
    (no exception) so the caller falls through to the deterministic raw path."""

    def _boom(*a, **k):
        raise RuntimeError("no Ollama")

    monkeypatch.setattr(llm_config, "preflight_llm_model", _boom)
    # If the chain were ever entered, this would blow up loudly — proving preflight gates first.
    monkeypatch.setattr(
        syn_mod,
        "build_chat_llm",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("LLM must not be built after preflight fails")
        ),
    )
    assert resolver.resolve_taxon_last_resort("some unresolvable term") is None


def test_success_resolves_cds_verified_taxon(monkeypatch):
    """A covered pick (cds >= min_cds) is promoted: the resolver returns the taxon_id."""
    _wire_happy_chain(monkeypatch, cds=100)
    assert resolver.resolve_taxon_last_resort("Lassa virus glycoprotein") == 11620


def test_cds_gate_miss_returns_none(monkeypatch):
    """A pick BELOW min_cds is a NAMED miss inside TaxonCandidateReviewStep — the resolver returns
    None (never a silently-wrong / uncovered taxon), so the caller falls through to raw."""
    _wire_happy_chain(monkeypatch, cds=1)  # default min_cds is 2
    assert resolver.resolve_taxon_last_resort("Lassa virus glycoprotein") is None


def test_no_candidate_match_returns_none(monkeypatch):
    """If the reviewer LLM matches no candidate id, that is a named miss -> None."""
    _wire_happy_chain(monkeypatch, cds=100, chosen="NONE")
    assert resolver.resolve_taxon_last_resort("Lassa virus glycoprotein") is None


def test_cache_short_circuits_no_reinvoke(monkeypatch):
    """A second call for the same term returns the cached verdict WITHOUT touching preflight or the
    LLM — proving the bounded single-attempt cache is consulted before any LLM work."""
    _wire_happy_chain(monkeypatch, cds=100)
    assert resolver.resolve_taxon_last_resort("Lassa virus") == 11620

    # Now make preflight AND both LLMs explode; a cache hit must never reach them.
    monkeypatch.setattr(
        llm_config,
        "preflight_llm_model",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("preflight must not run on a cache hit")
        ),
    )
    monkeypatch.setattr(
        syn_mod,
        "build_chat_llm",
        lambda **k: (_ for _ in ()).throw(AssertionError("LLM must not run on a cache hit")),
    )
    assert resolver.resolve_taxon_last_resort("Lassa virus") == 11620
    # Case/space-insensitive key: a differently-cased spelling still hits the cached verdict.
    assert resolver.resolve_taxon_last_resort("  lassa VIRUS ") == 11620


def test_empty_term_returns_none_without_llm(monkeypatch):
    """A blank term never triggers the LLM."""
    monkeypatch.setattr(
        llm_config,
        "preflight_llm_model",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("preflight must not run for a blank term")
        ),
    )
    assert resolver.resolve_taxon_last_resort("   ") is None
