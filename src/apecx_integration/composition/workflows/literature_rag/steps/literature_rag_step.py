"""LiteratureRagStep — turn ontology-filtered papers into a grounded, cited answer.

Second (and final) step of the literature_rag workflow. Given a scientist
``question`` plus a set of ``filtered_records`` (each ``{pmid, title, abstract}``,
and — for the open-access subset — ``full_text``; synthesis grounds on ``full_text``
when present via :func:`_evidence_text`, since abstracts omit epitope/animal-model detail),
this step:

  1. Ranks the records with a HYBRID signal and keeps AT MOST ``_TOP_K`` (=15):
     primary key = an IDF-weighted keyword score — the sum over the DISTINCT question
     facets each record mentions of each facet's rarity weight across the candidate set
     (:meth:`_facet_idf` over :func:`agents.literature.facets.facets_present`), so a rare
     discriminative facet counts far more than a generic one; tiebreak = sentence-
     transformers ``all-mpnet-base-v2`` cosine similarity (:meth:`_embed_sims`). An
     adaptive relevance gate then drops the low-relevance tail, so a question with few
     strongly-relevant records yields FEWER than 15 (never a padded 15). This rank+gate+cap
     is the step's SINGLE authority and runs in EVERY locus (desktop included). If
     sentence-transformers cannot load, the ranker degrades LOUD to keyword-only — still
     gated + capped, never the full set.
  2. Feeds those records to the SHARED synthesizer
     ``apecx_integration.agents.rag_synthesis.synthesize_response`` and returns its
     Markdown answer with the cited PMIDs parsed out.

Design note — why records go in as ``rag_chunks``, not ``publications``
----------------------------------------------------------------------
The shipped ``synthesize_response`` cites *publications* by DOI only: its
``_render_publications`` renderer REQUIRES a ``doi`` literal (``10.<id>/...``) and
its citation-against-inputs gate grounds publication citations on that DOI (see
``synthesizer._render_publications`` + ``agents/rag_synthesis/harvester_adapter.py``).
There is NO literal-PMID citation path. Our input contract is ``{pmid, title,
abstract}`` — PMID-only, no DOI — so passing these as ``publications=`` would RAISE
in the synthesizer's strict input validation.

So we reuse the SAME ``synthesize_response`` and the SAME citation-against-inputs
grounding gate AS-IS, via the ``rag_chunks`` seam instead: each retrieved record
becomes a RAG chunk whose ``text`` is its title+abstract. The synthesizer cites
``[RAG chunk #N]`` (a token its ``_render_rag_chunks`` renderer authorizes and its
grounding gate validates against the provided chunks — nothing hallucinated passes).
We then RELABEL each grounded ``[RAG chunk #N]`` back to ``[PMID:<pmid>]`` in the
answer and return those PMIDs. Because the gate already guaranteed every cited chunk
is one we supplied, every returned PMID provably traces to the provided evidence —
exactly the grounding guarantee a literature RAG owes its reader.

Degrade-loud contract
---------------------
  - No ``filtered_records`` → ``status="no_evidence"``, empty answer, NO LLM call.
  - Missing/empty ``question`` → ``ValueError`` (a wiring bug, not degradable data).
  - Synthesis fails (unreachable LLM, or a synthesizer gate such as the
    citation/grounding gate raises) → caught, logged LOUD, and converted to a
    well-formed ``status="synthesis_failed"`` envelope (empty answer/citations).
    The step never emits a fake "ok" — a non-"ok" status is the honest signal.

Framework contract (nanobrain):
  - Created via ``from_config`` only (never a direct constructor).
  - Implements ``process``; NEVER overrides ``execute``.
  - Owns its input/output data units + trigger (declared in the sibling YAML).
  - Single output data unit (``rag_output``); the whole returned dict is written
    there via the framework's single-output fallback.
"""

from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

from apecx_integration.agents.literature.facets import (
    extract_question_facets,
    facets_present,
)
from apecx_integration.agents.rag_synthesis import synthesize_response

#: Sibling synthesis config — same schema as the bundled default, with a
#: literature-tuned system_prompt that spells out the exact [RAG chunk #N]
#: citation token (prompts live in YAML, not in this .py — framework rule).
_SYNTHESIS_CONFIG_PATH = Path(__file__).resolve().parent / "synthesis_config.yml"

