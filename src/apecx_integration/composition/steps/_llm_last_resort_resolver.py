"""Last-resort LLM taxon resolution for the harmonized-search miss path (I7).

This module adds NO new LLM logic. It REUSES the existing, deterministic-first /
LLM-last-resort taxon-resolution chain already wired into ``viral_epitope_analysis`` —

    TaxonSynonymGenerationStep  (LLM widens candidate spellings)
    → BvbrcTaxonomySearchStep   (deterministic BV-BRC CDS ranking)
    → TaxonCandidateReviewStep  (LLM picks, then a deterministic CDS-coverage gate)

— and drives it SYNCHRONOUSLY for ONE harmonized-search miss term, returning the
CDS-gate-verified NCBI taxon_id (or ``None``).

Bounds / guards (I7):

* **Genuine non-umbrella miss only.** The single caller
  (``harmonized_search_execute_step._run_miss_envelope``) invokes this only AFTER the
  fail-close syndrome-umbrella check, i.e. on a genuine deterministic dict miss.
* **preflight gate.** ``preflight_llm_model()`` is called first; on ANY failure this
  returns ``None`` (skip silently to the raw fallback) — the LLM must never block or
  crash the deterministic path. This also covers desktop locus (no apecx-side LLM):
  the endpoint is unreachable, so a subsequent chain error degrades to ``None`` → raw.
* **Bounded single attempt.** A per-process FIFO-bounded cache (``BoundedDict``, the
  same pattern as ``taxon_candidate_review_step._REVIEW_CACHE``) keyed on the
  normalized term memoizes the verdict — one resolution attempt per distinct term.
* **CDS-gate-verified.** The CDS-coverage gate lives INSIDE ``TaxonCandidateReviewStep``
  (``min_cds``); a low-coverage pick is a NAMED miss (returns ``None`` here), never a
  silently-wrong taxon.

Sync / async mechanics
----------------------
``_run_miss_envelope`` runs SYNC — it is offloaded onto a worker thread via
``BaseStep.run_blocking``. The three chain steps expose ``async def process``. This
driver runs each via ``asyncio.run``, which creates a fresh event loop ON THE WORKER
THREAD (or, in a unit test, on the test thread — neither has a running loop), so the
main event loop is never touched and never blocked. This mirrors the proven
``tests/integration/test_taxon_resolution_fallback.py::_run_fallback_chain`` pattern.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from typing import Any

from apecx_integration._bounded_cache import BoundedDict

log = logging.getLogger(__name__)

# Concurrency (I7 hardening) — the single caller ``_run_miss_envelope`` runs PER-INDEX, and the
# ~9 destination indices fan out CONCURRENTLY on ``asyncio.to_thread`` worker threads. So ONE
# unresolved term can produce up to ~9 concurrent ``resolve_taxon_last_resort(term)`` calls sharing
# this module's mutable state. Three locks make that safe:
#   * ``_STEPS_LOCK`` guards the one-time step build (check-then-act on ``_STEPS``).
#   * ``_CACHE_LOCK`` serializes ``_LAST_RESORT_CACHE`` / ``_INFLIGHT`` mutation (OrderedDict
#     del+set+evict is not atomic; concurrent writers can lose a write or mutate-during-iterate).
#   * a per-term lock in ``_INFLIGHT`` collapses N concurrent same-term calls to ONE actual
#     resolution + (N-1) cache hits, so the per-term cost bound holds under fan-out.
_STEPS_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()

# Per-term in-flight resolution locks (guarded by ``_CACHE_LOCK``). An entry exists only while a
# term is being resolved for the first time; it is dropped once the verdict is cached (subsequent
# calls short-circuit on the cache before ever touching ``_INFLIGHT``, so it cannot leak).
_INFLIGHT: dict[str, threading.Lock] = {}

# Coarse overall wall-clock budget for one term's resolution (I7 hardening, FIX 3). The chain can
# stack two LLM calls (~300s each under a slow local model) plus a BV-BRC round-trip; each step
# carries its OWN timeout, but nothing bounded the STACK. This is a BETWEEN-STEPS check: it cannot
# preempt a step already running (per-step timeouts bound that), but it refuses to START a further
# step once the budget is blown, so the worst case degrades to raw instead of stacking unboundedly.
_OVERALL_BUDGET_S = 420.0

# taxon_id (int) for a CDS-verified resolution, or None for a remembered miss / unresolvable
# term. FIFO-bounded so a long-lived MCP server fielding many distinct miss terms cannot grow
# without limit (the same leak class BoundedDict guards elsewhere). All reads/writes are serialized
# under ``_CACHE_LOCK`` (see above) — a BoundedDict.__setitem__ del+set+evict is not atomic.
_LAST_RESORT_CACHE: BoundedDict = BoundedDict(maxsize=512)

# Lazily-built process singletons for the three chain steps. Building a nanobrain step is not
# free (executor + config), so the driver constructs each once and reuses it across miss terms.
_STEPS: list[tuple[Any, str]] = []

# (module, class, input-data-unit key) for the existing three-step resolution chain.
_CHAIN: tuple[tuple[str, str, str], ...] = (
    (
        "apecx_integration.composition.steps.taxon_synonym_generation_step",
        "TaxonSynonymGenerationStep",
        "synonym_gen_input",
    ),
    (
        "apecx_integration.composition.steps.bvbrc_taxonomy_search_step",
        "BvbrcTaxonomySearchStep",
        "bvbrc_search_input",
    ),
    (
        "apecx_integration.composition.steps.taxon_candidate_review_step",
        "TaxonCandidateReviewStep",
        "taxon_review_input",
    ),
)


def _clear_cache() -> None:
    """Test seam: drop the per-term verdict cache, in-flight locks, AND the built step singletons."""
    with _CACHE_LOCK:
        _LAST_RESORT_CACHE.clear()
        _INFLIGHT.clear()
    with _STEPS_LOCK:
        _STEPS.clear()


def _get_steps() -> list[tuple[Any, str]]:
    """Build (once) and return the three chain steps as ``(step, input_key)`` pairs.

    Each step is constructed via ``from_config({"name": ...})`` (dict config — no temp
    file), the framework-native construction path.

    Thread-safe (I7 hardening): concurrent fan-out could otherwise race the check-then-act on
    ``_STEPS`` — N threads each seeing it empty, each building+appending 3 → ``_STEPS`` holds N×3
    entries and the resolve loop re-runs the chain with wrong bundle shapes. The build is guarded
    by ``_STEPS_LOCK`` and assembled in a LOCAL list that is published to ``_STEPS`` ATOMICALLY
    only on FULL success, so a mid-build failure never leaves a partial ``_STEPS``.
    """
    steps = _STEPS
    if steps:  # fast path — fully-built list is published atomically, never seen partial
        return steps
    with _STEPS_LOCK:
        if _STEPS:  # another thread finished the build while we waited on the lock
            return _STEPS
        local: list[tuple[Any, str]] = []
        for module, cls, key in _CHAIN:
            step = getattr(importlib.import_module(module), cls).from_config(
                {"name": f"harmonized_last_resort_{cls}"}
            )
            local.append((step, key))
        # Publish atomically ONLY now that all three built cleanly (slice-assign is a single
        # GIL-atomic op — readers see either the empty list or the full 3, never a partial).
        _STEPS[:] = local
        return _STEPS


def _resolve_uncached(query: str, key: str) -> tuple[int | None, bool]:
    """Run preflight + the 3-step chain for ONE term. Returns ``(verdict, cacheable)``.

    ``cacheable`` is ``False`` for an ENVIRONMENTAL skip (LLM unreachable / unpulled, chain error,
    or overall-budget timeout) — those are not term-specific verdicts, so a later call retries when
    the condition clears. ``cacheable`` is ``True`` for a real verdict (a CDS-verified taxon_id, or
    ``None`` for a named / CDS-gate miss) that a single-attempt cache should remember for the term.
    """
    # Local imports: keep the resolver import-light and defer the LLM-config touch to call time
    # (the resolver module is imported lazily from the miss path only).
    from apecx_integration.agents._llm_config import llm_model_available, preflight_llm_model
    from apecx_integration.composition.steps.taxon_candidate_review_step import (
        _REVIEW_CACHE,
    )

    # LLM-availability gate (two parts; on ANY failure, skip SILENTLY to the raw fallback). NOT
    # cached (cacheable=False) — an unavailable LLM is an ENVIRONMENTAL condition (Ollama down /
    # model not pulled), not a term-specific verdict, so a later call retries when it comes up.
    #   1. preflight_llm_model() raises on a REACHABLE-but-unpulled model → loud operator guidance.
    #   2. preflight does NOT raise on an UNREACHABLE endpoint (offline dev is legitimate), so we
    #      ALSO require reachable+pulled — otherwise firing the chain would waste a BV-BRC
    #      round-trip in desktop/offline locus (there is no apecx-side LLM there).
    try:
        preflight_llm_model()
    except Exception as exc:  # noqa: BLE001 - optional LLM; degrade-loud, never raise
        log.warning(
            "llm_last_resort: LLM preflight failed (%s); skipping to raw fallback for %r",
            exc,
            query,
        )
        return None, False
    if not llm_model_available():
        log.info("llm_last_resort: no reachable+pulled LLM; skipping to raw fallback for %r", query)
        return None, False

    # Drive the real chain. The review step keys its OWN per-query verdict cache off the
    # normalized query, so clear that entry too, keeping this driver's single-attempt bound the
    # single source of truth for THIS term (avoids a stale cross-run review verdict).
    _REVIEW_CACHE.pop(key, None)
    started = time.monotonic()
    try:
        bundle: dict[str, Any] = {"query": query}
        for step, input_key in _get_steps():
            # Coarse overall budget: refuse to START a further step once the stack blew the
            # ceiling (per-step timeouts bound the in-step cost). Degrade to raw, do not cache.
            if time.monotonic() - started > _OVERALL_BUDGET_S:
                log.warning(
                    "llm_last_resort: overall budget %.0fs exceeded; abandoning %r -> raw fallback",
                    _OVERALL_BUDGET_S,
                    query,
                )
                return None, False
            bundle = asyncio.run(step.process({input_key: bundle}))
    except Exception as exc:  # noqa: BLE001 - the fallback must never break the deterministic path
        log.warning(
            "llm_last_resort: resolution chain error (%s); skipping to raw fallback for %r",
            exc,
            query,
        )
        return None, False

    taxon_id = bundle.get("taxon_id")
    resolved = (
        isinstance(taxon_id, int)
        and bundle.get("resolution_status") == "llm_fallback"
        and "NCBITaxon" in str(bundle.get("canonical_iri", ""))
    )
    verdict: int | None = taxon_id if resolved else None
    if verdict is not None:
        log.info("llm_last_resort: resolved %r -> taxon_id=%d", query, verdict)
    else:
        log.info("llm_last_resort: %r did not resolve to a CDS-verified taxon (named miss)", query)
    return verdict, True


def resolve_taxon_last_resort(query: str) -> int | None:
    """Drive the existing 3-step LLM taxon-resolution chain for ONE miss term.

    Returns the CDS-gate-verified NCBI taxon_id, or ``None`` when the LLM is unavailable,
    the term does not resolve, or the pick fails the CDS gate. NEVER raises — the
    deterministic miss path must never be blocked or crashed by the optional LLM.

    Concurrency-safe (I7 hardening): the ~9 destination indices fan out concurrently, so up to ~9
    threads can call this for the SAME term at once. A per-term in-flight lock (``_INFLIGHT``)
    collapses them to ONE actual resolution + cache hits for the rest, and all cache mutation is
    serialized under ``_CACHE_LOCK``.
    """
    key = (query or "").strip().lower()
    if not key:
        return None

    # Bounded single attempt: a cache hit short-circuits BEFORE preflight, any LLM call, or the
    # in-flight lock. Serialized so a concurrent BoundedDict del+set+evict cannot be seen mid-write.
    with _CACHE_LOCK:
        if key in _LAST_RESORT_CACHE:
            return _LAST_RESORT_CACHE[key]
        term_lock = _INFLIGHT.get(key)
        if term_lock is None:
            term_lock = threading.Lock()
            _INFLIGHT[key] = term_lock

    # Serialize concurrent same-term callers: the first resolves + caches, the rest fall through to
    # the re-check below and return the cached verdict without re-running the LLM/BV-BRC chain.
    with term_lock:
        with _CACHE_LOCK:
            if key in _LAST_RESORT_CACHE:
                _INFLIGHT.pop(key, None)
                return _LAST_RESORT_CACHE[key]

        verdict, cacheable = _resolve_uncached(query, key)

        with _CACHE_LOCK:
            if cacheable:
                _LAST_RESORT_CACHE[key] = verdict
            # Drop the in-flight entry: once cached, future callers short-circuit on the cache and
            # never reach here; for a non-cacheable (environmental) skip, dropping it lets a later
            # call retry. Waiters already holding this lock object keep their reference and, on
            # acquiring it, re-check the cache above — the drop cannot strand them.
            _INFLIGHT.pop(key, None)
        return verdict


__all__ = ["resolve_taxon_last_resort"]
