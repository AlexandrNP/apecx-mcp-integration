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
#: The LLM relevance-VERIFY filter is OPT-IN (mirrors the CE flag, same anti-pattern rationale).
#: The embedding + cross-encoder signals have PLATEAUED on a hard class of off-target records that
#: share all surface vocabulary and differ only by a criterion that requires READING the paper.
#: No ranker separates them; an LLM that reads each candidate and judges the question's specific
#: criterion can. When truthy ('1'/'true') AND an apecx LLM is reachable, a final filter stage runs
#: on the gate survivors (after CE/coverage, before the floor+cap). Default OFF -> pipeline behaves
#: exactly as today. Degrades LOUD (one warning) to the pre-verify set when the LLM is unavailable
#: or every per-candidate call errors.
_LLM_VERIFY_ENABLE_ENV = "APECX_ENABLE_LLM_VERIFY"
#: Generic verify prompt template (question + title/abstract as parameters). Lives in a sibling
#: file, NOT in this .py (framework rule: prompts live in YAML/template files, never source). No
#: domain terms, no PMIDs — purely a relevance-judgment template.
_VERIFY_PROMPT_PATH = Path(__file__).resolve().parent / "verify_prompt.md"
#: The GRADED LLM+CE FUSION stage is OPT-IN behind its OWN flag (mirrors the CE/verify flags, same
#: anti-pattern rationale). It is a SEPARATE, newer alternative to the binary verify filter above:
#: instead of a yes/no drop + hard CE override (which still lets a top-CE but off-target record
#: survive, and cannot lift a wrongly-low-CE true positive), it asks the LLM for a GRADED relevance
#: SCORE in [0,1] per gate-survivor and FUSES it with the retrieval confidence (joint CE sigmoid
#: when CE ran, else bi-encoder cosine) into a joint score, re-ranks by joint, and cuts the low
#: cluster with an in-pool elbow. Default OFF -> this stage is absent and the pipeline (INCLUDING
#: the verify path above, which is untouched) is byte-identical to the pre-fusion behaviour.
#: Degrades LOUD (one warning) to the pre-fusion ranked set when the LLM is unavailable or every
#: per-candidate call errors.
_LLM_FUSION_ENABLE_ENV = "APECX_ENABLE_LLM_FUSION"
#: Generic graded-score prompt template (question + title/abstract -> a single 0..1 number). Lives
#: in a sibling file, NOT in this .py (framework rule: prompts live in template files). No domain
#: terms, no PMIDs — a pure relevance-grading template.
_FUSION_PROMPT_PATH = Path(__file__).resolve().parent / "fusion_prompt.md"
#: Fusion weight: ``joint = _FUSION_LLM_WEIGHT * llm_score + (1 - _FUSION_LLM_WEIGHT) * ce_score``.
#: 0.5 weights the LLM's read of the question-specific criterion EQUALLY with the retrieval ranker's
#: confidence — so a top-CE but LLM-judged-off-target record sinks (fixes the hard-override
#: precision leak) while a wrongly-low-CE but LLM-relevant record is lifted (recovers a true hit the
#: CE alone ranked too low). A const, not an eval-tuned value.
_FUSION_LLM_WEIGHT = 0.5
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
#: Over-rejection protection quantile (verify Fix 2): a verify candidate whose retrieval-
#: confidence score (joint CE sigmoid when the CE stage ran, else the bi-encoder cosine) is at
#: or above this quantile of the CURRENT verify pool is in the TOP retrieval-confidence tier and
#: SURVIVES even a confident LLM "no" — a single weak-verifier "no" must not drop a candidate the
#: retrieval ranker is highly confident about. 0.75 = the top quartile; it is a standard quantile
#: of the IN-POOL score distribution, NOT an eval-specific constant (the actual cut VALUE is
#: derived from the pool at run time). The LLM verdict stays decisive for the uncertain
#: middle/tail (below the quantile) and whenever there is no positive retrieval signal.
_VERIFY_PROTECT_QUANTILE = 0.75
#: The light VERIFY-PRECISION PASS is OPT-IN behind its OWN flag (mirrors the CE/verify/fusion flags,
#: same anti-pattern rationale). It is a CONSERVATIVE tail-trim that runs ONLY when the verify stage
#: ACTUALLY applied: the loosened verify prompt recovers true positives (recall up) but admits a thin
#: low-confidence tail that costs precision. When truthy ('1'/'true') AND verify ran, it scores the
#: verify-SURVIVORS by their retrieval confidence (joint CE sigmoid when the CE stage ran, else the
#: bi-encoder cosine) and sheds ONLY a tail separated by a clear DOMINANT gap (reusing
#: :meth:`_fusion_cut`) — a tight cluster is kept untouched, so the recall win is preserved. Default
#: OFF -> this pass is absent and the verify path (and all its tests) is byte-identical.
_VERIFY_PRECISION_PASS_ENABLE_ENV = "APECX_ENABLE_VERIFY_PRECISION_PASS"
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