#: Sentence-transformers model used for the embedding tiebreak. Cached after first use.
_EMBED_MODEL_NAME = "all-mpnet-base-v2"
#: Cross-encoder model for the cycle-5 JOINT (question, doc) re-rank — the strongest
#: relevance stage. Cached after first use; override via the env var below.
_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_CROSS_ENCODER_MODEL_ENV = "APECX_CROSS_ENCODER_MODEL"
#: The CE re-rank is OPT-IN. Auto-activating a heavy component on mere library presence is a
#: silent-behavior anti-pattern (it changes ranking for everyone the moment sentence-transformers
#: is installed), so the CE stage runs ONLY when this flag is truthy ('1'/'true'). Default OFF ->
#: the pipeline is the pre-CE behavior (coverage -> bi-encoder bar -> coverage-only).
_CROSS_ENCODER_ENABLE_ENV = "APECX_ENABLE_CROSS_ENCODER"
#: The SINGLE rank+cap: hand out AT MOST this many hybrid-ranked records, every locus.
#: "15 MAX" is the product spec — the adaptive gate (:meth:`_hybrid_rank`) returns FEWER
#: when fewer records are strongly relevant. The tool (``literature_qa``) relies on THIS
#: as the one cap — it no longer pre-caps.
_TOP_K = 15
#: Discriminative-COVERAGE gate cutoff (supersedes the rejected fraction-of-SUM gate AND the
#: peak-only gate that under-trimmed): the SAME scale-free fraction does double duty, each use a
#: ratio WITHIN the candidate set so neither flips with N / log N. (1) A facet is DISCRIMINATIVE
#: when its IDF is >= this fraction of the rarest facet's IDF — so saturated common facets (low
#: IDF) are excluded while genuinely rare facets are kept. (2) After ranking by discriminative
#: COVERAGE (the IDF mass of the discriminative facets a record matches), keep a record only if
#: its coverage is >= this fraction of the best coverage in the set. Gating COVERAGE (not a
#: single facet's peak) is the fix for the peak gate's under-trim: when several facets are near-
#: equally rare, every record matching any ONE had peak == best peak and so survived to the cap;
#: coverage cuts the single-facet records once richer-coverage records exist, so the gate returns
#: FEWER than k when the discriminative signal is exhausted. A lone rarest-facet match still
#: clears it (its coverage == the best when nothing covers more) and common-only piles never do
#: (coverage 0 — no discriminative facet matched).
_GATE_KEEP_FRACTION = 0.5
#: Recall floor: never return fewer than this many usable records (capped by availability),
#: even when the gate would cut harder — protects against an over-aggressive cut.
_GATE_MIN_KEEP = 3
#: Semantic gate cutoff (cycle-3): AFTER the discriminative-coverage gate, keep a record only if
#: its question-cosine is >= this fraction of the BEST cosine among the COVERAGE-SURVIVING records
#: — a within-set RELATIVE bar (scale-stable, like ``_GATE_KEEP_FRACTION``, so it never flips with
#: the absolute cosine scale). This elevates the embedding signal from a mere sort tiebreak to a
#: GATE COMPONENT: a record that matches the question's rare keywords but sits as a semantic
#: outlier (low cosine) is trimmed even though it cleared coverage, so the COMBINED gate can
#: return FEWER records than coverage alone. The bar is SKIPPED (gate degrades to coverage-only)
#: when the embedding model is unavailable or carries no positive signal — degrade-loud, never
#: crash; the recall floor (``_GATE_MIN_KEEP``) still applies after it.
_GATE_SEM_FRACTION = 0.5
#: Cross-encoder re-ranker cutoff (cycle-5): AFTER the discriminative-coverage gate, a CROSS-
#: ENCODER (scores the (question, doc) pair JOINTLY, unlike the separately-embedded bi-encoder)
#: re-ranks the coverage survivors and REPLACES the bi-encoder semantic bar when available (the
#: stronger signal; running both double-trims). Keep a record only if its sigmoid-normalised CE
#: score is >= this ABSOLUTE value — i.e. the cross-encoder positively judges the (question, doc)
#: pair relevant (sigmoid >= 0.5 <=> raw logit >= 0). An absolute bar (not the former relative-to-
#: best fraction) is the fix for the SATURATION failure: when every survivor scores similarly high
#: the relative bar (>= fraction * best) trimmed nothing and the CE had no effect; the absolute bar
#: still cuts any survivor the CE judges NOT relevant, even one close to the best. Degrades down the
#: ladder (CE -> bi-encoder bar -> coverage-only) when the CrossEncoder cannot load; the recall
#: floor still applies after the bar.
_GATE_CE_ABS = 0.5
#: Matches the synthesizer's own inline RAG-chunk citation token shape.
_RAG_CHUNK_CITE = re.compile(r"\[RAG chunk #(\d+)\]")

