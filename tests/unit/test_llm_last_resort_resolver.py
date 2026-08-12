"""Unit tests for the I7 last-resort LLM taxon resolver.

The resolver REUSES the existing 3-step chain (synonym_gen -> bvbrc_search -> taxon_review),
so these tests monkeypatch only the LEAF I/O boundaries (LLM factory + preflight, BV-BRC HTTP,
CDS count) and let the REAL steps run — in particular the REAL CDS-coverage gate inside
TaxonCandidateReviewStep decides success vs miss.

Mock/integration parity: the same chain is exercised against a real LLM + real BV-BRC in
tests/integration/test_taxon_resolution_fallback.py (auto-skipped when either is unavailable).
"""

from __future__ import annotations

import threading
import time

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


# ─────────────────────────────────────────────────────────────────────────
# I7 CONCURRENCY HARDENING (FIX 1). The single caller _run_miss_envelope runs
# PER-INDEX and the ~9 destination indices fan out CONCURRENTLY on worker
# threads, so ONE unresolved term produces up to ~9 concurrent same-term calls
# sharing module-global state. These tests pin: (1b) in-flight dedup — the real
# chain runs ONCE, not once-per-thread; (1a) the step-build race — _STEPS ends
# with exactly 3 entries, never N×3.
# ─────────────────────────────────────────────────────────────────────────


def test_concurrent_same_term_resolves_chain_once_and_builds_steps_once(monkeypatch):
    """FIX 1a+1b: N threads resolve the SAME term simultaneously. The real 3-step chain must run
    exactly ONCE (in-flight dedup) — proven by counting synonym-step LLM builds — every thread must
    get the same verdict, no exception, and _STEPS must hold exactly 3 entries (build-race guard),
    not N×3. FAILS before FIX 1 (each thread races the cache+build → many chain runs / N×3 steps).
    """
    counts = {"syn": 0, "rev": 0}
    counts_lock = threading.Lock()

    monkeypatch.setattr(llm_config, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(llm_config, "llm_model_available", lambda *a, **k: True)
    monkeypatch.setattr(syn_mod, "preflight_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(review_mod, "preflight_llm_model", lambda *a, **k: None)

    def _syn_llm(**k):  # noqa: ANN003, ARG001
        with counts_lock:
            counts["syn"] += 1
        return _FakeLLM("Lassa virus\nLassa mammarenavirus")

    def _rev_llm(**k):  # noqa: ANN003, ARG001
        with counts_lock:
            counts["rev"] += 1
        return _FakeLLM("11620")

    monkeypatch.setattr(syn_mod, "build_chat_llm", _syn_llm)
    monkeypatch.setattr(review_mod, "build_chat_llm", _rev_llm)
    monkeypatch.setattr(
        bvbrc_mod.BvbrcTaxonomySearchStep, "_get_json", lambda self, p, q: [_LASSA_ROW]
    )
    monkeypatch.setattr(bvbrc_mod, "cds_count", lambda *a, **k: 100)
    monkeypatch.setattr(review_mod, "cds_count", lambda *a, **k: 100)

    n = 12
    barrier = threading.Barrier(n)
    results: dict[int, int | None] = {}
    errors: list[BaseException] = []
    res_lock = threading.Lock()

    def _worker(i: int) -> None:
        try:
            barrier.wait()  # release all threads at once to maximize the race window
            verdict = resolver.resolve_taxon_last_resort("Lassa virus")
            with res_lock:
                results[i] = verdict
        except BaseException as exc:  # noqa: BLE001 - record so the assertion can surface it
            with res_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"resolver raised under concurrency: {errors!r}"
    assert len(results) == n
    assert set(results.values()) == {11620}, results
    # In-flight dedup (FIX 1b): the real chain ran exactly once regardless of the 12 callers.
    assert counts["syn"] == 1, f"synonym chain ran {counts['syn']}x (expected 1 — dedup broken)"
    assert counts["rev"] == 1, f"review chain ran {counts['rev']}x (expected 1 — dedup broken)"
    # Build-race guard (FIX 1a): exactly the 3 chain steps, not N×3.
    assert len(resolver._STEPS) == 3, f"_STEPS holds {len(resolver._STEPS)} entries (expected 3)"


def test_concurrent_get_steps_build_race_publishes_exactly_three(monkeypatch):
    """FIX 1a in isolation: many threads racing the FIRST _get_steps() build (with a slow
    per-step construction to widen the window) must publish exactly 3 steps, never N×3. FAILS
    before FIX 1a (check-then-act append lets every thread build+append its own 3)."""

    class _FakeModule:
        def __getattr__(self, _name):  # noqa: ANN001
            class _Builder:
                @staticmethod
                def from_config(_cfg):  # noqa: ANN001
                    time.sleep(0.01)  # widen the race window
                    return object()

            return _Builder

    monkeypatch.setattr(resolver.importlib, "import_module", lambda _m: _FakeModule())

    n = 10
    barrier = threading.Barrier(n)
    seen: list[int] = []
    seen_lock = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        steps = resolver._get_steps()
        with seen_lock:
            seen.append(len(steps))

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(resolver._STEPS) == 3, f"_STEPS holds {len(resolver._STEPS)} entries (expected 3)"
    assert seen == [3] * n, f"every caller must see exactly 3 steps, got {seen}"
