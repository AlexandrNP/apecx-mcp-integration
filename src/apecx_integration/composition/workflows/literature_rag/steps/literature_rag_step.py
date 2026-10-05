"""LiteratureRagStep — turn ontology-filtered papers into a grounded, cited answer.

Second (and final) step of the literature_rag workflow. Given a scientist
``question`` plus a set of ``filtered_records`` (each ``{pmid, title, abstract}``),
this step:

  1. Embeds the records + the question with sentence-transformers
     ``all-mpnet-base-v2`` and retrieves the top-``k`` most similar records
     (cosine, ``k=5``). Retrieval is a self-contained, minimal embed+top-k here
     (a sibling branch is adding a shared ``build_faiss_subindex``; this step does
     NOT depend on it — we consolidate later).
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
import re
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

from apecx_integration.agents.rag_synthesis import synthesize_response

#: Sibling synthesis config — same schema as the bundled default, with a
#: literature-tuned system_prompt that spells out the exact [RAG chunk #N]
#: citation token (prompts live in YAML, not in this .py — framework rule).
_SYNTHESIS_CONFIG_PATH = Path(__file__).resolve().parent / "synthesis_config.yml"

#: Sentence-transformers model used for retrieval. Cached locally after first use.
_EMBED_MODEL_NAME = "all-mpnet-base-v2"
#: Number of records retrieved for the question (<= synthesis_config.max_rag_chunks=8).
_TOP_K = 5
#: Matches the synthesizer's own inline RAG-chunk citation token shape.
_RAG_CHUNK_CITE = re.compile(r"\[RAG chunk #(\d+)\]")


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

    def _retrieve_topk(
        self, question: str, records: list[dict[str, Any]], k: int
    ) -> list[dict[str, Any]]:
        """Embed records + question, return the top-k records by cosine similarity.

        Self-contained minimal embed+cosine-topk (no faiss dependency). Records
        with no usable text (empty title AND abstract) are dropped before ranking.
        Synchronous + CPU-bound — the caller runs it via ``asyncio.to_thread``.
        """
        import numpy as np

        usable = [
            r
            for r in records
            if (str(r.get("title") or "").strip() or str(r.get("abstract") or "").strip())
        ]
        if not usable:
            return []
        texts = [
            (
                str(r.get("title") or "").strip() + "\n" + str(r.get("abstract") or "").strip()
            ).strip()
            for r in usable
        ]
        model = self._get_embed_model()
        doc_emb = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        q_emb = model.encode([question], normalize_embeddings=True, convert_to_numpy=True)[0]
        sims = doc_emb @ q_emb
        order = np.argsort(-sims)[:k]
        return [usable[int(i)] for i in order]

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
            abstract = str(rec.get("abstract") or "").strip()
            lines.append(f"## [PMID:{pmid}] {title}".rstrip() if pmid else f"## {title}".rstrip())
            if abstract:
                lines.append(abstract)
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

        # Desktop locus: the connected host LLM is the synthesizer (CLAUDE.md "Two
        # operating modes" + LLM_ROLE). Hand it the FULL ontology-filtered evidence +
        # PMIDs and let it write the narrative. Done BEFORE retrieval on purpose: the
        # embedding narrowing (_retrieve_topk) needs the heavy ``rag`` extra, and
        # desktop mode must stay dependency-light (and the set is already bounded by
        # the harvest cap). The requires_llm gate reads LLM_ROLE so the tool is NOT
        # refused on a desktop with no Ollama.
        from apecx_integration.composition.runtime.execution_locus import (
            ExecutionLocus,
            get_active_locus,
        )

        if get_active_locus() == ExecutionLocus.DESKTOP:
            citations = sorted(
                {str(r.get("pmid")).strip() for r in filtered_records if r.get("pmid")}
            )
            self.nb_logger.info(
                "literature_rag LiteratureRagStep: desktop locus — handing %d "
                "ontology-filtered record(s) to the host for synthesis (no apecx "
                "LLM, no embedding retrieval)",
                len(filtered_records),
            )
            return {
                "answer": self._render_host_evidence(question, filtered_records),
                "citations": citations,
                "status": "host_synthesizes",
            }

        # 1. Retrieve top-k records (CPU-bound; offload so the event loop stays free).
        topk = await asyncio.to_thread(self._retrieve_topk, question, filtered_records, _TOP_K)
        if not topk:
            self.nb_logger.warning(
                "literature_rag LiteratureRagStep: %d filtered_records but none carry "
                "usable text (title/abstract); returning status=no_evidence",
                len(filtered_records),
            )
            return {"answer": "", "citations": [], "status": "no_evidence"}

        # 2. Build RAG chunks. The synthesizer numbers surviving chunks 1..N in the
        #    order given (its renderer also drops empty-text chunks; we pre-dropped
        #    them above so index_to_pmid stays aligned with the synthesizer's #N).
        chunks: list[dict[str, Any]] = []
        index_to_pmid: dict[int, str] = {}
        for i, rec in enumerate(topk, start=1):
            title = str(rec.get("title") or "").strip()
            abstract = str(rec.get("abstract") or "").strip()
            pmid = str(rec.get("pmid") or "").strip()
            chunks.append(
                {
                    "text": (title + "\n" + abstract).strip(),
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
            len(topk),
            len(citations),
        )
        return {"answer": answer, "citations": citations, "status": "ok"}