#: Per-paper char caps on the evidence text (full text when open-access, else abstract).
#: Desktop hands evidence to a large-context frontier host, so it can carry generous full
#: text; the backend LLM is often a small local model with a tight context + a size gate,
#: so its chunk text is capped tighter. Abstracts are short and unaffected by either cap.
_FULLTEXT_DESKTOP_CAP = 12_000
_FULLTEXT_BACKEND_CAP = 4_000


def _evidence_text(record: dict[str, Any], *, char_cap: int) -> str:
    """The richest grounding text for a record: open-access ``full_text`` (capped) when
    present, else the ``abstract``. Abstracts omit the epitope-residue / animal-model detail
    specialized questions need; the full-text body carries it (fetched upstream by the
    literature pipeline), so synthesis grounds on it when available."""
    full_text = str(record.get("full_text") or "").strip()
    if full_text:
        return full_text[:char_cap]
    return str(record.get("abstract") or "").strip()


def _sigmoid(x: float) -> float:
    """Numerically-stable logistic map of an unbounded cross-encoder logit into (0, 1).

    Raw cross-encoder outputs are unbounded logits (can be negative); the sigmoid gives a
    well-behaved 0-1 relevance so the absolute CE bar (``_GATE_CE_ABS`` = 0.5) never mis-handles
    negatives (a negative logit maps into (0, 0.5) <-> NOT relevant, a positive into (0.5, 1) <->
    relevant, zero to exactly 0.5 — the bar's own cut point). The
    branch avoids ``math.exp`` overflow on large-magnitude logits — degrade-loud: never crash.
    """
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _cross_encoder_enabled() -> bool:
    """Whether the opt-in cross-encoder re-rank stage is ON (``APECX_ENABLE_CROSS_ENCODER``).

    Default OFF. Auto-activating a heavy component on mere library presence silently changes the
    ranking the moment sentence-transformers is installed — an anti-pattern. ON only for an
    explicit truthy flag (``'1'`` / ``'true'``, case-insensitive); anything else (unset / ``'0'``
    / ``'false'``) keeps the pre-CE pipeline (coverage -> bi-encoder bar -> coverage-only).
    """
    import os

    return os.environ.get(_CROSS_ENCODER_ENABLE_ENV, "").strip().lower() in {"1", "true"}


