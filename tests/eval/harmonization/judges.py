"""The non-circular precision judge — is a returned record actually about the queried pathogen?

Judged from TWO signals the harmonized retrieval filter did NOT use (it filters on
``subjects.valueUri``):

  Judge A — SOURCE taxonomy: the record's source-DB taxon id (DataCite ``NCBI-Taxonomy``
    alternateIdentifier) is inside the queried species' subtree. Independent of ``subjects.valueUri``.
  Judge B — descriptive TEXT: the title/description/subjects/organism text names the pathogen (a dict
    synonym). Independent of taxonomy integers entirely.

CRITICAL: this module must NEVER call ``datacite_taxon_iris`` (that IS the filtered field — reading it
is the circularity the prior benchmark fell into). It is not imported here.

INDEPENDENCE CAVEAT (surfaced in the findings, not hidden): Judge A is FIELD-independent — it reads the
source ``NCBI-Taxonomy`` alternateIdentifier, a different field from the filtered ``subjects.valueUri``.
But both may have been written by the SAME apecx harvest/harmonization taxon resolution, so a
single-stamp harmonized record's Judge-A verdict can be PROVENANCE-partial (tautological). Judge A is
FULLY independent exactly where product precision degrades: the ``raw_substitution`` cells (served came
from the raw text leg — no ``valueUri`` filter ever touched them) and cross-bridge stamps. The residual
tautology is covered by the fully text-independent Judge B and by the LLM validation, which oversamples
the A/B ``disagree`` cases. The findings report Judge A independence per-regime.
"""

from __future__ import annotations

import re

from apecx_integration.agents.globus_search._datacite import (
    datacite_description,
    datacite_identifiers,
    datacite_organisms,
    datacite_subjects,
    datacite_title,
)
from apecx_integration.composition.steps.harmonized_search_execute_step import _iri_to_taxon_id


def build_subtree(index_obj, canonical_iri: str) -> set[int]:
    """Judge A's in-subtree id-set for a query: the queried species + all its descendant strain ids.

    Reuses the runtime ``DictionaryIndex.lookup_descendant_taxon_ids`` (loader.py) — the strict-hierarchy
    CTE over the SQLite ``taxon_hierarchy`` table (applies ``merged_taxons`` old→new redirects). NOT a
    build-time nodes.dmp walk. The CTE excludes the root, so the queried taxon itself is added here.
    """
    ids: set[int] = set(index_obj.lookup_descendant_taxon_ids(canonical_iri))
    root = _iri_to_taxon_id(canonical_iri)
    if root is not None:
        ids.add(root)
    return ids


def source_taxon_ids(content) -> list[int]:
    """Judge A signal: the record's source-DB NCBI taxon id(s) from the DataCite ``NCBI-Taxonomy``
    alternateIdentifier. This is the record's OWN primary taxon, DISTINCT from the full-lineage
    ``subjects.valueUri`` array the query filtered on — the independence that breaks the circularity.
    """
    out: list[int] = []
    for v in datacite_identifiers(content).get("NCBI-Taxonomy", []):
        # NCBI taxon ids are plain ints; be defensive against a rare ``species.strain`` form.
        head = str(v).strip().split(".", 1)[0]
        if head.isdigit():
            out.append(int(head))
    return out


def judge_a(content, subtree_ids: set[int]) -> bool | None:
    """True if the record's source taxon is in the queried subtree; False if off-target; None if the
    record carries no source taxon id (structural / non-taxonomic records — Judge A abstains)."""
    src = source_taxon_ids(content)
    if not src:
        return None
    return any(t in subtree_ids for t in src)


def _word_bounded(needle: str, hay: str) -> bool:
    return re.search(rf"\b{re.escape(needle)}\b", hay) is not None


def judge_b(content, synonyms) -> bool | None:
    """True if the record's descriptive text names a dict synonym of the pathogen; None when the record
    carries no usable text (abstain). Independent of BOTH the valueUri stamp and the source integer.

    Only a False is never returned as a hard "not about X": absence of a synonym in a title is weak
    evidence (titles are terse), so Judge B returns True (found) or None (no text / not found) — it
    AFFIRMS relevance but does not by itself DENY it. The denials come from Judge A (off-target taxon).
    """
    parts = [
        datacite_title(content) or "",
        datacite_description(content) or "",
        " ".join(datacite_subjects(content, limit=12)),
        " ".join(datacite_organisms(content)),
    ]
    hay = " ".join(parts).lower()
    if not hay.strip():
        return None
    for s in synonyms:
        s = s.strip().lower()
        if len(s) >= 3 and _word_bounded(s, hay):
            return True
    return None


def combined_verdict(a: bool | None, b: bool | None) -> str:
    """relevant | false_positive | disagree | unjudgeable.

    - relevant       : at least one judge affirms and neither denies.
    - false_positive : a judge denies and neither affirms (only Judge A denies — see judge_b).
    - disagree       : one affirms while the other denies (A/B conflict → LLM-validation target).
    - unjudgeable    : both abstain (None).
    """
    affirm = (a is True) or (b is True)
    deny = (a is False) or (b is False)
    if affirm and deny:
        return "disagree"
    if affirm:
        return "relevant"
    if deny:
        return "false_positive"
    return "unjudgeable"


def classify_fp(content, served_from_raw: bool, valueuri_count: int) -> str:
    """Attribute WHERE a false positive came from (only meaningful for a non-relevant verdict).

    - raw_substitution        : the served corpus fell back to the raw text-matched leg (broken/degraded
                                verdict) — the "chikungunya envelope → West Nile 7E4K" class. Judge A is
                                fully independent here (no valueUri filter ever touched these records).
    - multi_subject_incidental: the record carries ≥2 subjects.valueUri; the query taxon is incidental
                                (co-infection / host / vector / bound antigen partner).
    - structural_text_parse   : no source taxon id but organism text present (structural records).
    - mis_resolution          : the stamp itself points at a wrong species (resolver picked wrong taxon).
    """
    if served_from_raw:
        return "raw_substitution"
    if valueuri_count > 1:
        return "multi_subject_incidental"
    if not source_taxon_ids(content) and datacite_organisms(content):
        return "structural_text_parse"
    return "mis_resolution"
