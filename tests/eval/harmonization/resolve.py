"""Query resolution + regime classification for the harmonization eval.

Resolves each corpus query the SAME way harmonized_search does (dictionary ``lookup_entity``, with
``extract_virus_names`` applied first to free-text ``real_world`` phrases), and derives a per-query
RESOLUTION REGIME so precision can be aggregated by the failure mode it exposes. The regime is derived
from the resolver's own ``path`` + a transparent family-rank label heuristic — never a hardcoded
per-pathogen table.
"""

from __future__ import annotations

from dataclasses import dataclass

from apecx_integration.agents.globus_search.taxonomy_resolver import extract_virus_names
from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.lookup import LookupResult, lookup_entity

# ICTV rank suffixes above species — a resolved label carrying one is an over-broad ("umbrella")
# target: a genus/family IRI whose subtree spans many species, so a specific-intent query resolved
# here returns a heterogeneous set. Transparent heuristic; the precision judge confirms it from data.
_FAMILY_RANK_SUFFIXES = ("viridae", "virinae", "virales", "viricetes", "viricota")


@dataclass
class ResolvedQuery:
    term: str
    category: str
    lookup: LookupResult
    resolved_term: str  # what was actually resolved (may differ from term for real_world phrases)
    regime: str


def _classify_regime(result: LookupResult) -> str:
    if result.path == "ambiguous":
        return "ambiguous_paused"
    if result.path == "miss":
        return "miss_raw_fallback"
    if result.path == "slow":
        return "mis_resolution_risk"  # confidence ~0.3; may point at a wrong taxon
    if result.path == "ancestor":
        return "ancestor_substituted"  # strain miss → species IRI substituted
    # fast / override: cleanly resolved. Clean species vs over-broad family/genus.
    label = (result.canonical_label or "").lower()
    if any(tok.endswith(_FAMILY_RANK_SUFFIXES) for tok in label.split()):
        return "umbrella_overbroad"
    return "resolved_species"


def resolve_query(term: str, category: str) -> ResolvedQuery:
    """Resolve one corpus query. For ``real_world`` free-text phrases, extract the virus name first
    (mirroring the workflow's normalize→resolve), then look it up; other categories resolve directly."""
    resolved_term = term
    if category == "real_world":
        names = extract_virus_names(term)
        if names:
            resolved_term = names[0]  # most-specific first
    result = lookup_entity(resolved_term, entity_type=EntityType.PATHOGEN)
    return ResolvedQuery(
        term=term,
        category=category,
        lookup=result,
        resolved_term=resolved_term,
        regime=_classify_regime(result),
    )
