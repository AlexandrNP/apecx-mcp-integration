# I7 last-resort resolver — live-data integration verification (2026-08-12)

The bounded last-resort resolver (`resolve_taxon_last_resort`,
`src/apecx_integration/composition/steps/_llm_last_resort_resolver.py`) was implemented,
unit-tested, reviewed, and merged (`100e35c`) — but had **never been run end-to-end against
real infrastructure**. Unit tests mock the three driven steps, so they cannot expose the
three failure classes that only a live run reveals: (a) a valid-JSON-but-wrong-content LLM
answer, (b) an `asyncio.run`/event-loop interaction bug across the per-step drives, or (c) a
verification-gate false-negative against the real upstream catalog. This record closes that
gap.

## Run

- **Command** (non-destructive `PYTHONPATH` override — no venv reinstall, so the shared
  editable install other worktrees use was left untouched):
  ```
  PYTHONPATH="$(pwd)/src" .venv/bin/python <driver>
  ```
  Driver calls `resolve_taxon_last_resort(term)` for two terms that the deterministic
  dictionary + harmonized-search path is expected to miss.
- **Model**: `devstral:24b` (local Ollama at `localhost:11434`, confirmed reachable + pulled).
- **Upstream**: live BV-BRC taxonomy catalog (real network round-trips).
- **Worktree / commit**: `wt-i7-llm-lastresort` @ `100e35c`.
- **Timestamp**: 2026-08-12 17:16 local.

## Result — both terms resolved, CDS-gate-verified

| query term | resolved id | resolved name | genomes | cds | wall-clock |
|---|---|---|---|---|---|
| Powassan | **11083** | Powassan virus | 711 | 1092 | 39 s |
| Mayaro   | **59301** | Mayaro virus   | 264 | 706  | 8 s  |

Both ids are correct NCBI taxonomy identifiers. Both cleared the CDS verification gate
(non-zero `cds`), so each id is upstream-verified, not an unverified raw LLM guess.

## Full pipeline fired (from the run log)

For each term the log shows the complete real cascade, in order:
1. `TaxonSynonymGenerationStep` → LLM call to Ollama (`POST /v1/chat/completions 200 OK`),
   e.g. Powassan produced 7 synonyms.
2. `BvbrcTaxonomySearchStep` → live catalog search: `7 synonym(s) -> 4 distinct taxa ->
   4 candidate(s)` (top candidate `taxon_id=11083`, `cds=1092`).
3. `TaxonCandidateReviewStep` → second LLM call, selected `taxon_id=11083`.
4. Resolver returned `11083` and cached it.

No exception, no silently-empty result, exit code 0.

## What this validates

- **The last-resort tier works end-to-end on real inputs.** The one proof that mocked unit
  tests structurally cannot provide is now on the record: real LLM synonyms, real catalog
  hits, real CDS-gate acceptance, correct ids.
- **Degrade path not exercised here (by design).** Both probe terms exist in BV-BRC, so this
  run covers the *success* path. The abstain/degrade-loud path (term genuinely absent
  upstream → return `None` + diagnostic) is covered by the unit suite
  (`tests/unit/test_llm_last_resort_resolver.py`) and is a candidate for a future live probe
  with a deliberately-absent term.
- **Cold-start cost is real but bounded.** First term 39 s (model cold-load), second term
  8 s (warm). A last-resort tier is only reached after the deterministic path misses, so this
  latency is paid rarely.

## Static cross-check (companion to this live run)

An independent read-only cross-check of the shipped resolver against its six claimed
properties confirmed **all six**, with **no blocking bug** in the three feared classes:

- **asyncio.run in a running loop — safe.** The sole caller `_run_miss_envelope` is offloaded
  via `self.run_blocking(...)` (`harmonized_search_execute_step.py:854`), so
  `asyncio.run(step.process(...))` (`_llm_last_resort_resolver.py:197`) runs on a worker
  thread with no live loop.
- **bounded cache iterated-while-mutated — safe.** `BoundedDict.__setitem__` evicts via a
  fresh one-shot `next(iter(self))` per pass (`_bounded_cache.py:28-29`), never through a
  live iterator.
- **lock leak — none.** Every acquisition is a `with` block.

Confirmed properties (file:line in `_llm_last_resort_resolver.py` unless noted):
1. **Bounded cache** — `BoundedDict(maxsize=512)` (line 80); eviction removes oldest
   (`_bounded_cache.py:28-29`). Policy is FIFO-by-write-recency (a re-write refreshes the
   eviction slot), not strict insertion-order FIFO.
2. **Availability gate returns early** — `preflight_llm_model()` (168) + `llm_model_available()`
   (176) both `return None, False` before any upstream round-trip.
3. **Three concurrency locks** — `_STEPS_LOCK` (61, atomic step publish), per-term `_INFLIGHT`
   lock (67, collapses N same-term calls to 1 resolution + N-1 hits), `_CACHE_LOCK` (62,
   serializes cache mutations).
4. **CDS gate** — an id is yielded only when `resolution_status == "llm_fallback"` and
   `"NCBITaxon" in canonical_iri` (207-212), set only after the upstream CDS check passes
   (`taxon_candidate_review_step.py:274-279`); a failed gate caches `None`.
5. **Signature** — `resolve_taxon_last_resort(query: str) -> int | None` (220), sole export.
6. **Degrade-loud** — every failure path returns `None` + a diagnostic; the function never
   re-raises.

Two texture notes (neither a bug):
- The per-term "collapse to 1 resolution" bound is airtight only for **cacheable** verdicts;
  a non-cacheable environmental skip drops the in-flight entry so a later call may re-run —
  this is the intended "retry when the condition clears" behavior.
- The LLM-unavailable branch logs at **`log.info`** (line 177) while the reachable-but-unpulled
  branch logs at `log.warning` (170). Since unreachable is arguably the more severe condition,
  this is a minor degrade-loud inconsistency — see the improvements record.
