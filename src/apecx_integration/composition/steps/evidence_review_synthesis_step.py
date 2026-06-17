"""EvidenceReviewSynthesisStep — LLM evidence synthesis + a DETERMINISTIC
structural-evidence section.

Terminal-but-one step of ``viral_epitope_analysis``. It reuses the
``apecx_integration.agents.rag_synthesis.synthesize_response`` FUNCTION (one LLM
round-trip, grounded inline citations) to turn the assembled multi-source bundle
into evidence Markdown — then appends a **deterministically rendered** structural
section.

Why the structural section is deterministic and not left to the LLM: a no-hit
must be LOUD. If we relied on the synthesizer to "mention" PDB/EMDB, a no-hit
would silently become an omission (green test, missing product signal). Instead
``render_structural_section`` always emits a section — either the structural
records found, or the explicit ``structural_note`` limitation produced upstream
by ``StructuralEvidenceStep``. The presence of the section is guaranteed; only
its content varies.

Input contract (the bundle emitted by ``StructuralEvidenceStep``)::

    {"query": str, "rag_chunks": [...], "bvbrc_genomes": [...],
     "violin_mappings": [...], "publications": [...], "globus_results": [...],
     "structural_records": [...], "structural_note": str | None}

Output::  {"markdown": "<evidence markdown + structural section>"}

The ``markdown`` key feeds a downstream ``EnvelopeStep`` (default
``markdown_input_key``), which wraps it into the terminal ``WorkflowResult``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents.globus_search._datacite import (
    datacite_identifiers,
    datacite_primary_id,
    datacite_subjects,
    datacite_title,
)
from apecx_integration.composition.steps._stage_report import render_stage_reports

log = logging.getLogger(__name__)

# Object-identifier types surfaced in the Sources ledger, most-specific first.
_GLOBUS_ID_TYPES: tuple[str, ...] = (
    "PDB",
    "EMDB",
    "GenBank",
    "RefSeq",
    "BVBRC-Genome",
    "BVBRC-Protein",
    "UniProt",
    "DOI",
)

_INPUT_KEY = "review_input"
_STRUCTURAL_HEADING = "## Structural evidence (PDB / EMDB)"
_ANSWER_HEADING = "# Answer"
_CROSSDATA_HEADING = "## Cross-data reasoning"
_SOURCES_HEADING = "## Sources and evidence"
_COVERAGE_HEADING = "## Evidence coverage"
_ANALYSIS_HEADING = "## Analysis steps"
_FOLLOWUPS_HEADING = "## Follow-up questions"
_INSIGHT_HEADING = "## Integrated insight"
# The five contract sections, in the order the final document MUST carry them.
# `# Answer` · `## Cross-data reasoning` · `## Integrated insight` come from the
# LLM (or, on degrade, from render_evidence_fallback); `## Sources and evidence`
# and `## Follow-up questions` are built deterministically below.
_DEFAULT_PROMPT_FILENAME = "evidence_review_synthesis_prompt.yml"


def _sanitize_inline(text: Any, cap: int = 300) -> str:
    """Collapse a free/user-supplied string to a SINGLE inline markdown-safe line.

    All whitespace (incl. NEWLINES) → single spaces, ``#`` stripped, length capped. This
    prevents a user-controlled value (the query, a coverage term, a degrade reason) from
    injecting document STRUCTURE into the deterministic sections: collapsing newlines means
    nothing can start a new line (so an embedded ``## Sources and evidence`` cannot become a
    real header, ``>`` cannot start a blockquote, ``1.`` cannot start a list); stripping
    ``#`` is belt-and-suspenders. The five-section output contract relies on each contract
    header appearing EXACTLY once — an un-sanitized query with embedded headers duplicates
    them and breaks header-based parsing (and can fabricate a fake citations section)."""
    return " ".join(str(text if text is not None else "").replace("#", "").split())[:cap]


def _collapse_ws(text: Any) -> str:
    """LIGHT inline-safety for EXTERNAL-DB display strings (publication / genome / VIOLIN /
    structure titles): collapse all whitespace incl. NEWLINES to single spaces — nothing can
    start a new line, so a malformed title carrying ``\\n## ...`` cannot inject a stray header
    into the Sources/Structural section. Unlike ``_sanitize_inline`` it does NOT strip ``#``
    (harmless mid-line) and does NOT length-cap — a curated title must render in full, just on
    one line. (E4-6.)"""
    return " ".join(str(text if text is not None else "").split())


def render_evidence_fallback(
    query: str, publications: list[dict[str, Any]] | None, reason: str
) -> str:
    """Deterministic narrative body used when LLM synthesis FAILS its gate.

    Reliability contract: the evidence review must NEVER discard retrieved evidence
    because of an LLM-output-quality failure (e.g. the synthesizer's strict
    citation-grounding gate rejecting a backtick-wrapped-but-real ID). When
    ``synthesize_response`` raises, we still return the retrieved publications with
    their citations, and we NAME the reason narrative synthesis was withheld — loud,
    never silent, never empty-when-evidence-exists.

    Emits the SAME three contract headings the LLM would (``# Answer``,
    ``## Cross-data reasoning``, ``## Integrated insight``) so the degraded document
    is still contract-shaped — the caller appends the deterministic
    ``## Sources and evidence`` + ``## Follow-up questions`` exactly as on the success
    path. A citation-gate failure therefore yields a five-section doc with the
    evidence preserved, not a differently-shaped error page.
    """
    # Sanitize the reason before interpolation: the synthesizer's citation-gate
    # exception can embed the RAW LLM response (with its own stray `## ` headers +
    # newlines). Interpolated into the blockquote below, an embedded newline would
    # escape the `>` prefix and a stray `## Integrated insight` would leak as a
    # standalone header — breaking the five-section ordering on the degrade path.
    # Collapse ALL whitespace (incl. newlines) to single spaces, STRIP `#` (so an
    # echoed `## Integrated insight` cannot survive even as inline text that a
    # heading scan would mistake for a section), and cap length — so the reason can
    # never inject document structure. (Surfaced 2026-06-13 by a 4B model emitting
    # fullwidth brackets that tripped the gate; its raw response, carried in the gate
    # exception, embedded out-of-order contract headings.)
    reason = _sanitize_inline(reason, cap=500)
    pubs = publications or []
    lines = [
        _ANSWER_HEADING,
        "",
        f"> **Narrative synthesis was withheld** — {reason}. The retrieved evidence "
        f"is preserved below and enumerated in the Sources and evidence section so "
        f"nothing is lost.",
        "",
        f"Question: {_sanitize_inline(query)}",
        "",
    ]
    if pubs:
        lines.append("Retrieved publications relevant to the question:")
        lines.append("")
        for p in pubs:
            if not isinstance(p, dict):
                continue
            ident = p.get("doi") or p.get("id") or p.get("pmid") or ""
            title = p.get("title") or "(untitled)"
            cite = f"**[{ident}]** " if ident else ""
            lines.append(f"- {cite}*{title}*")
    else:
        lines.append("_No publications were retrieved for this query._")
    lines += [
        "",
        "## Cross-data reasoning",
        "",
        "> Cross-data reasoning was not generated because narrative synthesis was "
        "withheld (see the reason above). The retrieved records and how they relate "
        "are enumerated in the Sources and evidence section below.",
        "",
        _INSIGHT_HEADING,
        "",
        "> No integrated insight could be synthesized without the narrative model. "
        "Re-run once the synthesis-gate condition named above is resolved; the "
        "retrieved evidence itself is intact.",
    ]
    return "\n".join(lines)


def render_structural_section(
    structural_records: list[dict[str, Any]] | None,
    structural_note: str | None,
    reasoning: dict[str, Any] | None = None,
) -> str:
    """Render the structural-evidence Markdown section. Pure + LLM-free so the
    no-silent-failure guarantee is unit-testable without a live model.

    Contract: ALWAYS returns a non-empty section.
      - records present → a bulleted list (citation token + title + source);
      - else → the ``structural_note`` as a blockquote limitation.
    Then, from the PyMOL SASA ``reasoning`` result (``bundle['structural_reasoning']``):
      - if a visualization was rendered → embed the epitope-surface PNG;
      - else if the SASA assessment was unavailable → surface the UNDERLYING reason
        (the container note/traceback), never a silent "failed".
    """
    records = structural_records or []
    lines = [_STRUCTURAL_HEADING, ""]
    if records:
        for h in records:
            if not isinstance(h, dict):
                continue
            subject = h.get("subject") or "(unknown)"
            content = h.get("content") or {}
            # DataCite titles[0].title, not a flat "title" key (else "(untitled)").
            title = datacite_title(content)
            source = h.get("structural_source") or "structure"
            lines.append(f"- **[Globus {subject}]** *{title or '(untitled)'}* — {source}")
    else:
        # No records — the absence MUST be named, never silently empty.
        note = structural_note or (
            "No PDB or EMDB structural records were found for this query in the "
            "APECx structural corpus."
        )
        lines.append(f"> {note}")

    # PyMOL SASA / surface-exposure outcome: the visualization, or the real failure reason.
    if isinstance(reasoning, dict):
        viz = reasoning.get("visualization_artifact")
        if viz:
            pdb = reasoning.get("pdb_id") or "structure"
            lines.append("")
            lines.append(f"![Epitope surface map — {pdb}]({viz})")
        elif not reasoning.get("available"):
            rnote = reasoning.get("note")
            if rnote:
                # Fenced block preserves the multi-line container traceback/stderr (the REAL
                # reason) AND prevents any ``#``/``>`` in it from injecting document structure.
                safe = str(rnote).replace("```", "ʼʼʼ")
                lines += [
                    "",
                    "> **Surface-exposure (SASA) assessment unavailable** — underlying reason:",
                    "",
                    "```",
                    safe,
                    "```",
                ]
    return "\n".join(lines)


def render_sources_section(bundle: dict[str, Any]) -> str:
    """Deterministic ``## Sources and evidence`` — every retrieved record that
    carries a citable id, rendered as its inline-citation token + title + a
    one-line descriptor.

    Built in CODE (never the LLM) so the evidence ledger is immune to LLM omission:
    even if the narrative skips a source, the record is still listed here with the
    same token the narrative would have cited. Records with no citable id are skipped
    (they cannot be referenced); when nothing is citable the section is still present
    with an explicit, honest line.
    """
    lines: list[str] = [_SOURCES_HEADING, ""]
    entries: list[str] = []

    for p in bundle.get("publications") or []:
        if not isinstance(p, dict):
            continue
        doi = p.get("doi")
        if not doi or not str(doi).startswith("10."):
            continue
        title = p.get("title") or "(untitled)"
        meta: list[str] = []
        authors = p.get("authors")
        if isinstance(authors, list) and authors:
            meta.append(", ".join(str(a) for a in authors[:2]))
        elif isinstance(authors, str) and authors:
            meta.append(authors)
        if p.get("year"):
            meta.append(str(p["year"]))
        if p.get("journal") or p.get("publisher"):
            meta.append(str(p.get("journal") or p.get("publisher")))
        desc = " · ".join(meta) if meta else "publication"
        entries.append(_collapse_ws(f"- **[{doi}]** *{title}* — {desc}"))

    for g in bundle.get("bvbrc_genomes") or []:
        if not isinstance(g, dict):
            continue
        gid = g.get("genome_id") or g.get("id")
        if not gid:
            continue
        name = g.get("genome_name") or g.get("name") or "(unnamed genome)"
        host = g.get("host_name") or g.get("host") or ""
        desc = f"host: {host}" if host else "BV-BRC genome"
        entries.append(_collapse_ws(f"- **[BV-BRC genome {gid}]** *{name}* — {desc}"))

    for m in bundle.get("violin_mappings") or []:
        if not isinstance(m, dict):
            continue
        sid = m.get("synonym_id") or m.get("id")
        if not sid:
            continue
        canon = m.get("canonical_term") or m.get("canonical") or "(unmapped)"
        q = m.get("query_term") or m.get("query") or ""
        desc = f"`{q}` → `{canon}`" if q else f"canonical `{canon}`"
        entries.append(_collapse_ws(f"- **[VIOLIN {sid}]** *{canon}* — {desc}"))

    # RAG chunks numbered #1..#N over the chunks that carry text, matching the
    # synthesizer's own numbering (it enumerates the surviving, text-bearing list).
    n_rag = 0
    for c in bundle.get("rag_chunks") or []:
        if not isinstance(c, dict):
            continue
        text = (c.get("text") or "").strip()
        if not text:
            continue
        n_rag += 1
        src = c.get("source") or c.get("id") or "RAG corpus"
        snippet = text[:80].replace("\n", " ")
        entries.append(_collapse_ws(f"- **[RAG chunk #{n_rag}]** *{src}* — {snippet}…"))

    for h in bundle.get("globus_results") or []:
        if not isinstance(h, dict):
            continue
        # Two record shapes coexist in globus_results: the flat projected shape
        # (_summarize_record: title/subjects/subject/identifiers at top level) and the
        # globus_search {subject, content} shape. Resolve each field from whichever
        # carries it, so harmonized records (which had NO `subject` and were silently
        # dropped here) now render with their concrete object identifiers.
        content = h.get("content") or {}
        subject = h.get("subject") or datacite_primary_id(content)
        if not subject or not isinstance(subject, str):
            continue
        title = h.get("title") or datacite_title(content) or "(untitled)"
        subjects = h.get("subjects") or datacite_subjects(content)
        identifiers = h.get("identifiers") or datacite_identifiers(content)
        structural_source = h.get("structural_source")
        # Concrete object IDs beyond the citation token already shown (PDB/GenBank/…).
        id_bits: list[str] = []
        for id_type in _GLOBUS_ID_TYPES:
            for v in (identifiers.get(id_type) or [])[:2]:
                tok = f"{id_type}:{v}"
                if tok != subject and tok not in id_bits:
                    id_bits.append(tok)
        desc_parts: list[str] = []
        if structural_source:
            desc_parts.append(f"{structural_source} structure")
        elif subjects:
            desc_parts.append(", ".join(subjects[:4]))
        if id_bits:
            desc_parts.append(", ".join(id_bits[:6]))
        desc = " · ".join(desc_parts) or "harvested-corpus record"
        entries.append(_collapse_ws(f"- **[Globus {subject}]** *{title}* — {desc}"))

    if not entries:
        lines.append("_No retrieved records carried a citable identifier for this query._")
    else:
        lines.extend(entries)
    return "\n".join(lines)


_DISCLOSURE_HEADING = "## Data actually used"


def render_provenance_disclosure_section(bundle: dict[str, Any]) -> str:
    """Render the ``## Data actually used`` section — full disclosure of WHICH sequences and
    structures the analysis actually consumed (not just counts). Pure + LLM-free; ALWAYS
    returns a non-empty section (degrade-loud when a leg produced nothing).

    Sequences: fetched-vs-used counts + the per-strain identities aligned (genome_name + id).
    Structures: the structure selected for SASA + why, and every candidate analyzed/rejected
    with its chain, exposed-residue count, and rejection reason.
    """
    lines = [_DISCLOSURE_HEADING, ""]

    # --- Sequences used -------------------------------------------------------------------
    lines.append("### Sequences used")
    records = bundle.get("sequence_used_records")
    summary = bundle.get("sequence_fetch_summary") or {}
    if isinstance(records, list) and records:
        n_used = summary.get("n_used") or len(records)
        n_fetched = summary.get("n_fetched")
        n_dropped = summary.get("n_dropped_length_outlier")
        aligner = summary.get("aligner") or "the aligner"
        ver = summary.get("aligner_version")
        head = f"Aligned **{n_used}** per-strain {_collapse_ws(bundle.get('protein') or 'protein')} sequence(s)"
        if n_fetched:
            head += f" (of {n_fetched} fetched from BV-BRC"
            if n_dropped:
                head += f"; {n_dropped} dropped as length outliers before alignment"
            head += ")"
        head += f" with {aligner}{f' {ver}' if ver else ''}."
        lines.append(head)
        lines.append("")
        cap = 30
        for r in records[:cap]:
            if not isinstance(r, dict):
                continue
            strain = _collapse_ws(r.get("genome_name") or "(unnamed strain)")
            acc = _collapse_ws(r.get("id") or "")
            lines.append(f"- {strain}{f' — `{acc}`' if acc else ''}")
        if len(records) > cap:
            lines.append(f"- …and {len(records) - cap} more strain(s).")
    else:
        note = bundle.get("sequence_conservation_note") or (
            "No per-strain sequences were aligned for this query (sequence conservation "
            "unavailable — see the Analysis steps)."
        )
        lines.append(f"> {_collapse_ws(note)}")

    # Alignment-conservation visualization: the PNG when rendered (matplotlib + data present),
    # else the dependency-free inline text track (the degrade-loud floor) — never a broken image.
    viz_art = bundle.get("alignment_viz_artifact")
    viz_text = bundle.get("alignment_viz_text")
    if viz_art:
        prot = _collapse_ws(bundle.get("protein") or "protein")
        lines += ["", f"![Sequence conservation — {prot}]({viz_art})"]
    elif viz_text:
        lines += ["", viz_text]

    # --- Structures used ------------------------------------------------------------------
    lines += ["", "### Structures used"]
    reasoning = bundle.get("structural_reasoning")
    if isinstance(reasoning, dict) and reasoning.get("available"):
        sel = reasoning.get("selection") if isinstance(reasoning.get("selection"), dict) else {}
        why = "; ".join(sel.get("reasons") or []) or "best-ranked loadable structure"
        considered = sel.get("considered")
        n_analyzed = reasoning.get("n_analyzed_structures")
        lines.append(
            f"Selected **{sel.get('pdb_id') or reasoning.get('pdb_id')}** for SASA from "
            f"{considered if considered is not None else 'the'} candidate(s) "
            f"({_collapse_ws(why)}); analyzed {n_analyzed or 1} structure(s) for corroboration."
        )
        analyzed = reasoning.get("analyzed_structures")
        if isinstance(analyzed, list) and analyzed:
            lines.append("")
            for a in analyzed:
                if not isinstance(a, dict):
                    continue
                pdb = a.get("pdb_id") or "(unknown)"
                used = a.get("available")
                chain = a.get("chain")
                if used:
                    lines.append(
                        f"- **{pdb}** — used (chain {chain or '?'}, "
                        f"{a.get('n_exposed', 0)} exposed / {a.get('n_buried', 0)} buried)"
                    )
                else:
                    reason = _collapse_ws(a.get("note") or "not analyzed")
                    lines.append(f"- **{pdb}** — rejected: {reason}")
    else:
        note = bundle.get("structural_note") or (
            "No structure was analyzed for surface exposure (structural reasoning unavailable "
            "— see the Analysis steps)."
        )
        lines.append(f"> {_collapse_ws(note)}")

    return "\n".join(lines)


def render_analysis_steps_section(bundle: dict[str, Any]) -> str:
    """Deterministic ``## Analysis steps`` — the full pipeline progression, ordered.

    A PROMINENT, top-level rendering of the stage reports (resolve → all-9 harmonized search →
    assemble → data-readiness → structural → sequence → rhea → reasoning → functional →
    distill), so the reader sees the progression of steps the run went through — not buried in
    a sub-section. Reuses ``render_stage_reports`` (ordered by each report's ``order``).
    """
    return f"{_ANALYSIS_HEADING}\n\n{render_stage_reports(bundle)}"


def render_coverage_section(bundle: dict[str, Any]) -> str:
    """Deterministic ``## Evidence coverage`` — what each source contributed.

    Globus rows are driven from ``harmonized_search_summary`` so EVERY one of the (mandatory)
    9 destination indices appears — even an index that returned nothing — each shown as
    ``available`` (how many exist for this query in the index) vs ``used`` (how many were
    retrieved into the corpus). Searching all indices is mandatory, so showing all 9 makes
    that verifiable and distinguishes "searched, 0 records" from "not searched". RAG + PubMed
    come from the ``data_readiness`` counts. ``used`` is retrieval breadth (kept), distinct
    from the distillation digest (top-N the LLM actually reasons over).

    Always present; degrades to an explicit line when nothing was recorded.
    """
    lines: list[str] = [_COVERAGE_HEADING, ""]
    summary = bundle.get("harmonized_search_summary")
    dr = bundle.get("data_readiness")
    counts = dr.get("counts") if isinstance(dr, dict) else {}
    counts = counts if isinstance(counts, dict) else {}

    rendered_anything = False

    # Globus indices — ALL of them, available vs used.
    if isinstance(summary, dict):
        names = summary.get("index_names")
        if not isinstance(names, list) or not names:
            from apecx_integration.composition.steps.harmonized_search_execute_step import (
                _INDEX_UUIDS,
            )

            names = sorted(_INDEX_UUIDS)
        available = summary.get("per_index_available") or {}
        kept = summary.get("per_index_kept") or {}
        lines.append(f"Globus indices (all {len(names)} searched — mandatory):")
        for name in names:
            a = int(available.get(name, 0))
            u = int(kept.get(name, 0))
            marker = "" if a else "  _(searched, no records)_"
            lines.append(f"- **{name}**: {a} available / {u} used{marker}")
        lines.append("")
        rendered_anything = True

    # RAG + PubMed (retrieval counts from data_readiness).
    other = [("rag_chunks", "RAG chunks"), ("publications", "publications (PubMed)")]
    other_lines = [f"- **{label}**: {int(counts[key])}" for key, label in other if key in counts]
    if other_lines:
        lines.append("Other sources:")
        lines.extend(other_lines)
        lines.append("")
        rendered_anything = True

    if not rendered_anything:
        lines.append("_No per-source coverage was recorded for this query._")
        return "\n".join(lines)

    globus_used = (
        sum(int(v) for v in (summary.get("per_index_kept") or {}).values())
        if isinstance(summary, dict)
        else 0
    )
    other_used = sum(int(counts[k]) for k, _ in other if k in counts)
    lines.append(
        f"_Total records retrieved across sources: {globus_used + other_used} "
        f"(the distillation stage ranks these and keeps a top-N digest for synthesis)._"
    )
    return "\n".join(lines)


def render_followups_section(query: str, bundle: dict[str, Any]) -> str:
    """Deterministic ``## Follow-up questions`` — 3–5 templated questions seeded from
    the query and any NAMED coverage gaps (a structural no-hit/outage, an empty
    publication branch, empty genomic/ontology branches).

    No LLM: templated so the section is always present, always relevant to the actual
    coverage of this run, and never confabulated. Gap-seeded questions come first
    (most actionable), then query-seeded questions fill to the 3–5 band.
    """
    # Sanitize before interpolating into the seed questions: an un-sanitized query with
    # embedded newlines + `## ...` headers would inject fake contract sections (the query
    # is user-controlled). _sanitize_inline collapses it to one safe line.
    q = _sanitize_inline((query or "").rstrip("?"))
    questions: list[str] = []

    note = bundle.get("structural_note")
    structural_records = bundle.get("structural_records") or []
    if note and not structural_records:
        questions.append(
            "No PDB/EMDB structural records were found — would expanding the structure "
            "search (alternative protein/antigen names, related taxa, or an EMDB-only "
            "pass) surface relevant structures?"
        )

    if not (bundle.get("publications") or []):
        questions.append(
            "No publications were retrieved — would a broader PubMed query (synonyms, a "
            "wider date range) surface relevant primary literature?"
        )

    if not (bundle.get("bvbrc_genomes") or []) and not (bundle.get("violin_mappings") or []):
        questions.append(
            "No BV-BRC genomes or VIOLIN mappings matched — would normalizing the entity "
            "names against the synonym dictionary improve database coverage?"
        )

    # Query-seeded fillers (always available → guarantees the >=3 floor).
    seeds = [
        f"What additional experimental evidence would most strengthen the answer to: {q}?",
        f"Which conflicting or ambiguous findings about {q} warrant a targeted follow-up search?",
        f"What is the next mechanistic question raised by the current evidence on {q}?",
    ]
    for s in seeds:
        if len(questions) >= 5:
            break
        questions.append(s)

    questions = questions[:5]
    lines = [_FOLLOWUPS_HEADING, ""]
    lines.extend(f"{i}. {qn}" for i, qn in enumerate(questions, 1))
    return "\n".join(lines)


def _ensure_contract_headers(narrative: str) -> str:
    """Deterministically GUARANTEE the three narrative contract headings exist, in
    order (``# Answer`` → ``## Cross-data reasoning`` → ``## Integrated insight``).

    The LLM is asked to emit these, but a small model sometimes omits one — which
    would silently violate the output contract. Rather than trust the LLM, we
    inject any missing heading with a LOUD, honest degrade note (never fabricated
    reasoning). Combined with the deterministic Sources/Follow-ups/Structural
    sections, this makes a missing contract section impossible by construction —
    the contract no longer depends on LLM behavior.
    """
    text = narrative.strip()
    if _ANSWER_HEADING not in text:
        # No structure at all → the whole body is the answer.
        text = (
            f"{_ANSWER_HEADING}\n\n{text}"
            if text
            else f"{_ANSWER_HEADING}\n\n_No answer was produced._"
        )
    if _CROSSDATA_HEADING not in text:
        note = (
            f"{_CROSSDATA_HEADING}\n\n_The model did not emit a distinct cross-data "
            "reasoning section; see the reasoning trace below and the Sources section._"
        )
        insight_idx = text.find(_INSIGHT_HEADING)
        if insight_idx != -1:
            text = f"{text[:insight_idx].rstrip()}\n\n{note}\n\n{text[insight_idx:].lstrip()}"
        else:
            text = f"{text.rstrip()}\n\n{note}"
    if _INSIGHT_HEADING not in text:
        text = (
            f"{text.rstrip()}\n\n{_INSIGHT_HEADING}\n\n_The model did not emit a distinct "
            "integrated-insight section; the integrated view is the combination of the "
            "evidence and reasoning above._"
        )
    return text


def _insert_scope_caveat_if_unresolved(body: str, bundle: dict[str, Any]) -> str:
    """When NO viral species was resolved (``taxon_id`` is None — neither
    caller-supplied nor name-resolved), surface a LOUD scope caveat as the FIRST
    prose under ``# Answer``.

    Why this exists: the Structural section already names the ``not taxon-locked``
    limitation, but it sits BELOW the Answer + Cross-data reasoning — a user with a
    typo'd or non-viral query (e.g. ``human insulin protein structure``) reads a
    confident, authoritative-looking answer first and never reaches the caveat. That
    is a silent scope failure: the document reads as a viral epitope review when the
    virus-specific legs (sequence-conservation epitope mapping, taxon-locked structure
    selection) never ran. Keying off ``taxon_id is None`` — the same signal
    ``collect_provenance`` records as ``inputs.taxon_id`` — fires ONLY on the
    unresolved case (a caller-supplied or name-resolved taxon is always set), so a
    genuine viral query is never mislabeled. ``# Answer`` stays the first heading
    (output contract intact); the caveat is the first line under it.
    """
    if bundle.get("taxon_id") is not None:
        return body
    caveat = (
        "> ⚠️ **Scope caveat — no viral species resolved.** This query did not map to "
        "a virus taxon, so the virus-specific analyses (sequence-conservation epitope "
        "mapping, taxon-locked structure selection) did not run. What follows is "
        "taxon-agnostic structural and literature evidence only; treat any epitope or "
        "conservation claim as unverified for a specific virus — see the Structural "
        "evidence section for the resolution detail."
    )
    idx = body.find(_ANSWER_HEADING)
    if idx == -1:
        # No Answer heading (impossible post _ensure_contract_headers) — prepend rather
        # than drop the caveat.
        return f"{_ANSWER_HEADING}\n\n{caveat}\n\n{body.lstrip()}"
    after_heading = idx + len(_ANSWER_HEADING)
    rest = body[after_heading:].lstrip("\n")
    return f"{body[:after_heading]}\n\n{caveat}\n\n{rest}"


def collect_structured_output(bundle: dict[str, Any]) -> dict[str, Any]:
    """The STRUCTURED scientific result (not prose) — emitted alongside the markdown so the
    caller gets machine-readable data, surfaced as the WorkflowResult ``data_preview`` and
    written to the run's durable ``.json`` artifact. Shape: a ``DataShape`` bundle
    (``{"kind": "bundle", "parts": {...}}``) the terminal EnvelopeStep turns into data_preview.

    Carries the actual analysis the workflow computed and previously THREW AWAY by rendering
    only prose: the resolved entity, the sequence-conservation regions (MAFFT), the structural
    records with per-residue SASA exposed/buried (PyMOL), and the cited publications. Lightweight
    by construction (ids + summaries, not raw FASTA/structures) so it serializes cleanly."""

    def _pub(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "doi": p.get("doi") or p.get("id") or p.get("pmid"),
            "title": p.get("title"),
            "year": p.get("year"),
        }

    parts: dict[str, Any] = {
        "query": bundle.get("query"),
        "taxon_id": bundle.get("taxon_id"),
        "protein": bundle.get("protein"),
        # sequence-conservation leg (MAFFT → conserved positions/regions across strains)
        "conserved_regions": bundle.get("conserved_regions") or bundle.get("conserved_sites") or [],
        # structural leg (PDB/EMDB records, each carrying its SASA exposed/buried classification
        # from the containerized PyMOL reasoning step when present)
        "structural_records": bundle.get("structural_records") or [],
        "structural_note": bundle.get("structural_note"),
        "structural_reasoning": bundle.get("structural_reasoning"),
        "publications": [
            _pub(p) for p in (bundle.get("publications") or []) if isinstance(p, dict)
        ],
        "counts": {
            "conserved_regions": len(
                bundle.get("conserved_regions") or bundle.get("conserved_sites") or []
            ),
            "structural_records": len(bundle.get("structural_records") or []),
            "publications": len(bundle.get("publications") or []),
        },
    }
    return {"kind": "bundle", "parts": parts}


def compose_evidence_markdown(narrative_body: str, query: str, bundle: dict[str, Any]) -> str:
    """Assemble the full contract-shaped evidence document from a narrative body
    (the LLM output on the success path, or ``render_evidence_fallback`` on degrade).

    Order (the contract sections, in order, plus the always-present deterministic sections):

        # Answer · ## Cross-data reasoning · ## Integrated insight ·
        ## Analysis steps · ## Data actually used · ## Structural evidence (PDB / EMDB) ·
        ## Evidence coverage · ## Sources and evidence · ## Follow-up questions

    Analysis-steps + Coverage + Sources + Follow-ups are deterministic, so those sections can
    NEVER be omitted regardless of LLM behavior — a missing section is impossible by
    construction for them. (The former buried ``### Reasoning trace`` sub-section is replaced
    by the prominent top-level ``## Analysis steps`` progression below — same stage reports,
    not duplicated.)
    """
    body = _insert_scope_caveat_if_unresolved(_ensure_contract_headers(narrative_body), bundle)
    analysis = render_analysis_steps_section(bundle)
    disclosure = render_provenance_disclosure_section(bundle)
    structural = render_structural_section(
        bundle.get("structural_records"),
        bundle.get("structural_note"),
        reasoning=bundle.get("structural_reasoning"),
    )
    coverage = render_coverage_section(bundle)
    sources = render_sources_section(bundle)
    followups = render_followups_section(query, bundle)
    return (
        f"{body.rstrip()}\n\n{analysis}\n\n{disclosure}\n\n{structural}\n\n"
        f"{coverage}\n\n{sources}\n\n{followups}\n"
    )


class EvidenceReviewSynthesisStepConfig(StepConfig):
    """Config for EvidenceReviewSynthesisStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    synthesis_config_path: str | None = Field(
        default=None,
        description=(
            "Optional path to a custom synthesis_config.yml. When None the "
            "bundled default is used (same contract as RagSynthesisStep)."
        ),
    )

    evidence_prompt_path: str | None = Field(
        default=None,
        description=(
            "Optional path to a YAML file with a ``system_prompt`` key that "
            "mandates the evidence output contract (# Answer / ## Cross-data "
            "reasoning / ## Integrated insight). Passed as a per-call "
            "``system_prompt_override`` into ``synthesize_response`` so the SHARED "
            "synthesis_config.yml (used by rag_e2e_synthesis) is untouched. When "
            "None the bundled evidence prompt next to this step is used."
        ),
    )


class EvidenceReviewSynthesisStep(BaseStep):
    COMPONENT_TYPE: str = "evidence_review_synthesis_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]
    # This step's LLM use is the FINAL narrative synthesis: in desktop locus it omits the
    # call and the host synthesizes (see process()). The run-time requires_llm gate reads
    # this so it does NOT refuse the workflow on a desktop with no apecx LLM configured.
    LLM_ROLE: str = "final_synthesis"

    @classmethod
    def _get_config_class(cls):
        return EvidenceReviewSynthesisStepConfig

    @classmethod
    def extract_component_config(cls, config: EvidenceReviewSynthesisStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "synthesis_config_path": getattr(config, "synthesis_config_path", None),
            "evidence_prompt_path": getattr(config, "evidence_prompt_path", None),
        }

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        from apecx_integration.agents.rag_synthesis import SynthesisConfig

        path = component_config.get("synthesis_config_path")
        if path is None:
            self._synthesis_config: SynthesisConfig | None = None
        else:
            p = Path(path)
            if not p.is_file():
                raise FileNotFoundError(
                    f"EvidenceReviewSynthesisStep: synthesis_config_path {p} does not "
                    f"exist or is not a file."
                )
            import yaml

            self._synthesis_config = SynthesisConfig.model_validate(
                yaml.safe_load(p.read_text(encoding="utf-8"))
            )

        # Evidence output-contract system prompt. Loaded at init (fail-fast on a
        # missing/blank prompt) and passed per-call as ``system_prompt_override`` so
        # the SHARED synthesis_config.yml keeps its minimal, no-sections prompt for
        # the general rag_e2e_synthesis pipeline.
        import yaml as _yaml

        prompt_path = component_config.get("evidence_prompt_path")
        pp = Path(prompt_path) if prompt_path else Path(__file__).parent / _DEFAULT_PROMPT_FILENAME
        if not pp.is_file():
            raise FileNotFoundError(
                f"EvidenceReviewSynthesisStep: evidence prompt file {pp} does not exist "
                f"or is not a file."
            )
        prompt_doc = _yaml.safe_load(pp.read_text(encoding="utf-8")) or {}
        system_prompt = prompt_doc.get("system_prompt") if isinstance(prompt_doc, dict) else None
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError(
                f"EvidenceReviewSynthesisStep: evidence prompt file {pp} must carry a "
                f"non-empty 'system_prompt' string."
            )
        self._evidence_system_prompt: str = system_prompt

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"EvidenceReviewSynthesisStep '{self.name}': input_data must be a dict, "
                f"got {type(input_data).__name__}"
            )
        if (
            _INPUT_KEY in input_data
            and isinstance(input_data[_INPUT_KEY], dict)
            and "query" not in input_data
        ):
            input_data = input_data[_INPUT_KEY]

        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"EvidenceReviewSynthesisStep '{self.name}': bundle must carry a non-empty "
                f"'query' string; got {type(query).__name__}={query!r}"
            )

        # The SERVER ALWAYS writes the finished markdown report — in BOTH loci (the desktop
        # "defer to the host" scaffold was removed 2026-06-15: it produced no durable artifact
        # and the host LLM discarded it, so the user saw nothing). Synthesize the narrative with
        # the configured LLM when one is reachable; degrade LOUD to a deterministic body when not
        # (no hard LLM requirement → the workflow still runs in desktop with no Ollama). The
        # markdown is the user-facing deliverable; ``data`` carries the structured result.
        from apecx_integration.agents.rag_synthesis import synthesize_response

        # The bundle's source lists ARE the distilled top-N at this point: the upstream
        # EvidenceDistillationStep ranks the (unbounded) retrieved corpus and REPLACES
        # each source list with its quality-ranked top-N, so the LLM reasons over the
        # digest, never the raw corpus. (Standalone review with no distill step upstream
        # — e.g. tests — simply uses whatever was passed.)
        self.emit_progress("composing evidence review (LLM)")
        try:
            evidence_md = await asyncio.to_thread(
                synthesize_response,
                query.strip(),
                config=self._synthesis_config,
                # Evidence output-contract prompt (# Answer / ## Cross-data reasoning /
                # ## Integrated insight). Overrides ONLY the system message; all gates
                # stay sourced from the shared config.
                system_prompt_override=self._evidence_system_prompt,
                rag_chunks=input_data.get("rag_chunks") or [],
                bvbrc_genomes=input_data.get("bvbrc_genomes") or [],
                violin_mappings=input_data.get("violin_mappings") or [],
                publications=input_data.get("publications") or [],
                globus_results=input_data.get("globus_results") or [],
            )
        except Exception as exc:
            # RELIABILITY: a narrative-synthesis failure (e.g. the strict
            # citation-grounding gate rejecting a backtick-wrapped real ID, an
            # empty-retrieval gate, or an LLM outage) must NOT discard the
            # retrieved evidence. Degrade LOUD to a deterministic narrative body
            # that names the reason and lists what was retrieved — still emitting
            # the three contract headings so the document stays five-section shaped.
            reason = f"{type(exc).__name__}: {exc}"
            log.warning(
                "EvidenceReviewSynthesisStep %s: narrative synthesis failed (%s); "
                "degrading to deterministic evidence summary.",
                self.name,
                reason,
            )
            evidence_md = render_evidence_fallback(
                query.strip(), input_data.get("publications"), reason
            )

        # Assemble the full contract-shaped document. Sources + Follow-ups are
        # deterministic, so the five-section contract holds on BOTH the success and
        # the degrade-loud path — a missing section is impossible by construction.
        full_md = compose_evidence_markdown(evidence_md, query.strip(), input_data)

        # E3-8: collect the per-run provenance record HERE — this is the last step that
        # still holds the full bundle (downstream only the rendered markdown survives). It
        # is threaded review → gate → envelope into WorkflowResult.provenance. Pure + never
        # raises (CC-2): a provenance failure must not strand a run whose science completed.
        from apecx_integration.composition.steps._evidence_provenance import collect_provenance

        provenance = collect_provenance(input_data)

        log.info(
            "EvidenceReviewSynthesisStep %s: evidence=%d chars, full=%d chars, "
            "structural_records=%d, stage_reports=%d",
            self.name,
            len(evidence_md),
            len(full_md),
            len(input_data.get("structural_records") or []),
            len(input_data.get("stage_reports") or []),
        )
        return {
            "markdown": full_md,
            "data": collect_structured_output(input_data),
            "provenance": provenance,
        }
