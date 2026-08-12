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
from typing import Any

from apecx_integration._bounded_cache import BoundedDict

log = logging.getLogger(__name__)

# taxon_id (int) for a CDS-verified resolution, or None for a remembered miss / unresolvable
# term. FIFO-bounded so a long-lived MCP server fielding many distinct miss terms cannot grow
# without limit (the same leak class BoundedDict guards elsewhere).
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
    """Test seam: drop the per-term verdict cache AND the built step singletons."""
    _LAST_RESORT_CACHE.clear()
    _STEPS.clear()


def _get_steps() -> list[tuple[Any, str]]:
    """Build (once) and return the three chain steps as ``(step, input_key)`` pairs.

    Each step is constructed via ``from_config({"name": ...})`` (dict config — no temp
    file), the framework-native construction path.
    """
    if not _STEPS:
        for module, cls, key in _CHAIN:
            step = getattr(importlib.import_module(module), cls).from_config(
                {"name": f"harmonized_last_resort_{cls}"}
            )
            _STEPS.append((step, key))
    return _STEPS


def resolve_taxon_last_resort(query: str) -> int | None:
    """Drive the existing 3-step LLM taxon-resolution chain for ONE miss term.

    Returns the CDS-gate-verified NCBI taxon_id, or ``None`` when the LLM is unavailable,
    the term does not resolve, or the pick fails the CDS gate. NEVER raises — the
    deterministic miss path must never be blocked or crashed by the optional LLM.
    """
    # Local imports: keep the resolver import-light and defer the LLM-config touch to call time
    # (the resolver module is imported lazily from the miss path only).
    from apecx_integration.agents._llm_config import llm_model_available, preflight_llm_model
    from apecx_integration.composition.steps.taxon_candidate_review_step import (
        _REVIEW_CACHE,
    )

    key = (query or "").strip().lower()
    if not key:
        return None

    # Bounded single attempt: a cache hit short-circuits BEFORE preflight or any LLM call.
    if key in _LAST_RESORT_CACHE:
        return _LAST_RESORT_CACHE[key]

    # LLM-availability gate (two parts; on ANY failure, skip SILENTLY to the raw fallback). This is
    # deliberately NOT cached — an unavailable LLM is an ENVIRONMENTAL condition (Ollama down /
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
        return None
    if not llm_model_available():
        log.info("llm_last_resort: no reachable+pulled LLM; skipping to raw fallback for %r", query)
        return None

    # Drive the real chain. The review step keys its OWN per-query verdict cache off the
    # normalized query, so clear that entry too, keeping this driver's single-attempt bound the
    # single source of truth for THIS term (avoids a stale cross-run review verdict).
    _REVIEW_CACHE.pop(key, None)
    try:
        bundle: dict[str, Any] = {"query": query}
        for step, input_key in _get_steps():
            bundle = asyncio.run(step.process({input_key: bundle}))
    except Exception as exc:  # noqa: BLE001 - the fallback must never break the deterministic path
        log.warning(
            "llm_last_resort: resolution chain error (%s); skipping to raw fallback for %r",
            exc,
            query,
        )
        return None

    taxon_id = bundle.get("taxon_id")
    resolved = (
        isinstance(taxon_id, int)
        and bundle.get("resolution_status") == "llm_fallback"
        and "NCBITaxon" in str(bundle.get("canonical_iri", ""))
    )
    verdict: int | None = taxon_id if resolved else None
    _LAST_RESORT_CACHE[key] = verdict
    if verdict is not None:
        log.info("llm_last_resort: resolved %r -> taxon_id=%d", query, verdict)
    else:
        log.info("llm_last_resort: %r did not resolve to a CDS-verified taxon (named miss)", query)
    return verdict


__all__ = ["resolve_taxon_last_resort"]