class LiteratureRagStep(BaseStep):
    """Literature RAG answerer — retrieve top-k records, synthesize a cited answer."""

    COMPONENT_TYPE: str = "literature_rag_step"
    # This step is the FINAL narrative synthesis of the literature_rag pipeline: in
    # desktop locus it omits the apecx LLM call and the host (Claude Desktop)
    # synthesizes from the handed-over evidence (CLAUDE.md "Two operating modes").
    # The run-time requires_llm gate reads this so it does NOT refuse the workflow on
    # a desktop with no apecx LLM configured — without it the gate brands the step
    # ``in_dag`` and a desktop user with no Ollama is wrongly refused the tool.
    LLM_ROLE: str = "final_synthesis"

    #: The step's single input data-unit name (see sibling YAML).
    _INPUT_UNIT: str = "rag_input"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    def _unwrap_envelope(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return the RAG envelope.

        The workflow cascade delivers ``{"rag_input": <envelope>}`` (keyed by the
        input data-unit name), whereas a direct ``process(<envelope>)`` call passes
        the envelope flat. Accept both (mirrors ``OntologyFilterStep._unwrap_envelope``).
        """
        if (
            isinstance(input_data, dict)
            and set(input_data) == {self._INPUT_UNIT}
            and isinstance(input_data[self._INPUT_UNIT], dict)
        ):
            return input_data[self._INPUT_UNIT]
        return input_data if isinstance(input_data, dict) else {}

    def _get_synthesis_config(self):
        """Lazily load + cache the literature-tuned SynthesisConfig from YAML.

        Loaded here (not module top) so a caller that only exercises the
        no-evidence degrade path never touches the config file.
        """
        cfg = getattr(self, "_synthesis_config", None)
        if cfg is None:
            import yaml

            from apecx_integration.agents.rag_synthesis import SynthesisConfig

            raw = yaml.safe_load(_SYNTHESIS_CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = SynthesisConfig.model_validate(raw)
            self._synthesis_config = cfg
        return cfg

    def _get_embed_model(self):
        """Lazily load + cache the sentence-transformers model on the instance.

        Heavy import kept INSIDE the method (never at module top) so the package
        imports without the ``rag`` extra installed. Import order matters on
        macOS-ARM: ``sentence_transformers`` BEFORE any faiss touch (CLAUDE.md).
        """
        model = getattr(self, "_embed_model", None)
        if model is None:
            from sentence_transformers import SentenceTransformer  # noqa: I001

            model = SentenceTransformer(_EMBED_MODEL_NAME)
            self._embed_model = model
        return model

    @staticmethod
    def _record_text(record: dict[str, Any]) -> str:
        """Title + abstract joined — the text both ranking signals read."""
        return (
            str(record.get("title") or "").strip()
            + "\n"
            + str(record.get("abstract") or "").strip()
        ).strip()

    def _embed_sims(self, question: str, records: list[dict[str, Any]]) -> list[float]:
        """Cosine similarity of each record (aligned to ``records``) to the question.

        Reuses the self-contained sentence-transformers embed+cosine (no faiss). RAISES
        if the embedding model cannot load — the caller (:meth:`_hybrid_rank`) catches
        that and degrades LOUD to keyword-only ranking.
        """
        model = self._get_embed_model()
        texts = [self._record_text(r) for r in records]
        doc_emb = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        q_emb = model.encode([question], normalize_embeddings=True, convert_to_numpy=True)[0]
        return [float(s) for s in (doc_emb @ q_emb)]

    def _get_cross_encoder(self):
        """Lazily load + cache the sentence-transformers ``CrossEncoder`` on the instance.

        Heavy import kept INSIDE the method (never at module top) so the package imports
        without the ``rag`` extra installed (clean-install rule). Model overridable via
        ``APECX_CROSS_ENCODER_MODEL``. RAISES (import/load failure) when the extra is absent
        — the caller degrades LOUD down the relevance ladder.
        """
        model = getattr(self, "_cross_encoder", None)
        if model is None:
            import os

            from sentence_transformers import CrossEncoder  # noqa: I001

            name = os.environ.get(_CROSS_ENCODER_MODEL_ENV, _CROSS_ENCODER_MODEL)
            model = CrossEncoder(name)
            self._cross_encoder = model
        return model

    def _cross_encoder_rerank(self, question: str, records: list[dict[str, Any]]) -> list[float]:
        """Sigmoid-normalised (0, 1) cross-encoder relevance of each record to the question.

        A cross-encoder scores the (question, doc) pair JOINTLY, so it separates near-
        duplicates the separately-embedded bi-encoder cannot. Raw outputs are unbounded logits;
        :func:`_sigmoid` maps them into (0, 1) so the absolute CE bar (``_GATE_CE_ABS``) is well-
        behaved and never mis-gates a negative logit. Aligned to ``records``. RAISES if the CrossEncoder
        cannot load — the caller (:meth:`_hybrid_rank`) catches it and degrades down the ladder.
        """
        model = self._get_cross_encoder()
        pairs = [[question, self._record_text(r)] for r in records]
        raw = model.predict(pairs)
        return [_sigmoid(float(s)) for s in raw]

    @staticmethod
    def _facet_idf(facets: list[str], present_per_record: list[list[str]]) -> dict[str, float]:
        """IDF weight of each facet over the CURRENT candidate set (cycle-2 lever a).

        ``df`` = number of candidate records whose text contains the facet; with
        ``N = len(candidates)``, ``idf = log((N+1)/(df+1)) + 1``. A facet present in few
        candidates (discriminative) outweighs one present in most (generic) — the rarity
        weighting the evaluation feedback asked for. No external corpus and no hand-picked
        terms: the weight is computed purely from the records handed to the ranker.
        """
        n = len(present_per_record)
        df: dict[str, int] = dict.fromkeys(facets, 0)
        for present in present_per_record:
            for f in present:  # already DISTINCT per record (facets_present dedups)
                df[f] += 1
        return {f: math.log((n + 1) / (df[f] + 1)) + 1.0 for f in facets}

    def _hybrid_rank(
        self, question: str, records: list[dict[str, Any]], k: int
    ) -> list[dict[str, Any]]:
        """Rank records by an IDF-weighted keyword score (tiebreak: embedding cosine),
        then cut the low-relevance tail with an adaptive gate; return AT MOST ``k``.

        The step's SINGLE rank+cap authority (cycle-2). Records with no usable text (empty
        title AND abstract) are dropped first. Signals:

          * PRIMARY — IDF keyword score: sum over the DISTINCT facets a record contains of
            each facet's :meth:`_facet_idf` rarity weight over the candidate set. A rare,
            discriminative facet contributes far more than a common one (lever a).
          * SEMANTIC — bi-encoder cosine (:meth:`_embed_sims`): the sort tiebreak, and the
            FALLBACK relevance bar (below). Degrades LOUD to zeros when sentence-transformers
            cannot load, collapsing the order to keyword-only.
          * CROSS-ENCODER — joint (question, doc) relevance (:meth:`_cross_encoder_rerank`,
            sigmoid-normalised): the STRONGEST stage, which re-ranks + gates the coverage
            survivors and REPLACES the bi-encoder bar when available.

        RELEVANCE gate on the coverage survivors — the STRONGEST available bar runs (a DEGRADE
        LADDER, each tier loud):

          1. Discriminative-coverage: a record is kept only if its discriminative COVERAGE — the
             IDF mass of the discriminative facets (IDF >= ``_GATE_KEEP_FRACTION`` of the rarest
             facet's) it matches — is at least ``_GATE_KEEP_FRACTION`` of the best coverage in the
             set. So a question with few strongly-relevant records yields FEWER than ``k`` (records
             carrying only ONE of several near-equally-rare facets are trimmed once richer-coverage
             records exist), never a padded ``k``, and a common-only pile cannot survive by
             stacking low-IDF facets.
          2. Cross-encoder (NEW, strongest): re-rank the coverage survivors by sigmoid-normalised
             joint CE score desc and keep those whose score clears the ABSOLUTE ``_GATE_CE_ABS``
             bar (the CE positively judges the pair relevant), so the combined gate returns FEWER
             than coverage alone AND still trims under saturation (all survivors similarly high) —
             the case the former relative-to-best bar could not cut. DEGRADE LADDER when the
             CrossEncoder cannot load: fall back to the
             bi-encoder semantic bar (keep cosine >= ``_GATE_SEM_FRACTION`` of the best surviving
             cosine); if the embedding model is also unavailable or carries no positive signal,
             degrade to coverage-only. Each degrade step warns LOUD; never crashes.

        The kept set is always between ``min(_GATE_MIN_KEEP, n)`` and ``k`` records: the recall
        floor applies AFTER both bars, so neither can cut below ``min(_GATE_MIN_KEEP, n)``. If NO
        facet matches any record (all keyword scores 0), fall back to embedding-cosine top-``k``.
        Synchronous + CPU-bound — the caller runs it via ``asyncio.to_thread``.
        """
        usable = [r for r in records if self._record_text(r)]
        if not usable:
            return []

        facets = extract_question_facets(question)
        present_per_record = [facets_present(facets, self._record_text(r)) for r in usable]
        idf = self._facet_idf(facets, present_per_record)
        kw_scores = [sum(idf[f] for f in present) for present in present_per_record]

        embed_available = True
        try:
            sims = self._embed_sims(question, usable)
        except Exception as exc:  # noqa: BLE001 — degrade-loud, never dump the full unranked set
            self.nb_logger.warning(
                "literature_rag LiteratureRagStep: embedding model unavailable (%s: %s); "
                "ranking by IDF keyword overlap ONLY — still gated + capped at k=%d, "
                "NOT the full set; semantic gate SKIPPED (coverage-only)",
                type(exc).__name__,
                exc,
                k,
            )
            sims = [0.0] * len(usable)
            embed_available = False

        # No facet matched any candidate -> keyword signal is empty; fall back to the
        # embedding-cosine top-k (lever b constraint), still capped, never the full set.
        if not any(score > 0.0 for score in kw_scores):
            self.nb_logger.warning(
                "literature_rag LiteratureRagStep: no question facet matched any of %d "
                "candidate record(s); falling back to embedding-cosine top-%d ranking",
                len(usable),
                k,
            )
            emb_order = sorted(range(len(usable)), key=lambda i: sims[i], reverse=True)
            return [usable[i] for i in emb_order[:k]]

        # Discriminative facets = those whose IDF is within _GATE_KEEP_FRACTION of the rarest
        # facet's IDF (a ratio WITHIN the set -> scale-free). A record's discriminative COVERAGE
        # is the IDF mass of the discriminative facets it matches. Ranking by coverage (IDF-sum
        # then cosine as tiebreaks) puts records covering MORE of the question's rare content
        # above single-rare-facet matches; stable sort keeps input order on full ties.
        max_idf = max(idf.values())
        disc_facets = {f for f, w in idf.items() if w >= _GATE_KEEP_FRACTION * max_idf}
        disc_cov = [
            sum(idf[f] for f in present if f in disc_facets) for present in present_per_record
        ]
        order = sorted(
            range(len(usable)),
            key=lambda i: (disc_cov[i], kw_scores[i], sims[i]),
            reverse=True,
        )

        # Discriminative-coverage gate: keep records whose coverage is at least
        # _GATE_KEEP_FRACTION of the best coverage. Gating the coverage RATIO (not a single
        # facet's peak) trims single-rare-facet records once richer-coverage records exist, so
        # the set shrinks below k when the discriminative signal is exhausted; a lone rarest
        # match still clears it and common-only piles (coverage 0) never do.
        best_cov = disc_cov[order[0]]
        threshold = _GATE_KEEP_FRACTION * best_cov
        kept = [i for i in order if disc_cov[i] >= threshold]

        # Cross-encoder re-ranker (cycle-5) — the STRONGEST relevance stage. A cross-encoder
        # scores the (question, doc) pair JOINTLY, separating near-duplicates the separately-
        # embedded bi-encoder cannot. It runs on the COVERAGE-gate survivors and REPLACES the
        # bi-encoder semantic bar when available (running both double-trims). Re-sort survivors by
        # the sigmoid-normalised CE score desc (ties -> prior coverage order, deterministic), then
        # keep only those clearing the ABSOLUTE bar (_GATE_CE_ABS: sigmoid >= 0.5, the CE positively
        # judges the pair relevant). The absolute bar replaces the former relative-to-best fraction,
        # which could not trim under saturation (all survivors similarly high -> fraction*best kept
        # everything -> CE had no effect); the absolute bar still cuts a survivor the CE judges NOT
        # relevant even when it is close to the best. DEGRADE LADDER, each tier loud: CE unavailable
        # -> bi-encoder semantic bar (cycle-4) -> embeddings unavailable -> coverage-only. The recall
        # floor applies after whichever bar ran, padding in that bar's own sort order.
        # pad_source is the recall-floor refill order — it MUST span ALL usable records (not just
        # the gate survivors) in EVERY branch, so min(_GATE_MIN_KEEP, n) is always reachable even
        # when the coverage gate already cut below the floor. Default = coverage order over all
        # usable; the CE path rebuilds it as CE-sorted survivors first, then the rest in coverage
        # order (same full set, just re-prioritised) so the CE path never under-recalls its own
        # degraded fallback.
        pad_source = order
        ce_applied = False
        if _cross_encoder_enabled() and kept:
            try:
                ce_scores = self._cross_encoder_rerank(question, [usable[i] for i in kept])
            except Exception as exc:  # noqa: BLE001 — degrade down the ladder, never crash
                self.nb_logger.warning(
                    "literature_rag LiteratureRagStep: cross-encoder re-ranker unavailable "
                    "(%s: %s); degrading to the bi-encoder semantic bar (coverage-only if "
                    "embeddings are also unavailable)",
                    type(exc).__name__,
                    exc,
                )
            else:
                ce_by_index = dict(zip(kept, ce_scores, strict=True))
                prior_rank = {idx: rank for rank, idx in enumerate(kept)}
                ce_sorted = sorted(kept, key=lambda i: (-ce_by_index[i], prior_rank[i]))
                # Floor refills from the FULL usable set: CE-sorted survivors, then the records
                # the coverage gate already cut, in coverage order — so the floor always meets
                # min(_GATE_MIN_KEEP, n), never only the survivor count.
                survivor_set = set(ce_sorted)
                pad_source = ce_sorted + [i for i in order if i not in survivor_set]
                kept = [i for i in ce_sorted if ce_by_index[i] >= _GATE_CE_ABS]
                ce_applied = True

        # Bi-encoder semantic bar (cycle-3/4) — now the FIRST degrade tier: runs ONLY when the
        # cross-encoder is unavailable (skipped when CE applied, to avoid double-trimming). Still
        # skipped when the embedding model is unavailable OR the best surviving cosine is non-
        # positive (-> coverage-only). Among the survivors keep those whose question-cosine clears
        # _GATE_SEM_FRACTION of the best surviving cosine — a within-set RELATIVE bar. Preserves
        # rank order; never crashes (the keyword-only / all-zero paths warned above).
        if not ce_applied and embed_available and kept:
            best_sim = max(sims[i] for i in kept)
            if best_sim > 0.0:
                sem_threshold = _GATE_SEM_FRACTION * best_sim
                kept = [i for i in kept if sims[i] >= sem_threshold]

        floor = min(_GATE_MIN_KEEP, len(usable))
        if len(kept) < floor:
            kept = pad_source[:floor]
        return [usable[i] for i in kept[:k]]

    @staticmethod
    def _render_host_evidence(question: str, records: list[dict[str, Any]]) -> str:
        """Deterministic evidence body handed to the host LLM in desktop locus.

        Mirrors RagSynthesisStep's desktop inversion (hand the FULL deterministic
        evidence, not an empty 'defer to host' scaffold): the connected assistant
        writes the cited narrative from these retrieved, ontology-filtered records.
        """
        lines = [
            f"# Literature evidence for: {question}",
            "",
            "The connected assistant synthesizes the answer from the retrieved, "
            "ontology-filtered PubMed records below. Cite each claim with its "
            "``[PMID:<id>]``.",
            "",
        ]
        for rec in records:
            pmid = str(rec.get("pmid") or "").strip()
            title = str(rec.get("title") or "").strip()
            evidence = _evidence_text(rec, char_cap=_FULLTEXT_DESKTOP_CAP)
            lines.append(f"## [PMID:{pmid}] {title}".rstrip() if pmid else f"## {title}".rstrip())
            if str(rec.get("full_text") or "").strip():
                # Name the evidence tier so the host knows this is body text, not just the
                # abstract (and that it may be truncated to the per-paper cap).
                lines.append("_(open-access full text — may be truncated)_")
            if evidence:
                lines.append(evidence)
            lines.append("")
        return "\n".join(lines).strip()

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        envelope = self._unwrap_envelope(input_data)
        filtered_records = envelope.get("filtered_records") or []

        # Degrade-loud: no evidence -> no LLM call, well-formed empty envelope.
        if not filtered_records:
            self.nb_logger.warning(
                "literature_rag LiteratureRagStep: no filtered_records — "
                "cannot ground an answer; returning status=no_evidence (no LLM call)"
            )
            return {"answer": "", "citations": [], "status": "no_evidence"}

        question = envelope.get("question") or envelope.get("query")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(
                f"LiteratureRagStep '{self.name}': envelope must carry a non-empty "
                f"'question' string; got {type(question).__name__}={question!r}"
            )
        question = question.strip()

        # SINGLE rank+gate+cap authority — runs in EVERY locus (desktop included). Hybrid:
        # primary key = IDF-weighted keyword score, tiebreak = embedding cosine; an adaptive
        # relevance gate then drops the low-relevance tail (so fewer than _TOP_K come back
        # when few records are strongly relevant), capped at _TOP_K. Degrades LOUD to
        # keyword-only if sentence-transformers can't load — never the full unranked set.
        # CPU-bound; offloaded so the loop stays free.
        ranked = await asyncio.to_thread(self._hybrid_rank, question, filtered_records, _TOP_K)
        if not ranked:
            self.nb_logger.warning(
                "literature_rag LiteratureRagStep: %d filtered_records but none carry "
                "usable text (title/abstract); returning status=no_evidence",
                len(filtered_records),
            )
            return {"answer": "", "citations": [], "status": "no_evidence"}

        # Desktop locus: the connected host LLM is the synthesizer (CLAUDE.md "Two
        # operating modes" + LLM_ROLE). Hand it the ranked, capped ontology-filtered
        # evidence + PMIDs (at most _TOP_K — NOT the full set) and let it write the
        # narrative. The requires_llm gate reads LLM_ROLE so the tool is NOT refused on
        # a desktop with no Ollama.
        from apecx_integration.composition.runtime.execution_locus import (
            ExecutionLocus,
            get_active_locus,
        )

        if get_active_locus() == ExecutionLocus.DESKTOP:
            citations = sorted({str(r.get("pmid")).strip() for r in ranked if r.get("pmid")})
            self.nb_logger.info(
                "literature_rag LiteratureRagStep: desktop locus — handing %d "
                "hybrid-ranked record(s) to the host for synthesis (no apecx LLM)",
                len(ranked),
            )
            return {
                "answer": self._render_host_evidence(question, ranked),
                "citations": citations,
                "status": "host_synthesizes",
            }

        # Build RAG chunks from the ranked set. The synthesizer numbers surviving chunks
        # 1..N in the order given (its renderer also drops empty-text chunks; we pre-dropped
        # them in _hybrid_rank so index_to_pmid stays aligned with the synthesizer's #N).
        chunks: list[dict[str, Any]] = []
        index_to_pmid: dict[int, str] = {}
        for i, rec in enumerate(ranked, start=1):
            title = str(rec.get("title") or "").strip()
            # Ground on open-access full text (capped tighter than desktop — the backend LLM
            # is often a small local model with a tight context + a size gate) when present,
            # else the abstract.
            evidence = _evidence_text(rec, char_cap=_FULLTEXT_BACKEND_CAP)
            pmid = str(rec.get("pmid") or "").strip()
            chunks.append(
                {
                    "text": (title + "\n" + evidence).strip(),
                    "id": pmid or None,
                    "source": f"PMID:{pmid}" if pmid else None,
                }
            )
            index_to_pmid[i] = pmid

        # 3. Synthesize. Build the LLM the SAME way every synthesis path does — the
        #    shared factory (APECX_LLM_* env) with a loud, early preflight — then
        #    reuse synthesize_response + its citation-against-inputs gate AS-IS.
        try:
            from apecx_integration.agents._llm_config import preflight_llm_model
            from apecx_integration.agents._llm_factory import build_chat_llm

            preflight_llm_model()
            llm = build_chat_llm()
            raw_answer = await asyncio.to_thread(
                synthesize_response,
                question,
                rag_chunks=chunks,
                llm=llm,
                config=self._get_synthesis_config(),
            )
        except Exception as exc:  # noqa: BLE001 — degrade-loud, never fake an "ok" answer
            self.nb_logger.warning(
                "literature_rag LiteratureRagStep: synthesis failed (%s: %s); "
                "returning status=synthesis_failed",
                type(exc).__name__,
                exc,
            )
            return {"answer": "", "citations": [], "status": "synthesis_failed"}

        # 4. Relabel grounded [RAG chunk #N] -> [PMID:<pmid>] and collect citations.
        #    The gate already guaranteed every cited #N is a chunk we supplied, so
        #    every relabeled PMID traces to the provided evidence.
        cited_pmids: list[str] = []

        def _relabel(match: re.Match) -> str:
            pmid = index_to_pmid.get(int(match.group(1)))
            if pmid:
                cited_pmids.append(pmid)
                return f"[PMID:{pmid}]"
            return match.group(0)

        answer = _RAG_CHUNK_CITE.sub(_relabel, raw_answer)
        citations = sorted(set(cited_pmids))
        self.nb_logger.info(
            "literature_rag LiteratureRagStep: synthesized %d-char answer over "
            "%d retrieved records; cited %d distinct PMID(s)",
            len(answer),
            len(ranked),
            len(citations),
        )
        return {"answer": answer, "citations": citations, "status": "ok"}
