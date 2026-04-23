"""Cross-step DataUnit schemas for the violin_bvbrc T01 vertical slice.

These TypedDicts are the **producer/consumer contract** for every
DataUnit that crosses a step boundary in the T01 slice. They are
documentation, not runtime validation — Python's TypedDict is ignored
at runtime by design. The point is:

  1. A single grep-able place to see what each cross-step DataUnit
     carries.
  2. Static type-checker (mypy / pyright) coverage when the consuming
     code annotates with these types.
  3. Producer-side and consumer-side reference the same type, so a
     contract change shows up at both ends instead of drifting.

T01 vertical slice (per workflow_spec.md §3.1, with Steps 5 + 6
deferred per next_tasks_2026_04_22.md Task 4):

    Step 1 (entity_extraction)
        ──> Step1Output   (entities + query_terms)
        ──> Step 3a (synonym_cache_lookup)
            ──> Step3aOutput  (cached_mappings + novel_terms)
            ──> Step 3c (synonym_llm_proposals)
                ──> Step3cOutput  (llm_proposals)
                ──> Step 4 (synonym_approval_gate, ApprovalStep passthrough)
                    ──> Step4Output  (llm_proposals + reviewer modifications)
                    ──> Step 4p (verified_synonym_writeback)
                        ──> Step4pOutput  (written + already_existed)
                        ──> Step 7 (result_ranking)

Why Step 2 (bvbrc_snapshot_match) is NOT in the T01 slice
---------------------------------------------------------
Per workflow_spec.md §3.1, Step 2 is on a parallel branch that joins
the synonym chain at Step 6 (genomic_annotation). Since T01 defers
both Step 5 and Step 6 (enrichment), Step 2's only consumer is also
deferred — including it in T01 would be dead work, and the chosen
nanobrain step (`EnhancedBVBRCDataAcquisitionStep`) has an input
contract designed for downstream-of-HITL synonyms, not raw entity
candidates. Surfaced and resolved in this branch's commit.
"""

from __future__ import annotations

from typing import TypedDict

# ---------------------------------------------------------------------------
# Atomic types (re-used across step shapes)
# ---------------------------------------------------------------------------

class EntityCandidate(TypedDict):
    """One LLM-extracted entity candidate from Step 1."""

    name: str
    type: str  # 'pathogen', 'vaccine', 'gene', 'genome', 'disease', 'medical_term'
    confidence: float  # [0.5, 1.0] after the wrapped function's filter


class LLMSynonymProposal(TypedDict):
    """One Step 3c LLM proposal."""

    query_entity: str
    synonym: str
    score: float


class ApprovedMapping(TypedDict, total=False):
    """One mapping accepted by the human reviewer at Step 4. Used as
    Step 4p's canonical input shape. ``total=False`` because optional
    metadata fields (source_run_id, comment) may be absent.
    """

    query_term: str
    canonical_term: str
    confidence: float
    source_run_id: str
    comment: str


# ---------------------------------------------------------------------------
# Per-step output shapes (cross-boundary DataUnit contents)
# ---------------------------------------------------------------------------

class Step1Output(TypedDict):
    """EntityExtractionStep output. Carries BOTH the rich entity dicts
    and the bare query-term names so downstream steps can pick what
    they need without re-flattening:

    - ``entities``: full LLM extraction results, used by Step 2 (when
      it's wired) and by any downstream consumer that wants confidence
      / type metadata.
    - ``query_terms``: just the names (``[e["name"] for e in entities]``).
      The cache-lookup chain (Step 3a) takes only names; this avoids a
      transform-link between Step 1 and Step 3a.
    """

    entities: list[EntityCandidate]
    query_terms: list[str]


class Step3aOutput(TypedDict):
    """SynonymCacheLookupStep output."""

    cached_mappings: dict[str, str]  # query_term -> canonical_term
    novel_terms: list[str]            # query_terms that missed the cache


class Step3cOutput(TypedDict):
    """SynonymLLMProposalsStep output."""

    llm_proposals: list[LLMSynonymProposal]


class Step4Output(TypedDict, total=False):
    """ApprovalStep output. Generic — the framework shallow-merges
    reviewer modifications into the input, so the output dict may
    carry any subset of the input keys plus reviewer-supplied keys.

    For T01 we expect ``llm_proposals`` to pass through (with the
    HARD-gate's APPROVED decision implying every proposal is accepted
    as-is). On APPROVED_WITH_MODIFICATIONS, the modifications dict
    REPLACES the proposed synonyms — the workflow_spec.md §3.2
    payload format documents the shape.
    """

    llm_proposals: list[LLMSynonymProposal]


class Step4pOutput(TypedDict):
    """VerifiedSynonymWritebackStep output."""

    written: list[str]            # synonym IDs returned by the Control Plane
    already_existed: list[str]    # query_terms that 409'd (race-with-concurrent-run)


# ---------------------------------------------------------------------------
# Final-output shape (Step 7 result)
# ---------------------------------------------------------------------------

class T01FinalOutput(TypedDict, total=False):
    """ResultCollectionStep output for the T01 slice. The
    ResultCollectionStep is generic; this TypedDict captures the
    minimum shape T01's integration test asserts on.
    """

    written: list[str]
    already_existed: list[str]
    cached_mappings: dict[str, str]


__all__ = [
    "ApprovedMapping",
    "EntityCandidate",
    "LLMSynonymProposal",
    "Step1Output",
    "Step3aOutput",
    "Step3cOutput",
    "Step4Output",
    "Step4pOutput",
    "T01FinalOutput",
]
