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
#: The SINGLE rank+cap: hand out AT MOST this many hybrid-ranked records, every locus.
#: "15 MAX" is the product spec — the adaptive gate (:meth:`_hybrid_rank`) returns FEWER
#: when fewer records are strongly relevant. The tool (``literature_qa``) relies on THIS
#: as the one cap — it no longer pre-caps.
_TOP_K = 15
#: Relevance-gate cutoff (cycle-2 lever b): after sorting by the IDF keyword score, keep
#: records whose score is at least this FRACTION of the top record's score; drop the
#: low-relevance tail. Chosen over an elbow/largest-gap rule because a fraction-of-top is
#: scale-free, has no special-casing for 1-2 record sets, and is predictable to reason about.
_GATE_FRACTION = 0.4
#: Recall floor: never return fewer than this many usable records (capped by availability),
#: even when the gate would cut harder — protects against an over-aggressive cut.
_GATE_MIN_KEEP = 3
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
          * TIEBREAK — embedding cosine (:meth:`_embed_sims`). Degrades LOUD to zeros when
            sentence-transformers cannot load, collapsing the order to keyword-only.

        Gate (lever b): keep records scoring at least ``_GATE_FRACTION`` of the top keyword
        score, always between ``min(_GATE_MIN_KEEP, n)`` and ``k`` records — so a question
        with few strongly-relevant records yields FEWER than ``k``, never a padded ``k``.
        If NO facet matches any record (all keyword scores 0), fall back to embedding-cosine
        top-``k``. Synchronous + CPU-bound — the caller runs it via ``asyncio.to_thread``.
        """
        usable = [r for r in records if self._record_text(r)]
        if not usable:
            return []

        facets = extract_question_facets(question)
        present_per_record = [facets_present(facets, self._record_text(r)) for r in usable]
        idf = self._facet_idf(facets, present_per_record)
        kw_scores = [sum(idf[f] for f in present) for present in present_per_record]

        try:
            sims = self._embed_sims(question, usable)
        except Exception as exc:  # noqa: BLE001 — degrade-loud, never dump the full unranked set
            self.nb_logger.warning(
                "literature_rag LiteratureRagStep: embedding model unavailable (%s: %s); "
                "ranking by IDF keyword overlap ONLY — still gated + capped at k=%d, "
                "NOT the full set",
                type(exc).__name__,
                exc,
                k,
            )
            sims = [0.0] * len(usable)

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

        # Sort by (IDF keyword score DESC, cosine DESC). Stable sort keeps input order for
        # full ties, so repeated runs on identical candidates are deterministic.
        order = sorted(range(len(usable)), key=lambda i: (kw_scores[i], sims[i]), reverse=True)

        # Adaptive relevance gate: keep the high-relevance head, drop the diluting tail.
        top_score = kw_scores[order[0]]
        threshold = _GATE_FRACTION * top_score
        kept = [i for i in order if kw_scores[i] >= threshold]
        floor = min(_GATE_MIN_KEEP, len(usable))
        if len(kept) < floor:
            kept = order[:floor]
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