def _quantile(values: list[float], q: float) -> float:
    """Linear-interpolated ``q``-quantile (``q`` in [0, 1]) of ``values`` — pure-Python, numpy-free.

    Matches numpy's default ``'linear'`` method so the derived tier cut is a STANDARD statistic of
    the current candidate pool, not a hand-tuned constant. Empty ``values`` -> ``0.0``. Used by
    :meth:`LiteratureRagStep._top_confidence_positions` to turn the fixed protection QUANTILE into a
    concrete in-pool score threshold at run time.
    """
    xs = sorted(values)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _cross_encoder_enabled() -> bool:
    """Whether the opt-in cross-encoder re-rank stage is ON (``APECX_ENABLE_CROSS_ENCODER``).

    Default OFF. Auto-activating a heavy component on mere library presence silently changes the
    ranking the moment sentence-transformers is installed — an anti-pattern. ON only for an
    explicit truthy flag (``'1'`` / ``'true'``, case-insensitive); anything else (unset / ``'0'``
    / ``'false'``) keeps the pre-CE pipeline (coverage -> bi-encoder bar -> coverage-only).
    """
    import os

    return os.environ.get(_CROSS_ENCODER_ENABLE_ENV, "").strip().lower() in {"1", "true"}


def _llm_verify_enabled() -> bool:
    """Whether the opt-in LLM relevance-verify filter is ON (``APECX_ENABLE_LLM_VERIFY``).

    Default OFF, mirroring :func:`_cross_encoder_enabled` exactly. ON only for an explicit truthy
    flag (``'1'`` / ``'true'``, case-insensitive); anything else (unset / ``'0'`` / ``'false'``)
    leaves the pipeline at its pre-verify behavior (coverage -> CE/bi-encoder bar -> floor+cap).
    """
    import os

    return os.environ.get(_LLM_VERIFY_ENABLE_ENV, "").strip().lower() in {"1", "true"}


def _llm_fusion_enabled() -> bool:
    """Whether the opt-in GRADED LLM+CE fusion stage is ON (``APECX_ENABLE_LLM_FUSION``).

    Default OFF, mirroring :func:`_cross_encoder_enabled` / :func:`_llm_verify_enabled` exactly. ON
    only for an explicit truthy flag (``'1'`` / ``'true'``, case-insensitive); anything else (unset
    / ``'0'`` / ``'false'``) leaves the pipeline at its pre-fusion behaviour. Independent of the
    verify flag: the fusion stage is a separate block gated on THIS env var alone.
    """
    import os

    return os.environ.get(_LLM_FUSION_ENABLE_ENV, "").strip().lower() in {"1", "true"}


def _verify_precision_pass_enabled() -> bool:
    """Whether the opt-in verify-precision pass is ON (``APECX_ENABLE_VERIFY_PRECISION_PASS``).

    Default OFF, mirroring :func:`_cross_encoder_enabled` / :func:`_llm_verify_enabled` /
    :func:`_llm_fusion_enabled` exactly. ON only for an explicit truthy flag (``'1'`` / ``'true'``,
    case-insensitive); anything else (unset / ``'0'`` / ``'false'``) leaves the pipeline at its
    pre-pass behaviour. Independent of all other flags: the pass is a separate block gated on THIS
    env var alone (and only fires when the verify stage actually applied).
    """
    import os

    return os.environ.get(_VERIFY_PRECISION_PASS_ENABLE_ENV, "").strip().lower() in {"1", "true"}


#: Leading tokens an LLM uses to affirm / deny relevance. Matched against the FIRST word of the
#: (lowercased, stripped) response — the prompt asks for a single leading word.
_VERIFY_YES_WORDS = frozenset(
    {"yes", "y", "true", "relevant", "relevance", "satisfies", "satisfied"}
)
_VERIFY_NO_WORDS = frozenset({"no", "n", "false", "irrelevant", "unrelated", "not", "off"})


def _parse_verify_verdict(text: str) -> bool | None:
    """Robustly parse an LLM relevance reply into ``True`` (relevant) / ``False`` (not) / ``None``.

    Accepts the shapes the brief requires and then some: a leading ``yes``/``no``, ``true``/
    ``false``, ``relevant``/``irrelevant`` word, OR a leading numeric score (``1``/``0``/``0.8``),
    where ``score >= 0.5`` is relevant. Returns ``None`` when the reply is empty or carries no
    parseable leading verdict — the caller treats ``None`` as UNKNOWN and KEEPS the candidate
    (uncertainty must never silently drop retrieved evidence; the verify filter only DROPS a
    confident ``False``). Case-insensitive; tolerant of surrounding punctuation/markdown.
    """
    s = (text or "").strip().lower().lstrip("*`_#->. \t")
    if not s:
        return None
    word = re.match(r"[a-z]+", s)
    if word:
        w = word.group(0)
        if w in _VERIFY_YES_WORDS:
            return True
        if w in _VERIFY_NO_WORDS:
            return False
    num = re.match(r"[-+]?\d*\.?\d+", s)
    if num:
        try:
            return float(num.group(0)) >= 0.5
        except ValueError:
            return None
    return None


