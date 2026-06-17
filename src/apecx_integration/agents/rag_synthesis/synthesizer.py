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
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from apecx_integration.agents.globus_search._datacite import (
    datacite_description,
    datacite_subjects,
    datacite_title,
)

logger = logging.getLogger(__name__)


_THIS_DIR = Path(__file__).resolve().parent
DEFAULT_SYNTHESIS_CONFIG_PATH = _THIS_DIR / "synthesis_config.yml"


class SynthesisConfig(BaseModel):
    """Tunable synthesis behavior.

    Loaded from ``synthesis_config.yml`` by default; operators
    override per-deployment by editing the YAML or passing an
    instance directly to ``synthesize_response``.

    ``extra='forbid'`` — a typo in the YAML (e.g.
    ``max_rag_chuncks: 8``) raises at config-load time instead of
    silently using the schema default. Probe 955 (batch 36) found
    the silent-acceptance shape and this commit closes it.
    """

    model_config = ConfigDict(extra="forbid")

    system_prompt: str = Field(
        ...,
        min_length=1,
        description=(
            "The system message that governs synthesis style, citation "
            "format, and tone. Operators can rewrite per institution. "
            "Must be non-empty; a blank value would produce an empty "
            "LLM system message and is almost certainly a YAML typo."
        ),
    )
    max_rag_chunks: int = Field(
        default=8,
        ge=1,
        description=(
            "Cap on the number of retrieved RAG chunks fed to the LLM. "
            "Higher = more context but slower / more expensive. 8 is a "
            "reasonable default for mistral-nemo at 4K context."
        ),
    )
    max_bvbrc_genomes: int = Field(
        default=5,
        ge=0,
        description="Cap on BV-BRC genome rows surfaced to the LLM.",
    )
    max_violin_mappings: int = Field(
        default=20,
        ge=0,
        description="Cap on VIOLIN mappings surfaced to the LLM.",
    )
    max_publications: int = Field(
        default=5,
        ge=0,
        description="Cap on harvester publications surfaced to the LLM.",
    )
    max_globus_results: int = Field(
        default=10,
        ge=0,
        description=(
            "Cap on Globus Search hits (from the APECx harvested-corpus "
            "index) surfaced to the LLM. Set to 0 to omit the Globus "
            "section from the prompt entirely."
        ),
    )
    abstract_max_chars: int = Field(
        default=300,
        ge=0,
        description=(
            "Per-publication / per-Globus-record abstract length (chars) rendered into the LLM "
            "prompt. The 300 default is a terse summary; raise it (the evidence-synthesis config "
            "uses a larger value) so the model performs REAL literature analysis over near-full "
            "abstracts rather than reasoning off titles alone. 0 omits abstracts entirely."
        ),
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
            r"\[BV-BRC genome [^\]\s\[]+\]",
            r"\[VIOLIN [^\]\s\[]+\]",
            r"\[RAG chunk #\d+\]",
            r"\[10\.[0-9]+/[^\]\s\[]+\]",
            r"\[Globus [^\]\s\[]+\]",
        ],
        description=(
            "Regex patterns matching INLINE CITATION TOKENS in LLM "
            "output. Each pattern matches a complete citation (not a "
            "prefix) so the validator can extract distinct citation "
            "tokens — not just count whether ANY pattern matches "
            "anywhere. The inner character class ``[^\\]\\s\\[]+`` "
            "excludes closing bracket, whitespace, AND opening bracket "
            "to prevent the greedy-match-across-tokens shape probe 1066 "
            "(batch 40, 2026-04-27) surfaced: an interrupted citation "
            "``[10.1234/abc...`` followed later by ``[10.5/y]`` would "
            "otherwise match ``[10.1234/abc... and also [10.5/y]`` as "
            "one token, silently swallowing a legitimate later citation "
            "and accepting a malformed earlier one. DOIs / IDs / "
            "ontology IDs do not contain whitespace or unescaped "
            "brackets, so excluding them is safe."
        ),
    )
    min_response_chars: int = Field(
        default=200,
        ge=0,
        description=(
            "Minimum response length (characters) below which the "
            "response is rejected as curtailed. Local LLMs occasionally "
            "return one-line responses like ``See above.`` that satisfy "
            "every citation rule but ship no value. The user directive "
            "(2026-04-27) calls out non-trivial, not-curtailed responses "
            "as a hard requirement; 200 chars is the floor. Set to 0 to "
            "disable (e.g., for unit tests with deliberately short "
            "fixtures)."
        ),
    )
    min_distinct_citations: int = Field(
        default=1,
        ge=0,
        description=(
            "Minimum count of DISTINCT inline citation tokens. Above 1 "
            "forces multi-source grounding (the LLM cannot cite one "
            "source N times to satisfy the rule). The validator extracts "
            "all matches across all patterns, deduplicates by exact "
            "token text, and counts. Default 1 preserves prior "
            "behavior; 2+ is recommended for production deployments "
            "where the retrieval surface always populates >1 source."
        ),
    )
    fail_on_empty_retrieval: bool = Field(
        default=True,
        description=(
            "When True, fail-fast BEFORE invoking the LLM if every "
            "retrieval input (RAG chunks, BV-BRC genomes, VIOLIN "
            "mappings, publications) is empty / None. The synthesis "
            "is grounded in retrieved data; with no data, the LLM can "
            "only confabulate and the citation validator will fail "
            "downstream with a confusing error pointing at the wrong "
            "cause. This flag promotes the failure to its true root."
        ),
    )
    strict_input_validation: bool = Field(
        default=True,
        description=(
            "When True, reject input rows missing essential fields "
            "(genome_id / canonical_term / chunk text) BEFORE rendering. "
            "Closes a silent-failure shape: a BV-BRC row with no "
            "genome_id rendered as ``[BV-BRC genome ?]`` would let the "
            "LLM cite ``?`` and pass validation with a meaningless "
            "citation. Operators with dirty data can disable by setting "
            "this to False; the renderer then falls back to skipping "
            "the row with a logger.warning."
        ),
    )
    validate_citations_against_inputs: bool = Field(
        default=True,
        description=(
            "When True, every distinct citation token the LLM emits "
            "MUST appear in the set of tokens the renderers built from "
            "the input data. This closes the last silent-failure shape "
            "in citation validation: an LLM that hallucinates a "
            "plausible-looking ID (e.g. ``[BV-BRC genome 99999.99]`` "
            "for a genome that was never in the input) currently passes "
            "the regex-only check because the pattern matches by shape, "
            "not by content. Each renderer reports the set of tokens it "
            "authorizes; the validator unions them and rejects any "
            "extracted token outside the union, naming the offending "
            "tokens and the closest legitimate alternatives. Disable "
            "ONLY for callers that deliberately let the LLM cite data "
            "outside the supplied retrieval bundle (rare; usually a "
            "smell)."
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
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text(encoding="utf-8"))
    return SynthesisConfig.model_validate(raw)


def _reject_or_skip(msg: str, *, strict: bool, kind: str, idx: int) -> bool:
    """Centralize the strict-vs-lenient policy. In strict mode, raise;
    in lenient mode, log a warning and tell the caller to skip.

    Returns True when the caller should skip the row, False when the
    caller should proceed. (Strict mode never returns — it raises.)
    """
    if strict:
        raise ValueError(
            f"synthesize_response: {kind} #{idx} contract violation — "
            f"{msg}. Disable ``strict_input_validation`` in the synthesis "
            f"config if your data pipeline is allowed to surface dirty "
            f"rows; the default is fail-fast so silent-failure shapes "
            f"(LLM citing ``?`` as if it were data) cannot ship."
        )
    logger.warning(
        "rag_synthesis: skipping %s #%d — %s (strict_input_validation=False)",
        kind,
        idx,
        msg,
    )
    return True


def _render_rag_chunks(
    chunks: Iterable[dict[str, Any]], cap: int, *, strict: bool
) -> tuple[str, set[str]]:
    """Render retrieved RAG chunks for inclusion in the user prompt.

    Each chunk is a dict with a non-empty ``text`` field; optional
    ``id``, ``source``, ``score`` are surfaced when present so the
    LLM can cite them precisely. The chunk's *index* in the surviving
    list is what the LLM cites (``[RAG chunk #N]``) — that is why
    rendering uses 1-based ``enumerate`` ON THE FILTERED list, not on
    the input list (so #1 is always the first surviving chunk and
    cannot be a skipped row).

    Returns ``(rendered, allowed_tokens)`` where ``allowed_tokens`` is
    the set of citation tokens this renderer authorizes (e.g.
    ``{"[RAG chunk #1]", "[RAG chunk #2]"}``). The synthesizer's
    citation-grounding validator unions these per-source sets and
    rejects any LLM-emitted token outside the union.
    """
    rendered_lines: list[str] = []
    allowed: set[str] = set()
    surviving = 0
    for i, chunk in enumerate(list(chunks)[:cap]):
        if not isinstance(chunk, dict):
            _reject_or_skip(
                f"expected dict, got {type(chunk).__name__}",
                strict=strict,
                kind="rag_chunk",
                idx=i,
            )
            continue
        text = (chunk.get("text") or "").strip()
        if not text:
            _reject_or_skip(
                "missing or empty ``text`` field",
                strict=strict,
                kind="rag_chunk",
                idx=i,
            )
            continue
        surviving += 1
        allowed.add(f"[RAG chunk #{surviving}]")
        cid = chunk.get("id")
        source = chunk.get("source")
        score = chunk.get("score")
        header_parts = [f"### RAG chunk #{surviving}"]
        if cid:
            header_parts.append(f"id={cid}")
        if source:
            header_parts.append(f"source={source}")
        if score is not None:
            header_parts.append(f"similarity={score:.3f}")
        rendered_lines.append(" — ".join(header_parts))
        rendered_lines.append(text)
        rendered_lines.append("")
    if not rendered_lines:
        return ("(no RAG chunks retrieved)", allowed)
    return ("\n".join(rendered_lines).rstrip(), allowed)


def _render_bvbrc_genomes(
    genomes: Iterable[dict[str, Any]], cap: int, *, strict: bool
) -> tuple[str, set[str]]:
    """Render BV-BRC genome rows. Returns ``(rendered, allowed_tokens)``.

    ``allowed_tokens`` carries one ``[BV-BRC genome <gid>]`` per
    surviving row; the citation-grounding validator uses this to
    reject hallucinated genome IDs.
    """
    rendered_lines: list[str] = []
    allowed: set[str] = set()
    for i, g in enumerate(list(genomes)[:cap]):
        if not isinstance(g, dict):
            _reject_or_skip(
                f"expected dict, got {type(g).__name__}",
                strict=strict,
                kind="bvbrc_genome",
                idx=i,
            )
            continue
        gid = g.get("genome_id") or g.get("id")
        if not gid:
            _reject_or_skip(
                "missing ``genome_id`` / ``id`` field — citation would "
                "render as ``[BV-BRC genome ?]`` and let the LLM cite "
                "garbage past the validator",
                strict=strict,
                kind="bvbrc_genome",
                idx=i,
            )
            continue
        allowed.add(f"[BV-BRC genome {gid}]")
        name = g.get("genome_name") or g.get("name") or "(unnamed)"
        taxon = g.get("taxon_lineage") or g.get("lineage") or ""
        host = g.get("host_name") or g.get("host") or ""
        bits = [f"- **BV-BRC genome `{gid}`** — {name}"]
        if taxon:
            bits.append(f"  - Taxonomy: {taxon}")
        if host:
            bits.append(f"  - Host: {host}")
        rendered_lines.extend(bits)
    if not rendered_lines:
        return ("(no BV-BRC genomes matched)", allowed)
    return ("\n".join(rendered_lines), allowed)


def _render_violin_mappings(
    mappings: Iterable[dict[str, Any]], cap: int, *, strict: bool
) -> tuple[str, set[str]]:
    """Render VIOLIN cached mappings. Returns ``(rendered, allowed_tokens)``.

    A mapping with no ``synonym_id``/``id`` is dropped in strict mode:
    without an ID the citation token degenerates to a free-text phrase
    that won't satisfy the ``[VIOLIN <id>]`` pattern, and the row's
    presence in the prompt risks the LLM emitting a malformed marker.
    ``allowed_tokens`` carries one ``[VIOLIN <sid>]`` per surviving
    row; the citation-grounding validator uses it to reject IDs that
    were never offered to the LLM.
    """
    rendered_lines: list[str] = []
    allowed: set[str] = set()
    for i, m in enumerate(list(mappings)[:cap]):
        if not isinstance(m, dict):
            _reject_or_skip(
                f"expected dict, got {type(m).__name__}",
                strict=strict,
                kind="violin_mapping",
                idx=i,
            )
            continue
        sid = m.get("synonym_id") or m.get("id")
        if not sid:
            _reject_or_skip(
                "missing ``synonym_id`` / ``id`` field — citation would "
                "lack a stable token and the LLM would emit a malformed "
                "``[VIOLIN]`` marker",
                strict=strict,
                kind="violin_mapping",
                idx=i,
            )
            continue
        c = m.get("canonical_term") or m.get("canonical")
        if not c:
            _reject_or_skip(
                "missing ``canonical_term`` / ``canonical`` field",
                strict=strict,
                kind="violin_mapping",
                idx=i,
            )
            continue
        allowed.add(f"[VIOLIN {sid}]")
        q = m.get("query_term") or m.get("query") or "(unspecified)"
        conf = m.get("confidence")
        bit = f"- **VIOLIN mapping**: `{q}` → `{c}` [VIOLIN {sid}]"
        rendered_lines.append(bit)
        if conf is not None:
            rendered_lines.append(f"  - Confidence: {conf}")
    if not rendered_lines:
        return ("(no VIOLIN cached mappings)", allowed)
    return ("\n".join(rendered_lines), allowed)


def _render_publications(
    pubs: Iterable[dict[str, Any]], cap: int, *, strict: bool, abstract_max_chars: int = 300
) -> tuple[str, set[str]]:
    """Render harvester publication metadata.

    Expected keys (DataCite-shaped):
      - ``doi`` REQUIRED — used as the inline citation token.
      - ``title``, ``authors``, ``year``, ``journal`` (optional).
      - ``abstract`` or ``description`` (optional, truncated).

    A publication without a DOI cannot be cited (the ``[10.x/...]``
    pattern requires a DOI literal); strict mode rejects, lenient
    mode skips with a warning. Returns ``(rendered, allowed_tokens)``.
    """
    rendered_lines: list[str] = []
    allowed: set[str] = set()
    for i, p in enumerate(list(pubs)[:cap]):
        if not isinstance(p, dict):
            _reject_or_skip(
                f"expected dict, got {type(p).__name__}",
                strict=strict,
                kind="publication",
                idx=i,
            )
            continue
        doi = p.get("doi")
        if not doi or not str(doi).startswith("10."):
            _reject_or_skip(
                f"missing or non-DOI ``doi`` field (got {doi!r}); "
                "citation requires a DOI literal matching ``10.<id>/...``",
                strict=strict,
                kind="publication",
                idx=i,
            )
            continue
        # DOI must not contain ], [, or whitespace — those characters are
        # excluded by the citation extraction regex ([^\]\s\[]+). A DOI with
        # such characters would be rendered in the prompt and added to
        # allowed_tokens, but _extract_distinct_citations would never match
        # it, so the LLM would be unable to produce a valid citation and the
        # validator would fail with a confusing "0 citations" error rather
        # than a clear rejection at render time.
        import re as _re

        if _re.search(r"[\]\s\[]", str(doi)):
            _reject_or_skip(
                f"doi {doi!r} contains characters (']', '[', or whitespace) "
                "that break the citation extraction pattern; the LLM cannot "
                "produce a valid inline citation for this DOI",
                strict=strict,
                kind="publication",
                idx=i,
            )
            continue
        allowed.add(f"[{doi}]")
        title = p.get("title") or "(untitled)"
        authors = p.get("authors") or []
        year = p.get("year") or ""
        journal = p.get("journal") or p.get("publisher") or ""
        abstract = p.get("abstract") or p.get("description") or ""
        bits = [f"- **[{doi}]** *{title}*"]
        meta_parts: list[str] = []
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
        if abstract and abstract_max_chars > 0:
            shown = abstract[:abstract_max_chars]
            ellipsis = "…" if len(abstract) > abstract_max_chars else ""
            bits.append(f"  - {shown}{ellipsis}")
        rendered_lines.extend(bits)
    if not rendered_lines:
        return ("(no publications)", allowed)
    return ("\n".join(rendered_lines), allowed)


def _render_globus_results(
    hits: Iterable[dict[str, Any]], cap: int, *, strict: bool, abstract_max_chars: int = 300
) -> tuple[str, set[str]]:
    """Render Globus Search hits from the APECx harvested-corpus index.

    Expected hit keys (per ``apecx_integration.agents.globus_search.search``):

      - ``subject`` REQUIRED — unique harvester record ID (DOI, PMID,
        PDB accession, etc.). Used as the inline citation token via
        the ``[Globus <subject>]`` shape.
      - ``content`` (dict, optional) — indexed payload. Shape varies by
        source; we surface ``title`` / ``abstract`` / ``description``
        when present.

    Returns ``(rendered, allowed_tokens)``. A hit without a ``subject``
    cannot be cited; strict mode rejects, lenient mode skips with a
    warning.
    """
    rendered_lines: list[str] = []
    allowed: set[str] = set()
    if cap <= 0:
        return ("(no Globus Search hits)", allowed)
    import re as _re

    for i, h in enumerate(list(hits)[:cap]):
        if not isinstance(h, dict):
            _reject_or_skip(
                f"expected dict, got {type(h).__name__}",
                strict=strict,
                kind="globus_result",
                idx=i,
            )
            continue
        subject = h.get("subject")
        if not subject or not isinstance(subject, str):
            _reject_or_skip(
                f"missing or non-str ``subject`` field (got {subject!r}); "
                "Globus citation requires a string subject (record ID)",
                strict=strict,
                kind="globus_result",
                idx=i,
            )
            continue
        # Same constraint as DOI: subject must not contain ']', '[', or
        # whitespace — those characters are excluded by the citation
        # extraction pattern ``[Globus [^\]\s\[]+]``.
        if _re.search(r"[\]\s\[]", subject):
            _reject_or_skip(
                f"subject {subject!r} contains characters (']', '[', or "
                "whitespace) that break the citation extraction pattern",
                strict=strict,
                kind="globus_result",
                idx=i,
            )
            continue
        token = f"[Globus {subject}]"
        allowed.add(token)
        content = h.get("content") or {}
        # DataCite-aware extraction: every record in the aggregate index stores
        # its title at content["titles"][0]["title"], not a flat "title" key.
        # Reading the flat key dropped the title of EVERY harvested-corpus hit
        # (journal articles AND PDB/EMDB structures) to "(untitled)".
        title = datacite_title(content)
        abstract = datacite_description(content)
        subjects = datacite_subjects(content)
        bits = [f"- **{token}** *{title or '(untitled)'}*"]
        if abstract and abstract_max_chars > 0:
            shown = str(abstract)[:abstract_max_chars]
            ellipsis = "…" if len(str(abstract)) > abstract_max_chars else ""
            bits.append(f"  - {shown}{ellipsis}")
        if subjects:
            bits.append(f"  - keywords: {', '.join(subjects)}")
        rendered_lines.extend(bits)
    if not rendered_lines:
        return ("(no Globus Search hits)", allowed)
    return ("\n".join(rendered_lines), allowed)


def _extract_distinct_citations(text: str, patterns: list[str]) -> set[str]:
    """Return the set of distinct citation tokens in ``text``.

    Patterns each match a complete citation (e.g. ``\\[RAG chunk #\\d+\\]``,
    not a prefix). Distinct = unique by exact token text — so the LLM
    citing the same source 12 times counts as one distinct citation.
    """
    import re

    out: set[str] = set()
    for pat in patterns:
        out.update(re.findall(pat, text))
    return out


def synthesize_response(
    query: str,
    *,
    rag_chunks: Iterable[dict[str, Any]] | None = None,
    bvbrc_genomes: Iterable[dict[str, Any]] | None = None,
    violin_mappings: Iterable[dict[str, Any]] | None = None,
    publications: Iterable[dict[str, Any]] | None = None,
    globus_results: Iterable[dict[str, Any]] | None = None,
    llm: Any = None,
    config: SynthesisConfig | None = None,
    system_prompt_override: str | None = None,
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
        system_prompt_override: When provided (non-empty), replaces ONLY
            the system message text for this call — every other knob and
            citation gate still comes from ``config``. This is the seam a
            caller (e.g. ``EvidenceReviewSynthesisStep``) uses to enforce an
            output contract WITHOUT cloning the whole config or mutating the
            shared ``synthesis_config.yml`` that other workflows depend on.

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

    # System-prompt override seam. The override replaces ONLY the system message
    # text; all gates (citations, length, empty-retrieval, grounding) stay sourced
    # from ``cfg``. A blank override is almost certainly a caller bug (the LLM would
    # get no role) — reject it rather than silently fall back to cfg.system_prompt.
    if system_prompt_override is not None:
        if not isinstance(system_prompt_override, str) or not system_prompt_override.strip():
            raise ValueError(
                "synthesize_response: system_prompt_override, when provided, must be a "
                f"non-empty string; got {type(system_prompt_override).__name__}="
                f"{system_prompt_override!r}"
            )
        system_prompt = system_prompt_override
    else:
        system_prompt = cfg.system_prompt

    rag_block, rag_tokens = _render_rag_chunks(
        rag_chunks or [],
        cfg.max_rag_chunks,
        strict=cfg.strict_input_validation,
    )
    bvbrc_block, bvbrc_tokens = _render_bvbrc_genomes(
        bvbrc_genomes or [],
        cfg.max_bvbrc_genomes,
        strict=cfg.strict_input_validation,
    )
    violin_block, violin_tokens = _render_violin_mappings(
        violin_mappings or [],
        cfg.max_violin_mappings,
        strict=cfg.strict_input_validation,
    )
    pubs_block, pub_tokens = _render_publications(
        publications or [],
        cfg.max_publications,
        strict=cfg.strict_input_validation,
        abstract_max_chars=cfg.abstract_max_chars,
    )
    globus_block, globus_tokens = _render_globus_results(
        globus_results or [],
        cfg.max_globus_results,
        strict=cfg.strict_input_validation,
        abstract_max_chars=cfg.abstract_max_chars,
    )
    # The union of every token a renderer authorized. The LLM is
    # allowed to cite any of these and nothing else (when
    # ``validate_citations_against_inputs`` is on).
    allowed_tokens: set[str] = (
        rag_tokens | bvbrc_tokens | violin_tokens | pub_tokens | globus_tokens
    )
    n_rag, n_bvbrc, n_violin, n_pubs, n_globus = (
        len(rag_tokens),
        len(bvbrc_tokens),
        len(violin_tokens),
        len(pub_tokens),
        len(globus_tokens),
    )

    # Pre-LLM all-empty check. Without retrieved data the LLM can only
    # confabulate; running it would burn tokens and produce a
    # citation-free response that fails the post-LLM validator with a
    # confusing error pointing at the wrong cause. Promote the failure
    # to its true root by checking BEFORE the LLM call.
    if cfg.fail_on_empty_retrieval and not allowed_tokens:
        raise ValueError(
            "synthesize_response: every retrieval input is empty after "
            "validation. The synthesis is grounded in retrieved data; "
            "with no data the LLM can only confabulate. Either supply "
            "retrieval results from BV-BRC / VIOLIN / RAG / harvesters, "
            "or set ``fail_on_empty_retrieval=False`` in the synthesis "
            "config (not recommended — citation validation will then "
            "fail with a less actionable error).\n\n"
            f"Surviving counts: rag={n_rag} bvbrc={n_bvbrc} "
            f"violin={n_violin} pubs={n_pubs} globus={n_globus}"
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
        f"## Publications\n\n{pubs_block}\n\n"
        f"## Globus Search hits (APECx harvested corpus)\n\n{globus_block}"
    )

    # Lazy import: tests that pass a stub ``llm`` don't need the heavy
    # langchain stack to load. Canonical factory lives in
    # ``_llm_factory``; importing it here (not at module top) keeps
    # the synthesis package import-cheap for callers that always
    # supply their own LLM.
    if llm is None:
        from apecx_integration.agents._llm_config import preflight_llm_model
        from apecx_integration.agents._llm_factory import build_chat_llm

        # Fail loud + early (once per process) if the synthesis model is not
        # pulled on a reachable endpoint — clearer than a cryptic Ollama 404
        # on the LLM call below.
        preflight_llm_model()
        llm = build_chat_llm()

    from langchain_core.messages import HumanMessage, SystemMessage

    response = llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
    )
    content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ValueError(
            f"synthesize_response: LLM returned empty/non-string content "
            f"(got {type(content).__name__}={content!r})"
        )

    if cfg.min_response_chars and len(content.strip()) < cfg.min_response_chars:
        raise ValueError(
            f"synthesize_response: LLM response is curtailed "
            f"(len={len(content.strip())} < min_response_chars="
            f"{cfg.min_response_chars}). Local LLMs occasionally return "
            f"trivial one-liners that satisfy the citation rule but "
            f"ship no value; the user directive (2026-04-27) calls "
            f"non-trivial responses out as a hard requirement. Lower "
            f"``min_response_chars`` in the config if your fixtures "
            f"are deliberately short.\n\nResponse was:\n{content}"
        )

    if cfg.require_inline_citations:
        distinct = _extract_distinct_citations(content, cfg.citation_marker_patterns)
        if len(distinct) < cfg.min_distinct_citations:
            raise ValueError(
                f"synthesize_response: LLM response has only "
                f"{len(distinct)} distinct citation token(s) but "
                f"``min_distinct_citations`` requires "
                f"{cfg.min_distinct_citations}. Distinct tokens "
                f"found: {sorted(distinct)!r}. The synthesis must "
                f"ground every claim in retrieved data; an uncited or "
                f"single-source response is the silent-failure shape "
                f"this validator guards. Disable "
                f"``require_inline_citations`` or lower "
                f"``min_distinct_citations`` if this is too strict for "
                f"your deployment.\n\nResponse was:\n{content}"
            )

        # Citation-input grounding. Each renderer reports the set of
        # citation tokens it authorized; the LLM is only allowed to cite
        # tokens in the union. A token outside the union is a
        # hallucination — the LLM invented an ID (or scrambled a
        # legitimate one). Reject; do NOT pass garbage to the
        # scientist. This closes the silent-failure shape where
        # ``[BV-BRC genome 99999.99]`` (never in inputs) satisfies the
        # regex-only check.
        if cfg.validate_citations_against_inputs:
            unknown = distinct - allowed_tokens
            if unknown:
                raise ValueError(
                    f"synthesize_response: LLM cited "
                    f"{len(unknown)} token(s) that were NOT in the "
                    f"retrieval inputs. The LLM is hallucinating IDs "
                    f"(citation grounding has been violated). "
                    f"Hallucinated tokens: {sorted(unknown)!r}. "
                    f"Allowed tokens (from inputs): "
                    f"{sorted(allowed_tokens)!r}. Disable "
                    f"``validate_citations_against_inputs`` ONLY if "
                    f"your caller deliberately allows the LLM to cite "
                    f"data outside the supplied retrieval bundle (rare "
                    f"and usually a smell).\n\nResponse was:\n{content}"
                )

    return content
