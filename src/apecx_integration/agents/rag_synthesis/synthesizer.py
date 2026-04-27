"""LLM synthesis with retrieved RAG chunks + structured DB context.

Produces free-form Markdown with inline citations. The synthesis
prompt and rendering rules are configurable via a YAML file
(``SynthesisConfig``); local-LLM deployments work out of the box
because the LLM client is built from the project-wide
``APECX_LLM_*`` env-var contract.

Architectural intent (user directive 2026-04-27):
  "LLM should synthesize non-trivial responses combining BV-BRC +
   VIOLIN + RAG semantic chunks."

This module is the single seam where those three data sources meet
the LLM. Separation of concerns:

  - Retrieval (FAISS over RAG semantic chunks) lives in
    ``nanobrain.lightweight.component_index.ComponentIndex`` and
    its loaders.
  - Structured DB lookup (BV-BRC genome data, VIOLIN cached
    mappings) lives in ``apecx_integration.agents.violin_bvbrc``
    and the workflow steps that wrap it.
  - Publication metadata lives in ``apecx-harvesters`` (DataCite
    shape). The current shim accepts caller-supplied dicts; a
    future harvester step will load them automatically.

This module ONLY builds the prompt + invokes the LLM + returns
Markdown. It does no retrieval or DB lookup of its own — the
caller is responsible for assembling the inputs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


_THIS_DIR = Path(__file__).resolve().parent
DEFAULT_SYNTHESIS_CONFIG_PATH = _THIS_DIR / "synthesis_config.yml"


class SynthesisConfig(BaseModel):
    """Tunable synthesis behavior.

    Loaded from ``synthesis_config.yml`` by default; operators
    override per-deployment by editing the YAML or passing an
    instance directly to ``synthesize_response``.
    """

    system_prompt: str = Field(
        ...,
        description=(
            "The system message that governs synthesis style, citation "
            "format, and tone. Operators can rewrite per institution."
        ),
    )
    max_rag_chunks: int = Field(
        default=8, ge=1,
        description=(
            "Cap on the number of retrieved RAG chunks fed to the LLM. "
            "Higher = more context but slower / more expensive. 8 is a "
            "reasonable default for mistral-nemo at 4K context."
        ),
    )
    max_bvbrc_genomes: int = Field(
        default=5, ge=0,
        description="Cap on BV-BRC genome rows surfaced to the LLM.",
    )
    max_violin_mappings: int = Field(
        default=20, ge=0,
        description="Cap on VIOLIN mappings surfaced to the LLM.",
    )
    max_publications: int = Field(
        default=5, ge=0,
        description="Cap on harvester publications surfaced to the LLM.",
    )
    require_inline_citations: bool = Field(
        default=True,
        description=(
            "Hard-fail when the LLM response carries no inline citation "
            "markers. The synthesis is supposed to ground every claim "
            "in retrieved data; an uncited paragraph is a soft-failure "
            "shape this flag promotes to a hard error."
        ),
    )
    citation_marker_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\[BV-BRC genome ",
            r"\[VIOLIN ",
            r"\[RAG chunk #",
            r"\[10\.",
        ],
        description=(
            "Regex patterns the validator looks for in LLM output. "
            "When ``require_inline_citations`` is True, the response "
            "must contain at least one match across all patterns."
        ),
    )


def _load_default_config() -> SynthesisConfig:
    """Load the bundled synthesis config from disk. Loaded lazily so
    a missing/malformed YAML at module import time doesn't break
    callers that pass their own config."""
    if not DEFAULT_SYNTHESIS_CONFIG_PATH.is_file():
        raise RuntimeError(
            f"Synthesis config not found at {DEFAULT_SYNTHESIS_CONFIG_PATH}. "
            "This is the bundled default; if missing, the package is "
            "incomplete."
        )
    raw = yaml.safe_load(
        DEFAULT_SYNTHESIS_CONFIG_PATH.read_text(encoding="utf-8")
    )
    return SynthesisConfig.model_validate(raw)


def _render_rag_chunks(chunks: Iterable[dict[str, Any]], cap: int) -> str:
    """Render retrieved RAG chunks for inclusion in the user prompt.

    Each chunk is a dict with at least a ``text`` field; optional
    ``id``, ``source``, ``score`` are surfaced when present so the
    LLM can cite them precisely.
    """
    lines: list[str] = []
    for i, chunk in enumerate(list(chunks)[:cap], start=1):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        cid = chunk.get("id")
        source = chunk.get("source")
        score = chunk.get("score")
        header_parts = [f"### RAG chunk #{i}"]
        if cid:
            header_parts.append(f"id={cid}")
        if source:
            header_parts.append(f"source={source}")
        if score is not None:
            header_parts.append(f"similarity={score:.3f}")
        lines.append(" — ".join(header_parts))
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() if lines else "(no RAG chunks retrieved)"


def _render_bvbrc_genomes(genomes: Iterable[dict[str, Any]], cap: int) -> str:
    """Render BV-BRC genome rows in a Markdown-friendly shape."""
    lines: list[str] = []
    for i, g in enumerate(list(genomes)[:cap], start=1):
        gid = g.get("genome_id") or g.get("id") or "?"
        name = g.get("genome_name") or g.get("name") or "?"
        taxon = g.get("taxon_lineage") or g.get("lineage") or ""
        host = g.get("host_name") or g.get("host") or ""
        bits = [f"- **BV-BRC genome `{gid}`** — {name}"]
        if taxon:
            bits.append(f"  - Taxonomy: {taxon}")
        if host:
            bits.append(f"  - Host: {host}")
        lines.extend(bits)
    return "\n".join(lines) if lines else "(no BV-BRC genomes matched)"


def _render_violin_mappings(mappings: Iterable[dict[str, Any]], cap: int) -> str:
    """Render VIOLIN cached mappings."""
    lines: list[str] = []
    for m in list(mappings)[:cap]:
        q = m.get("query_term") or m.get("query") or "?"
        c = m.get("canonical_term") or m.get("canonical") or "?"
        sid = m.get("synonym_id") or m.get("id") or ""
        conf = m.get("confidence")
        bits = [f"- **VIOLIN mapping**: `{q}` → `{c}`"]
        if sid:
            bits[0] += f" [VIOLIN {sid}]"
        if conf is not None:
            bits.append(f"  - Confidence: {conf}")
        lines.extend(bits)
    return "\n".join(lines) if lines else "(no VIOLIN cached mappings)"


def _render_publications(pubs: Iterable[dict[str, Any]], cap: int) -> str:
    """Render harvester publication metadata.

    Expected keys (DataCite-shaped, all optional):
      - ``doi`` (used as inline citation marker)
      - ``title``, ``authors``, ``year``, ``journal``
      - ``abstract`` or ``description``
    """
    lines: list[str] = []
    for p in list(pubs)[:cap]:
        doi = p.get("doi") or "?"
        title = p.get("title") or "(untitled)"
        authors = p.get("authors") or []
        year = p.get("year") or ""
        journal = p.get("journal") or p.get("publisher") or ""
        abstract = p.get("abstract") or p.get("description") or ""
        bits = [f"- **[{doi}]** *{title}*"]
        meta_parts = []
        if authors:
            if isinstance(authors, list):
                meta_parts.append(", ".join(str(a) for a in authors[:3]))
            else:
                meta_parts.append(str(authors))
        if year:
            meta_parts.append(str(year))
        if journal:
            meta_parts.append(journal)
        if meta_parts:
            bits.append(f"  - {' · '.join(meta_parts)}")
        if abstract:
            bits.append(f"  - {abstract[:300]}")
        lines.extend(bits)
    return "\n".join(lines) if lines else "(no publications)"


def _validate_response_has_citations(
    text: str, patterns: list[str]
) -> bool:
    import re
    return any(re.search(pat, text) for pat in patterns)


def synthesize_response(
    query: str,
    *,
    rag_chunks: Iterable[dict[str, Any]] | None = None,
    bvbrc_genomes: Iterable[dict[str, Any]] | None = None,
    violin_mappings: Iterable[dict[str, Any]] | None = None,
    publications: Iterable[dict[str, Any]] | None = None,
    llm: Any = None,
    config: SynthesisConfig | None = None,
) -> str:
    """Synthesize a free-form Markdown response with inline citations.

    The caller is responsible for assembling the inputs (retrieving
    RAG chunks, looking up BV-BRC genomes, fetching VIOLIN mappings,
    pulling publications). This function renders them into a prompt,
    invokes the LLM, validates the response shape, and returns it.

    Args:
        query: The free-text scientist question.
        rag_chunks: Iterable of dicts ``{text, id, source, score}``
            (id / source / score optional). Capped per ``config.
            max_rag_chunks``.
        bvbrc_genomes: Iterable of BV-BRC genome rows. Capped per
            ``config.max_bvbrc_genomes``.
        violin_mappings: Iterable of VIOLIN cached mapping rows.
            Capped per ``config.max_violin_mappings``.
        publications: Iterable of DataCite-shaped publication
            records. Capped per ``config.max_publications``.
        llm: A LangChain-compatible LLM client (must have an
            ``invoke(messages) -> response`` method whose response
            has a ``content`` attribute). When None, the default
            ``_build_chat_llm`` factory is used.
        config: Synthesis config. When None, the bundled default
            (``synthesis_config.yml``) is loaded.

    Returns:
        The LLM's Markdown response, post-validated to contain at
        least one inline citation marker (when configured).

    Raises:
        ValueError: response carried no inline citation marker
            and ``config.require_inline_citations`` is True.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError(
            f"synthesize_response: query must be a non-empty string, got "
            f"{type(query).__name__}={query!r}"
        )

    cfg = config or _load_default_config()

    rag_block = _render_rag_chunks(rag_chunks or [], cfg.max_rag_chunks)
    bvbrc_block = _render_bvbrc_genomes(
        bvbrc_genomes or [], cfg.max_bvbrc_genomes
    )
    violin_block = _render_violin_mappings(
        violin_mappings or [], cfg.max_violin_mappings
    )
    pubs_block = _render_publications(
        publications or [], cfg.max_publications
    )

    # User prompt = data only. The system prompt declares the role
    # and citation marker shapes (operator-tunable in
    # synthesis_config.yml); the user prompt hands the LLM the
    # query + retrieved context and gets out of the way. Synthesis
    # — depth, length, structure, tone — is the local LLM's job.
    user_prompt = (
        f"Question: {query.strip()}\n\n"
        f"## Retrieved RAG chunks\n\n{rag_block}\n\n"
        f"## BV-BRC genomes\n\n{bvbrc_block}\n\n"
        f"## VIOLIN cached mappings\n\n{violin_block}\n\n"
        f"## Publications\n\n{pubs_block}"
    )

    # Lazy import: tests that pass a stub ``llm`` don't need the heavy
    # langchain stack to load. Canonical factory lives in
    # ``_llm_factory``; importing it here (not at module top) keeps
    # the synthesis package import-cheap for callers that always
    # supply their own LLM.
    if llm is None:
        from apecx_integration.agents._llm_factory import build_chat_llm
        llm = build_chat_llm()

    from langchain_core.messages import HumanMessage, SystemMessage

    response = llm.invoke([
        SystemMessage(content=cfg.system_prompt),
        HumanMessage(content=user_prompt),
    ])
    content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ValueError(
            f"synthesize_response: LLM returned empty/non-string content "
            f"(got {type(content).__name__}={content!r})"
        )

    if cfg.require_inline_citations:
        if not _validate_response_has_citations(
            content, cfg.citation_marker_patterns
        ):
            raise ValueError(
                "synthesize_response: LLM response carries NO inline "
                "citation marker. The response should ground every "
                "claim in retrieved data. Disable "
                "``require_inline_citations`` in the synthesis config "
                "if this validation is too strict for your deployment, "
                "but the default policy is fail-fast.\n\n"
                f"Response was:\n{content}"
            )

    return content