def _parse_fusion_score(text: str) -> float:
    """Parse an LLM graded-relevance reply into a float in [0, 1] (fusion stage).

    Parse order (brief): a LEADING float wins and is CLAMPED to [0, 1] — so a graded ``0.8``
    keeps its value (unlike :func:`_parse_verify_verdict`, which collapses any number to a
    yes/no at 0.5). Else fall back to the binary verify semantics as a backstop: a leading
    yes/relevant word -> ``1.0``, a no/irrelevant word -> ``0.0``; anything unparseable (empty,
    ``"maybe"``, garbage) -> the neutral ``0.5``. ALWAYS returns a float: a successful LLM call
    that produced SOME text is a usable (possibly neutral) score — distinct from a call that
    ERRORED, which :meth:`LiteratureRagStep._llm_relevance_scores` records as ``None``.
    """
    s = (text or "").strip().lower().lstrip("*`_#->. \t")
    num = re.match(r"[-+]?\d*\.?\d+", s)
    if num:
        try:
            return min(1.0, max(0.0, float(num.group(0))))
        except ValueError:
            pass
    verdict = _parse_verify_verdict(text)
    if verdict is True:
        return 1.0
    if verdict is False:
        return 0.0
    return 0.5


#: Bibliographic fields emitted in an evidence citation, in render order. This base's harvested
#: records carry ``{pmid, pmcid, title}`` (see ``agents/literature/pipeline.harvest_and_stamp``);
#: ``abstract`` / ``full_text`` are CONTENT (they populate the chunk text, not the citation) and
#: ``iris`` are ontology tags, so neither appears here. A record missing any of these simply omits
#: it — citations are never fabricated or null-filled.
_CITATION_FIELDS = ("pmid", "pmcid", "title")


def _citation_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """Present-only publication citation from a record's bibliographic fields.

    Emits each of :data:`_CITATION_FIELDS` ONLY when the record carries a non-empty value; an
    absent/blank field is OMITTED (never fabricated or null-filled). Values are returned verbatim
    (string-coerced + stripped) — presentation, not interpretation.
    """
    citation: dict[str, Any] = {}
    for field in _CITATION_FIELDS:
        value = record.get(field)
        if value is not None and str(value).strip():
            citation[field] = str(value).strip()
    return citation


def build_evidence_blocks(
    records: list[dict[str, Any]],
    scores: list[dict[str, float]],
    *,
    char_cap: int,
) -> list[dict[str, Any]]:
    """Ordered per-document evidence blocks — PURE presentation, no ranking (no behavior change).

    One block per record, in the given (already ranked+capped) order, aligned 1:1 with ``scores``::

        {"rank": <1-based>, "citation": {present fields}, "chunks": [{"text": ...}], "scores": {...}}

    The chunk ``text`` is the retrieved passage used downstream: the record's title + its grounding
    text (:func:`_evidence_text` — open-access ``full_text`` when present, else ``abstract``, capped
    at ``char_cap`` so the block mirrors exactly what THIS locus hands to synthesis). ``scores`` is
    the per-document retrieval-signal dict collected during ranking (coverage / keyword / cosine /
    cross-encoder, present-only). Never reorders, drops, or adds a record.
    """
    blocks: list[dict[str, Any]] = []
    for rank, (record, score) in enumerate(zip(records, scores, strict=True), start=1):
        title = str(record.get("title") or "").strip()
        passage = _evidence_text(record, char_cap=char_cap)
        text = (title + "\n" + passage).strip() if passage else title
        blocks.append(
            {
                "rank": rank,
                "citation": _citation_from_record(record),
                "chunks": [{"text": text}] if text else [],
                "scores": dict(score),
            }
        )
    return blocks


