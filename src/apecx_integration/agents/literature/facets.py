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

#: Minimum stem-prefix length used to match facets longer than 3 chars (morphological
#: variants). Tightened from 4 -> 5 (cycle-2 lever c): a 4-char shared prefix was
#: over-matching unrelated words. A facet shorter than this but longer than 3 chars uses
#: its WHOLE token as the prefix (``key[:_STEM_PREFIX_LEN]`` degrades to the whole key).
_STEM_PREFIX_LEN = 5


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


def _facet_in_words(key: str, words: set[str]) -> bool:
    """Whether a single lowercased ``key`` matches any whole word in ``words``.

    Matching rule (tightened in cycle-2 lever c):
      - a facet of 3 chars or fewer matches only a WHOLE text word — never a
        substring inside a longer word (no ``e2`` inside ``e2f1`` false hits);
      - a longer facet matches any text word that STARTS WITH the facet's stem
        (a word-boundary prefix match, so morphological variants like ``epitopes``
        vs ``epitope`` count). The stem is the first :data:`_STEM_PREFIX_LEN` chars,
        or the whole facet when it is shorter — raising the floor from 4 to 5 kills
        the short-shared-prefix false hits Tester A reported.
    """
    if len(key) <= 3:
        return key in words
    stem = key[:_STEM_PREFIX_LEN]
    return any(w.startswith(stem) for w in words)


def facets_present(facets: list[str], text: str) -> list[str]:
    """The DISTINCT facets from ``facets`` that occur in ``text`` (case-insensitive).

    Order follows ``facets``; each facet appears at most once. Shares the single
    :func:`_facet_in_words` matching rule with :func:`keyword_facet_hits`. The IDF
    keyword signal in ``LiteratureRagStep._hybrid_rank`` needs to know WHICH facets a
    record carries (to sum their rarity weights), not just how many — hence this
    companion to the count-only :func:`keyword_facet_hits`.
    """
    words = {w.lower() for w in _TOKEN_RE.findall(text)}
    if not words:
        return []
    return [facet for facet in facets if _facet_in_words(facet.lower(), words)]


def keyword_facet_hits(facets: list[str], text: str) -> int:
    """Count how many DISTINCT facets occur in ``text`` (case-insensitive).

    Thin count over :func:`facets_present` (DRY — one matching rule). See
    :func:`_facet_in_words` for the stem / short-token semantics.
    """
    return len(facets_present(facets, text))


__all__ = [
    "QUESTION_STOPWORDS",
    "extract_question_facets",
    "facets_present",
    "keyword_facet_hits",
]
