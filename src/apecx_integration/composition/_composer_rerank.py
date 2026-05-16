"""Post-retrieval re-rank helpers extracted from composer.py (G78).

Three free functions that operate on a list of ``SearchHit`` objects
plus a user prompt. The re-rank boosts hits whose class name shares
a token with the prompt — addresses the asymmetry where semantic
retrieval ranks by description similarity rather than by "the user
named this class."

Extracted 2026-05-16 from composer.py. Re-exported from composer.py
so existing test imports
(``from apecx_integration.composition.composer import _rerank_by_class_name_match``)
keep working without change.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-checker only
    from apecx_integration.composition.component_catalog import SearchHit


def _prompt_tokens(prompt: str) -> set[str]:
    """Extract lowercase alphanumeric tokens from a prompt for
    substring-match re-ranking. Tokens < 3 chars are dropped to
    avoid spurious matches on short pronouns / particles ("a",
    "of", "to") that match too many components."""
    return {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_]+", prompt) if len(t) >= 3}


def _class_name_of(class_path: str) -> str:
    return class_path.rsplit(".", 1)[-1] if class_path else ""


def _rerank_by_class_name_match(
    hits: list[SearchHit],
    prompt: str,
) -> list[SearchHit]:
    """Boost hits whose class_name shares a token with the prompt.

    B3 (2026-05-11): pure post-retrieval re-rank. We do NOT mutate
    the SearchHit objects; we return a new list ordered by a
    composite key (token_match_count desc, original_score desc).
    Hits with no token overlap fall through in their original
    order — the base retrieval scoring stays authoritative for
    long-tail components.

    Why this is a net positive even when retrieval already ranks
    well: FAISS / substring-match retrieval scores semantic
    similarity, not lexical "the user named this class." When the
    user writes "use EntityExtractionStep to ...", semantic
    retrieval may still surface ``MoreGenericSearchStep`` above it
    because their descriptions share more vocabulary. The re-rank
    fixes that asymmetry.
    """
    tokens = _prompt_tokens(prompt)
    if not tokens:
        return hits

    def _score(hit: SearchHit) -> tuple[int, int]:
        class_name = _class_name_of(hit.component.class_path)
        # Split CamelCase into lowercase parts so
        # ``EntityExtractionStep`` → [``entity``, ``extraction``,
        # ``step``].
        parts = [p.lower() for p in re.findall(r"[A-Z][a-z0-9]+|[a-z0-9]+", class_name)]
        # A part matches a prompt token if either is a substring of
        # the other — catches "extract" ↔ "extraction" and
        # "entities" ↔ "entity" without pulling in a stemmer.
        # Filter out the ubiquitous "step" / "tool" / "agent" trailing
        # parts so they don't boost everything that ends with the
        # generic suffix.
        ignored = {"step", "tool", "agent", "workflow"}
        match_count = sum(
            1 for p in parts if p not in ignored and any(p in t or t in p for t in tokens)
        )
        return (match_count, hit.score)

    return sorted(hits, key=_score, reverse=True)


__all__ = ["_prompt_tokens", "_class_name_of", "_rerank_by_class_name_match"]
