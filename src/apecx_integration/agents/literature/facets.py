"""Shared question-facet extraction + keyword-overlap scoring (base-dep clean).

ONE definition of "what counts as a question content token" for the literature
pipeline. Two consumers reuse it (DRY):

  - :func:`apecx_integration.mcp_surface.tools.literature_qa._question_term`
    builds a PubMed eSearch term from the facets.
  - :class:`...literature_rag.steps.literature_rag_step.LiteratureRagStep`
    scores each candidate record by how many distinct facets it mentions
    (the keyword half of its hybrid ranker).

The question is treated as an OPAQUE token string — this module never
interprets its meaning. Imports only the stdlib (``re``), so it is safe to
import at module scope even on a clean install with no ``rag`` extra.
"""

from __future__ import annotations

import re

#: Word tokenizer: runs of alphanumerics (drops punctuation / boolean operators like ``()``).
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

#: Generic / bibliographic words that carry no topical signal and must never be facets.
QUESTION_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "article",
        "articles",
        "as",
        "at",
        "be",
        "by",
        "describe",
        "each",
        "for",
        "from",
        "how",
        "id",
        "ids",
        "in",
        "is",
        "it",
        "its",
        "list",
        "not",
        "of",
        "on",
        "or",
        "papers",
        "pmid",
        "pmids",
        "publications",
        "pubmed",
        "reported",
        "reporting",
        "study",
        "studies",
        "that",
        "the",
        "their",
        "this",
        "to",
        "what",
        "which",
        "with",
        "was",
        "were",
        "where",
        "who",
    ]
)

#: Prefix length used to stem-match facets longer than 3 chars (morphological variants).
_STEM_PREFIX_LEN = 4


def extract_question_facets(question: str, organism: str = "") -> list[str]:
    """The question's content tokens, in first-seen order, original case preserved.

    Drops: tokens shorter than 2 chars, generic/bibliographic :data:`QUESTION_STOPWORDS`,
    and (when ``organism`` is given) the organism's own words. Deduplicated
    case-insensitively. The sole facet definition shared across the pipeline.
    """
    organism_words = {w.lower() for w in _TOKEN_RE.findall(organism)} if organism else set()
    facets: list[str] = []
    seen: set[str] = set()
    for word in _TOKEN_RE.findall(question):
        key = word.lower()
        if len(key) < 2 or key in QUESTION_STOPWORDS or key in organism_words or key in seen:
            continue
        seen.add(key)
        facets.append(word)
    return facets


def keyword_facet_hits(facets: list[str], text: str) -> int:
    """Count how many DISTINCT facets occur in ``text`` (case-insensitive).

    Matching rule (mirrors the stem intent of PubMed term expansion):
      - a facet longer than 3 chars matches any text word that starts with the
        facet's :data:`_STEM_PREFIX_LEN`-char stem, so morphological variants
        (``epitopes`` vs ``epitope``) count as a hit;
      - a facet of 3 chars or fewer matches only a WHOLE text word — never a
        substring inside a longer word (no ``e2`` inside ``e2f1`` false hits).
    """
    words = {w.lower() for w in _TOKEN_RE.findall(text)}
    if not words:
        return 0
    hits = 0
    for facet in facets:
        key = facet.lower()
        if len(key) > 3:
            stem = key[:_STEM_PREFIX_LEN]
            if any(w.startswith(stem) for w in words):
                hits += 1
        elif key in words:
            hits += 1
    return hits


__all__ = ["QUESTION_STOPWORDS", "extract_question_facets", "keyword_facet_hits"]