def render_evidence_markdown(evidence: list[dict[str, Any]]) -> str:
    """Deterministic Markdown view of an evidence list (pure function of the structure).

    Renders each block in list order: a numbered citation header (present fields only, fixed
    field order), its retrieval scores (keys sorted, fixed numeric format), then each chunk's
    text. Depends ONLY on the passed structure — the same input always yields byte-identical
    output — so the host/desktop path can present ``result["evidence"]`` without re-deriving it.
    """
    lines: list[str] = []
    for block in evidence:
        rank = block.get("rank")
        citation = block.get("citation") or {}
        cite_parts = [
            f"{field}: {citation[field]}" for field in _CITATION_FIELDS if field in citation
        ]
        header = f"## {rank}. " + (" | ".join(cite_parts) if cite_parts else "(no citation fields)")
        lines.append(header.rstrip())
        scores = block.get("scores") or {}
        if scores:
            score_parts = [f"{key}={float(scores[key]):.4f}" for key in sorted(scores)]
            lines.append("- scores: " + ", ".join(score_parts))
        for chunk in block.get("chunks") or []:
            text = str(chunk.get("text") or "").strip()
            if text:
                lines.append("")
                lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


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

    def _get_verify_prompt_template(self) -> str:
        """Lazily load + cache the generic verify prompt template from the sibling file.

        Loaded here (not module top) so a caller that never enables the verify stage never
        touches the file. The template carries ``{question}``/``{title}``/``{abstract}``
        placeholders (no domain terms, no PMIDs) — the prompt lives in a file, not source.
        """
        tpl = getattr(self, "_verify_prompt_template", None)
        if tpl is None:
            tpl = _VERIFY_PROMPT_PATH.read_text(encoding="utf-8")
            self._verify_prompt_template = tpl
        return tpl

    def _build_verify_llm(self):
        """Build the apecx LLM for the verify filter via the SHARED factory (``APECX_LLM_*``).

        Reuses the exact client/config path every synthesis path uses (``preflight_llm_model`` +
        ``build_chat_llm``) — no new client. RAISES when the endpoint is unreachable / model not
        pulled / the langchain stack is absent; the caller catches it and degrades LOUD.
        """
        from apecx_integration.agents._llm_config import preflight_llm_model
        from apecx_integration.agents._llm_factory import build_chat_llm

        preflight_llm_model()
        return build_chat_llm()

    def _llm_relevance_verdicts(
        self, question: str, records: list[dict[str, Any]]
    ) -> list[bool | None]:
        """Per-record LLM relevance verdict (aligned to ``records``): ``True``/``False``/``None``.

        For EACH record, call the apecx LLM with the generic verify prompt (question + the
        record's title+abstract) and parse the reply with :func:`_parse_verify_verdict`. A
        per-record call error maps to ``None`` (UNKNOWN — the caller keeps it; uncertainty must
        not silently drop evidence). RAISES only if the LLM client itself cannot be built — the
        caller (:meth:`_hybrid_rank`) catches that and SKIPS the whole verify stage, loud.
        Synchronous (``llm.invoke``); the whole rank runs in a worker thread already.
        """
        from langchain_core.messages import HumanMessage

        llm = self._build_verify_llm()
        template = self._get_verify_prompt_template()
        verdicts: list[bool | None] = []
        for rec in records:
            prompt = template.format(
                question=question,
                title=str(rec.get("title") or "").strip(),
                abstract=_evidence_text(rec, char_cap=_FULLTEXT_BACKEND_CAP),
            )
            try:
                response = llm.invoke([HumanMessage(content=prompt)])
                content = getattr(response, "content", None)
                verdicts.append(
                    _parse_verify_verdict(content) if isinstance(content, str) else None
                )
            except Exception as exc:  # noqa: BLE001 — a per-record failure is UNKNOWN, never a crash
                self.nb_logger.warning(
                    "literature_rag LiteratureRagStep: LLM verify call failed for one "
                    "candidate (%s: %s); treating as UNKNOWN (kept)",
                    type(exc).__name__,
                    exc,
                )
                verdicts.append(None)
        return verdicts

    def _get_fusion_prompt_template(self) -> str:
        """Lazily load + cache the generic graded-score prompt template from the sibling file.

        Loaded here (not module top) so a caller that never enables fusion never touches the file.
        Carries ``{question}``/``{title}``/``{abstract}`` placeholders (no domain terms, no PMIDs).
        """
        tpl = getattr(self, "_fusion_prompt_template", None)
        if tpl is None:
            tpl = _FUSION_PROMPT_PATH.read_text(encoding="utf-8")
            self._fusion_prompt_template = tpl
        return tpl

    def _build_fusion_llm(self):
        """Build the apecx LLM for the fusion stage via the SHARED factory (``APECX_LLM_*``).

        Identical plumbing to :meth:`_build_verify_llm` but a SEPARATE method so the fusion path is
        independently stubbable in tests and never touches the verify path. RAISES when the
        endpoint is unreachable / model not pulled / the langchain stack is absent; the caller
        catches it and degrades LOUD to the pre-fusion set.
        """
        from apecx_integration.agents._llm_config import preflight_llm_model
        from apecx_integration.agents._llm_factory import build_chat_llm

        preflight_llm_model()
        return build_chat_llm()

    def _llm_relevance_scores(
        self, question: str, records: list[dict[str, Any]]
    ) -> list[float | None]:
        """Per-record GRADED LLM relevance score (aligned to ``records``): a float in [0, 1], or
        ``None`` when the per-record LLM call itself ERRORED.

        For EACH record, call the apecx LLM with the generic graded-score prompt (question + the
        record's title+abstract) and parse the reply with :func:`_parse_fusion_score` (leading
        float -> clamped [0, 1]; yes/no word -> 1.0/0.0; anything else -> 0.5 backstop). A
        per-record call error maps to ``None`` so the caller can (a) substitute the 0.5 unknown
        backstop in the fusion when SOME calls succeeded and (b) detect the all-errored case and
        degrade LOUD. RAISES only if the LLM client itself cannot be built — the caller
        (:meth:`_hybrid_rank`) catches that and SKIPS the whole fusion stage, loud. Synchronous
        (``llm.invoke``); the whole rank runs in a worker thread already.
        """
        from langchain_core.messages import HumanMessage

        llm = self._build_fusion_llm()
        template = self._get_fusion_prompt_template()
        scores: list[float | None] = []
        for rec in records:
            prompt = template.format(
                question=question,
                title=str(rec.get("title") or "").strip(),
                abstract=_evidence_text(rec, char_cap=_FULLTEXT_BACKEND_CAP),
            )
            try:
                response = llm.invoke([HumanMessage(content=prompt)])
                content = getattr(response, "content", None)
                scores.append(_parse_fusion_score(content) if isinstance(content, str) else None)
            except Exception as exc:  # noqa: BLE001 — a per-record failure is a failed call, no crash
                self.nb_logger.warning(
                    "literature_rag LiteratureRagStep: LLM fusion score call failed for one "
                    "candidate (%s: %s); treating it as a failed call (0.5 backstop when any "
                    "other call succeeds, else the whole fusion stage is skipped)",
                    type(exc).__name__,
                    exc,
                )
                scores.append(None)
        return scores

    @staticmethod
    def _fusion_cut(sorted_desc: list[float]) -> float:
        """In-pool elbow threshold for the fusion joint scores: keep every score >= the return.

        ``sorted_desc`` is the joint scores sorted DESCENDING. The cut drops the low cluster that
        sits below the single DOMINANT gap in the descending scores — "dominant" means that gap
        exceeds the COMBINED rest (i.e. it is more than half the total top-to-bottom descent), a
        comparison derived purely from THIS pool, with NO eval constant. When no gap dominates
        (saturated / evenly-spread pool), nothing is cut (threshold = the min score -> keep all),
        so a pool of uniformly-relevant survivors is NOT over-trimmed. Precision-favoring: ties on
        the max gap resolve to the TOPMOST gap (``list.index`` returns the first), the most
        aggressive deterministic cut. Empty -> 0.0; single -> that score.
        """
        if len(sorted_desc) <= 1:
            return sorted_desc[0] if sorted_desc else 0.0
        gaps = [a - b for a, b in zip(sorted_desc, sorted_desc[1:], strict=False)]
        total = sorted_desc[0] - sorted_desc[-1]  # == sum(gaps)
        max_gap = max(gaps)
        # No dominant separation (max gap <= the combined rest) -> keep all.
        if total <= 0.0 or max_gap <= total - max_gap:
            return sorted_desc[-1]
        idx = gaps.index(max_gap)  # topmost dominant gap -> precision-favoring + deterministic
        return sorted_desc[idx]  # keep everything >= the upper side of that gap

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

    @staticmethod
    def _top_confidence_positions(scores: list[float]) -> set[int]:
        """Positions of the TOP retrieval-confidence tier within the in-pool ``scores`` (verify Fix 2).

        A position is protected when its score is at or above the ``_VERIFY_PROTECT_QUANTILE``
        (top-quartile) value of ``scores`` — a quantile of THIS pool's own distribution, so the cut
        VALUE is derived from the current candidates, never a fixed eval constant. When there is no
        positive signal (empty, or every score <= 0 — e.g. the CE stage did not run AND embeddings
        are unavailable so the cosines are all zero), the tier is EMPTY: with no retrieval confidence
        to trust, the LLM verdict stays fully decisive. Aligned to the order of ``scores``.
        """
        if not scores or not any(s > 0.0 for s in scores):
            return set()
        threshold = _quantile(scores, _VERIFY_PROTECT_QUANTILE)
        return {pos for pos, s in enumerate(scores) if s >= threshold}

    def _hybrid_rank(
        self,
        question: str,
        records: list[dict[str, Any]],
        k: int,
        *,
        return_scores: bool = False,
    ) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, float]]]:
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

          3. LLM relevance-verify (NEW, opt-in via ``APECX_ENABLE_LLM_VERIFY``, STRONGEST): on the
             survivors of (1)+(2), an apecx LLM READS each candidate (title+abstract) and judges
             whether it SATISFIES the question's specific criterion — the signal the embedding /
             cross-encoder stages cannot provide for records that share all surface vocabulary.
             DROPS a confident "not relevant" verdict (False) ONLY when the candidate is NOT in the
             TOP retrieval-confidence tier (Fix 2: a top-quantile CE/cosine hit SURVIVES even a
             weak-verifier "no"); keeps "relevant" or UNKNOWN. The tier is the top quantile
             (``_VERIFY_PROTECT_QUANTILE``) of the in-pool CE sigmoid (or bi-encoder cosine) — a
             pool-derived cut, no eval constant. SKIPPED (one loud warning, pre-verify set returned)
             when the LLM is unreachable or every call errors. Default OFF -> this stage is absent
             and the pipeline is byte-identical to the pre-verify behaviour.

          4. GRADED LLM+CE fusion (NEW, opt-in via ``APECX_ENABLE_LLM_FUSION``, a separate
             ALTERNATIVE to (3)): on the survivors of (1)+(2), an apecx LLM returns a GRADED score
             in [0,1] per candidate; it is FUSED with the retrieval confidence (joint CE sigmoid, or
             bi-encoder cosine) as ``joint = _FUSION_LLM_WEIGHT*llm + (1-w)*ce``; survivors are
             re-ranked by ``joint`` desc and the low cluster is cut with an in-pool elbow
             (:meth:`_fusion_cut`, no eval constant). This separates a top-CE-but-off-target record
             (sinks on a low LLM score) from a wrongly-low-CE true positive (lifted on a high LLM
             score) — the blunt-override failure the binary verdict in (3) cannot fix. SKIPPED
             (one loud warning, pre-fusion set) when the LLM is unreachable or every call errors.
             Default OFF -> absent; a separate flag + block from (3), so each is independent.

          3b. VERIFY-PRECISION PASS (NEW, opt-in via ``APECX_ENABLE_VERIFY_PRECISION_PASS``): a light
             CONSERVATIVE tail-trim that runs ONLY when (3) ACTUALLY applied. On the verify-SURVIVORS
             only, it scores each by its retrieval confidence (joint CE sigmoid when the CE stage ran,
             else the bi-encoder cosine) and sheds ONLY a tail separated by a clear DOMINANT gap —
             reusing :meth:`_fusion_cut` (saturated / no dominant gap -> keep ALL, so the recall win
             is preserved). Prior order is preserved (it sheds, it does NOT re-rank); the floor
             repopulates ONLY from verify-survivors (never re-injecting a verify-dropped record).
             Default OFF -> absent; the verify output is byte-identical.

        Recall floor: when verify/fusion did NOT run, the kept set is always between
        ``min(_GATE_MIN_KEEP, n)`` and ``k`` — the floor pads from ``pad_source`` after the
        coverage + CE/bi-encoder bars, so neither cuts below the floor (byte-identical to the
        pre-verify behaviour). When verify DID run (Fix 1, precision-first), the floor may
        repopulate ONLY from the verify-survivors, never re-injecting a rejected record; so if
        verify trims below the floor the step returns exactly those survivors (a clean set smaller
        than the floor is correct). If NO facet matches any record (all keyword scores 0), fall
        back to embedding-cosine top-``k``. Synchronous + CPU/IO-bound — caller runs it via
        ``asyncio.to_thread``.

        Observability: with ``return_scores=True`` the return becomes
        ``(ranked_records, per_record_scores)`` where ``per_record_scores`` is aligned 1:1 with
        ``ranked_records`` and each entry is a present-only dict of the retrieval signals computed
        for that record (``keyword_idf`` / ``semantic_cosine`` / ``discriminative_coverage`` /
        ``cross_encoder``). A pure side-channel for the evidence block — it NEVER affects which
        records are returned or their order. Default (``return_scores=False``) returns the ranked
        records list exactly as before.
        """
        usable = [r for r in records if self._record_text(r)]
        if not usable:
            return ([], []) if return_scores else []

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

        # Per-document retrieval signals, collected for the evidence block ONLY (observability —
        # NO effect on ranking/order). Keyed by usable-index; augmented as later signals
        # (discriminative coverage, cross-encoder) are computed below. Present-only: a signal that
        # did not run for this record (embeddings unavailable, CE stage off) is simply absent.
        score_by_index: dict[int, dict[str, float]] = {}
        for _i in range(len(usable)):
            _sig: dict[str, float] = {"keyword_idf": float(kw_scores[_i])}
            if embed_available:
                _sig["semantic_cosine"] = float(sims[_i])
            score_by_index[_i] = _sig

        def _ranked(indices: list[int]):
            out_records = [usable[i] for i in indices]
            if return_scores:
                return out_records, [dict(score_by_index[i]) for i in indices]
            return out_records

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
            return _ranked(emb_order[:k])

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
        for _i in range(len(usable)):
            score_by_index[_i]["discriminative_coverage"] = float(disc_cov[_i])
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
        #: Per-usable-index CE sigmoid scores, populated only when the CE stage runs. Read by the
        #: LLM-verify over-rejection protection (Fix 2) as the strongest retrieval-confidence signal.
        ce_by_index: dict[int, float] = {}
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
                for _i, _ce in ce_by_index.items():
                    score_by_index[_i]["cross_encoder"] = float(_ce)
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

        # LLM relevance-verify filter (opt-in, STRONGEST stage) — runs on the gate survivors in
        # EVERY locus, like the CE gate, AFTER the CE/bi-encoder bar and BEFORE the floor+cap. The
        # embedding + CE signals have plateaued on off-target records that share all surface
        # vocabulary and differ only by a criterion that needs READING the paper; an LLM that reads
        # each candidate and judges the question's specific criterion separates them. DROP only a
        # confident "not relevant" verdict (False); KEEP a "relevant" (True) or an UNKNOWN (None —
        # parse miss / per-record error), so uncertainty never silently drops evidence. Prior order
        # of ``kept`` is preserved (deterministic ties). DEGRADE LOUD: if the LLM client cannot be
        # built, OR every per-record call errored (all verdicts None), SKIP the filter entirely with
        # ONE warning and fall back to the pre-verify ``kept``. The floor+cap below then apply to the
        # verify result exactly as to the CE result, so the recall floor is never violated.
        verify_applied = False
        if _llm_verify_enabled() and kept:
            try:
                verdicts = self._llm_relevance_verdicts(question, [usable[i] for i in kept])
            except Exception as exc:  # noqa: BLE001 — LLM unavailable -> skip verify, loud, no crash
                self.nb_logger.warning(
                    "literature_rag LiteratureRagStep: LLM verify filter unavailable "
                    "(%s: %s); SKIPPING it and returning the pre-verify (CE/coverage) set",
                    type(exc).__name__,
                    exc,
                )
            else:
                if any(v is not None for v in verdicts):
                    # Fix 2 — protect the TOP retrieval-confidence tier from a weak-verifier "no".
                    # Score each verify candidate by the strongest retrieval signal already computed
                    # in THIS path (joint CE sigmoid when the CE stage ran, else the bi-encoder
                    # cosine); the top-quantile tier is derived from that in-pool distribution. DROP
                    # a candidate only on a confident "no" (False) that is NOT in the top tier; a
                    # top-tier hit SURVIVES even on "no". "yes" (True) / UNKNOWN (None) still keep,
                    # so uncertainty never silently drops evidence and the LLM verdict remains
                    # decisive for the uncertain middle/tail. Prior order of ``kept`` preserved.
                    conf_scores = (
                        [ce_by_index[i] for i in kept] if ce_by_index else [sims[i] for i in kept]
                    )
                    protected = self._top_confidence_positions(conf_scores)
                    kept = [
                        i
                        for pos, (i, v) in enumerate(zip(kept, verdicts, strict=True))
                        if v is not False or pos in protected
                    ]
                    verify_applied = True
                else:
                    self.nb_logger.warning(
                        "literature_rag LiteratureRagStep: every LLM verify call failed "
                        "(no usable verdict); SKIPPING the verify filter and returning the "
                        "pre-verify (CE/coverage) set"
                    )

        # VERIFY-PRECISION PASS (opt-in via ``APECX_ENABLE_VERIFY_PRECISION_PASS``) — a light,
        # CONSERVATIVE tail-trim that runs ONLY when the verify stage ACTUALLY applied, so it acts on
        # genuine verify-SURVIVORS (verify OFF / skipped -> ``verify_applied`` is False -> this never
        # runs). The loosened verify prompt recovers true positives (recall up) but admits a thin
        # low-confidence tail that lowers precision. Here, on the verify-survivors ONLY, score each by
        # the strongest retrieval signal already computed in THIS path (joint CE sigmoid when the CE
        # stage ran, else the bi-encoder cosine) and shed ONLY a tail separated by a clear DOMINANT
        # gap — reusing :meth:`_fusion_cut` (cut below the single gap that exceeds half the total
        # descent; saturated / no-gap -> keep ALL). So a well-separated low tail is trimmed while a
        # tight cluster is kept untouched, keeping the recall win intact (no dominant gap -> no trim).
        # Prior order of ``kept`` is PRESERVED — the pass sheds a tail, it does NOT re-rank.
        # Floor-from-survivors + no-reinject: if the trim would drop below the recall floor,
        # repopulate ONLY from the highest-confidence verify-survivors (the pool IS ``kept``, so a
        # verify-dropped record is never re-injected). Deterministic. Default OFF -> absent; the
        # verify output stays byte-identical.
        if verify_applied and _verify_precision_pass_enabled() and kept:
            conf = [ce_by_index[i] for i in kept] if ce_by_index else [sims[i] for i in kept]
            cut = self._fusion_cut(sorted(conf, reverse=True))
            survivor_pos = [pos for pos, c in enumerate(conf) if c >= cut]
            prec_floor = min(_GATE_MIN_KEEP, len(usable))
            if len(survivor_pos) < prec_floor:
                # Pad back the highest-confidence trimmed survivors (deterministic ties -> prior
                # order) up to the floor — the pool is exactly the verify survivors, so this never
                # re-injects a verify-dropped record.
                keep_set = set(survivor_pos)
                dropped = sorted(
                    (pos for pos in range(len(kept)) if pos not in keep_set),
                    key=lambda pos: (-conf[pos], pos),
                )
                survivor_pos = sorted(keep_set | set(dropped[: prec_floor - len(survivor_pos)]))
            kept = [kept[pos] for pos in survivor_pos]

        # GRADED LLM+CE FUSION filter (opt-in via ``APECX_ENABLE_LLM_FUSION``, a NEWER alternative
        # to the binary verify filter above) — runs on the gate survivors in EVERY locus, AFTER the
        # CE/bi-encoder bar and BEFORE the floor+cap, exactly where verify runs. Separate flag,
        # separate block: when the fusion flag is unset NONE of this executes and the pipeline
        # (verify path included) is byte-identical. Instead of a yes/no drop + hard CE override
        # (which lets a top-CE but off-target record survive and cannot lift a wrongly-low-CE true
        # positive), ask the LLM for a GRADED score in [0,1] per survivor and FUSE it with the
        # retrieval confidence (joint CE sigmoid when the CE stage ran, else the bi-encoder cosine):
        # joint = w*llm + (1-w)*ce. Re-rank survivors by joint desc (ties -> prior order ->
        # deterministic) and cut the low cluster with the in-pool elbow (:meth:`_fusion_cut`, no eval
        # constant). So a high-CE/near-0-LLM record sinks below the cut while a high-LLM/mid-CE record
        # is retained. DEGRADE LOUD: client un-buildable OR every per-call errored (all-None) -> SKIP
        # fusion with ONE warning and return the pre-fusion ``kept``.
        fusion_applied = False
        if _llm_fusion_enabled() and kept:
            try:
                scores = self._llm_relevance_scores(question, [usable[i] for i in kept])
            except Exception as exc:  # noqa: BLE001 — LLM unavailable -> skip fusion, loud, no crash
                self.nb_logger.warning(
                    "literature_rag LiteratureRagStep: LLM fusion filter unavailable "
                    "(%s: %s); SKIPPING it and returning the pre-fusion (CE/coverage) set",
                    type(exc).__name__,
                    exc,
                )
            else:
                if any(s is not None for s in scores):
                    # ce_score per survivor: the strongest retrieval signal already computed in THIS
                    # path (joint CE sigmoid when the CE stage ran, else the bi-encoder cosine). A
                    # failed per-call score (None) falls back to the 0.5 neutral backstop.
                    ce_scores = (
                        [ce_by_index[i] for i in kept] if ce_by_index else [sims[i] for i in kept]
                    )
                    joint_by_index = {
                        idx: _FUSION_LLM_WEIGHT * (s if s is not None else 0.5)
                        + (1.0 - _FUSION_LLM_WEIGHT) * ce
                        for idx, s, ce in zip(kept, scores, ce_scores, strict=True)
                    }
                    prior_rank = {idx: rank for rank, idx in enumerate(kept)}
                    ranked_kept = sorted(kept, key=lambda i: (-joint_by_index[i], prior_rank[i]))
                    cut = self._fusion_cut([joint_by_index[i] for i in ranked_kept])
                    kept = [i for i in ranked_kept if joint_by_index[i] >= cut]
                    fusion_applied = True
                else:
                    self.nb_logger.warning(
                        "literature_rag LiteratureRagStep: every LLM fusion score call failed "
                        "(no usable score); SKIPPING the fusion filter and returning the "
                        "pre-fusion (CE/coverage) set"
                    )

        # Recall floor. Fix 1 (precision): when the verify OR the fusion stage ACTUALLY ran, the
        # floor may repopulate ONLY from THEIR survivors — never re-inject an LLM-dropped / below-
        # cut record. The survivors ARE ``kept``, so if either trimmed below the floor we return
        # exactly those survivors (a smaller, clean set is correct under a precision-first
        # objective): the pre-LLM floor-pad from ``pad_source`` (which spans dropped records) is
        # SKIPPED. When neither ran (both flags OFF / client unavailable / all-UNKNOWN), the floor
        # behaves EXACTLY as before — padding from ``pad_source`` so no prior test/cycle is
        # disturbed (``not fusion_applied`` is a no-op when fusion is OFF).
        floor = min(_GATE_MIN_KEEP, len(usable))
        if not verify_applied and not fusion_applied and len(kept) < floor:
            kept = pad_source[:floor]
        return _ranked(kept[:k])

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
        ranked, ranked_scores = await asyncio.to_thread(
            self._hybrid_rank, question, filtered_records, _TOP_K, return_scores=True
        )
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
                # Structured per-document evidence (presentation only — same ranked set/order as
                # the answer + citations above). Desktop char cap, matching the host-handed text.
                "evidence": build_evidence_blocks(
                    ranked, ranked_scores, char_cap=_FULLTEXT_DESKTOP_CAP
                ),
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
        return {
            "answer": answer,
            "citations": citations,
            "status": "ok",
            # Structured per-document evidence (presentation only — same ranked set/order the
            # synthesizer saw). Backend char cap, matching the RAG chunk text handed to synthesis.
            "evidence": build_evidence_blocks(
                ranked, ranked_scores, char_cap=_FULLTEXT_BACKEND_CAP
            ),
        }
