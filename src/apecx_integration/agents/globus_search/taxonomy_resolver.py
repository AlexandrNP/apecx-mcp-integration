"""Extract candidate virus name(s) from free query text (for downstream taxon resolution).

This module's job is NAME EXTRACTION only: pull the distinctive virus name(s) out of a free
query string and normalize short names / acronyms to a canonical scientific spelling (e.g.
"SARS-CoV-2" -> "Severe acute respiratory syndrome coronavirus 2", "lassa" -> "Lassa
mammarenavirus"). The extracted name is then resolved to an NCBI ``taxon_id`` by the
DICT-based resolver ``harmonized_resolve_step.build_resolution_plan`` (which applies the
synonym dictionary's ``merged_taxons`` old->new redirects, e.g. Lassa 11620 -> 3052310).

HISTORY: this module previously ALSO resolved the taxon itself, against the live BV-BRC
taxonomy index (``resolve_query_to_taxon`` / ``resolve_virus_taxon``). That live-name-matching
resolver was retired (2026-06-18) because it diverged from the dict resolver: it did not apply
``merged_taxons`` redirects (Lassa -> stale 11620 with 0 live coverage), it fuzzy-matched wrong
organisms ("Junin virus" -> an Influenza A strain "A/Junin/.../H3N2"), and it picked nodes by the
stale BV-BRC ``genomes`` count which does not reflect ``genome_feature`` reachability. The
sequence-conservation leg (``EvidenceQueryNormalizeStep``) now resolves via the SAME
``build_resolution_plan`` the resolve step uses, so the leg's taxon agrees with the recorded
``bundle["taxon_id"]``. See the dual-resolver-divergence investigation (2026-06-18).
"""

from __future__ import annotations

import re

# Short-name / acronym -> canonical scientific spelling. ORDER MATTERS: the more specific pattern
# must precede the more general one it is a substring of (SARS-CoV-2 before SARS-CoV; HIV-1 before
# HIV; "influenza A" before "influenza"). Negative lookaheads keep the general pattern from also
# firing on the specific form. The canonical spelling on the right is what gets handed to the dict
# resolver (build_resolution_plan), so acronyms/short names resolve to the right NCBI taxon.
_VIRUS_ALIASES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(?:sars[\s-]?cov[\s-]?2|2019[\s-]?ncov)\b"),
        "Severe acute respiratory syndrome coronavirus 2",
    ),
    (
        re.compile(r"\bsars[\s-]?cov\b(?![\s-]?2)|\bsars\s+coronavirus\b"),
        "Severe acute respiratory syndrome-related coronavirus",
    ),
    (
        re.compile(r"\bmers[\s-]?cov\b|\bmers\b"),
        "Middle East respiratory syndrome-related coronavirus",
    ),
    (re.compile(r"\bhiv[\s-]?1\b"), "Human immunodeficiency virus 1"),
    (re.compile(r"\bhiv[\s-]?2\b"), "Human immunodeficiency virus 2"),
    (re.compile(r"\bhiv\b"), "Human immunodeficiency virus 1"),
    (re.compile(r"\binfluenza\s+a\b"), "Influenza A virus"),
    (re.compile(r"\binfluenza\s+b\b"), "Influenza B virus"),
    (re.compile(r"\binfluenza\b|\bflu\b"), "Influenza A virus"),
    (re.compile(r"\bchikungunya\b|\bchikv\b"), "Chikungunya virus"),
    (re.compile(r"\bwest\s+nile\b|\bwnv\b"), "West Nile virus"),
    (re.compile(r"\bdengue\b|\bdenv\b"), "Dengue virus"),
    (re.compile(r"\bzika\b|\bzikv\b"), "Zika virus"),
    (re.compile(r"\bebola\b|\bebov\b"), "Zaire ebolavirus"),
    (re.compile(r"\bmarburg\b"), "Marburg marburgvirus"),
    (re.compile(r"\brsv\b|\brespiratory\s+syncytial\b"), "Human respiratory syncytial virus"),
    (re.compile(r"\bmeasles\b"), "Measles morbillivirus"),
    (re.compile(r"\brabies\b"), "Rabies lyssavirus"),
    (re.compile(r"\brubella\b"), "Rubella virus"),
    (re.compile(r"\byellow\s+fever\b"), "Yellow fever virus"),
    (re.compile(r"\bmumps\b"), "Mumps orthorubulavirus"),
    (re.compile(r"\bhcv\b|\bhepatitis\s+c\b"), "Hepatitis C virus"),
    (re.compile(r"\bhbv\b|\bhepatitis\s+b\b"), "Hepatitis B virus"),
    (re.compile(r"\blassa\b"), "Lassa mammarenavirus"),
    (re.compile(r"\bnipah\b"), "Nipah henipavirus"),
    (re.compile(r"\bvariola\b|\bsmallpox\b"), "Variola virus"),
]

# Generic "<word(s)> virus(es)" phrase: group(1) is the distinctive part BEFORE "virus"
# (kept outside the capture so the greedy word-run does not swallow "virus" itself), so
# arbitrary viruses NOT in the alias table (e.g. "Powassan virus", "Mayaro virus") still
# get a candidate name to resolve. The full search name is rebuilt as "<group1> virus".
#
# NOTE on ``virus(?:es)?`` vs ``viruses?``: the seemingly-equivalent ``viruses?`` form makes
# this pattern silently NEVER match (the trailing ``s?`` interacts pathologically with the
# preceding greedy ``{0,3}`` word-run under CPython's backtracker — verified empirically).
# ``virus(?:es)?`` is correct.
_VIRUS_PHRASE_RE = re.compile(
    r"\b([a-z][a-z0-9'-]*(?:\s+[a-z][a-z0-9'-]*){0,3})\s+virus(?:es)?\b",
    re.IGNORECASE,
)
# Single-token suffix-form names (norovirus, ebolavirus, rotavirus, …). The phrase RE above needs a
# SPACE before "virus", so it MISSES a one-word "<X>virus" name → extraction returns [] → the PubMed
# branch falls back to the raw verbose query and ANDs every token → 0 hits. Match the suffix form too.
_VIRUS_SUFFIX_RE = re.compile(r"\b([a-z][a-z0-9'-]*virus)\b", re.IGNORECASE)
# Suffix-form words that are NOT organism names: "antivirus" (software), "provirus" (a genomic
# state — an integrated viral genome, not a taxon).
_VIRUS_SUFFIX_DENYLIST = {"antivirus", "provirus"}


def extract_virus_names(query: str) -> list[str]:
    """Pull candidate virus name(s) from free query text, most-specific first.

    Returns canonical scientific spellings (from the alias table) followed by any generic
    ``"<X> virus"`` phrases and one-word suffix-form names (``norovirus``), de-duplicated
    case-insensitively in priority order. The caller resolves them (via ``build_resolution_plan``)
    and uses the first that resolves to a taxon.
    """
    if not isinstance(query, str) or not query.strip():
        return []
    lowered = query.lower()
    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())

    for pattern, canonical in _VIRUS_ALIASES:
        if pattern.search(lowered):
            _add(canonical)
    for match in _VIRUS_PHRASE_RE.finditer(query):
        _add(f"{match.group(1).strip()} virus")
    for match in _VIRUS_SUFFIX_RE.finditer(query):
        name = match.group(1).strip()
        if name.lower() not in _VIRUS_SUFFIX_DENYLIST:
            _add(name)
    return out


__all__ = ["extract_virus_names"]
