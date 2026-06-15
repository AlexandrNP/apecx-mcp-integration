"""Virus-name -> (taxon_id, canonical species name) resolution via the BV-BRC taxonomy API.

WHY THIS EXISTS (the gap, verified by a real multi-taxon probe 2026-06-13):
``viral_epitope_analysis`` only does FULL multi-stage science (sequence
conservation + structural reasoning + functional validation) when it has an NCBI
``taxon_id``. When the caller does not pass one, the workflow used to resolve the
species ONLY from a curated 4-virus subset (CHIKV/WNV/ZIKV/DENV) plus a ``"<name>
virus"`` text pattern. So "SARS-CoV-2 spike glycoprotein conserved epitopes" or
"influenza hemagglutinin ..." resolved NOTHING -> no sequence fetch -> 3 of 6
reasoning stages silently dropped. This module resolves an ARBITRARY virus name
from the query text to a real ``taxon_id`` + canonical species name.

WHY BV-BRC TAXONOMY (not NCBI first): BV-BRC is ALREADY the sequence source for the
conservation leg (``BvbrcProteinFastaStep`` queries ``genome_feature`` /
``feature_sequence``). A taxon resolved against the BV-BRC taxonomy index is therefore
GUARANTEED to have BV-BRC sequence coverage — resolving against NCBI could pick a
taxon BV-BRC has no genomes for, silently emptying the conservation leg. We verify
coverage by requiring the chosen node's aggregate ``genomes`` count to be > 0.

THE REAL ENDPOINT (probed live 2026-06-13):

    GET https://www.bv-brc.org/api/taxonomy/?eq(taxon_name,<name>)
        &select(taxon_id,taxon_name,taxon_rank,genomes)
        &sort(-genomes)&limit(<n>)&http_accept=application/json

``eq(taxon_name,X)`` is a SOLR tokenized AND-match (every token of ``X`` must be present
in the node's ``taxon_name``), so a multi-token canonical name ("Influenza A virus")
matches only influenza-A nodes, not arbitrary "...virus" nodes. ``sort(-genomes)`` floats
the canonical SPECIES node (which carries the aggregate genome count — e.g. "Influenza A
virus" 11320 has 1.8M genomes) ABOVE the strain/synthetic-vector nodes (0–few genomes)
that the same substring also matches. We take the highest-``genomes`` node with genomes > 0.

NAME NORMALIZATION (the alias layer): bare ``eq(taxon_name,"SARS-CoV-2")`` matches only
expression-vector noise — the BV-BRC ``taxon_name`` is the full scientific name "Severe
acute respiratory syndrome coronavirus 2". So short names / acronyms are first expanded to
the BV-BRC scientific spelling via a curated ALIAS table (this is domain knowledge — acronym
expansion — NOT fabricated taxonomy; every taxon_id still comes from the live BV-BRC index).
Names not in the alias table fall through the generic ``"<X> virus"`` phrase pattern.

DEGRADE-LOUD (G127 / CC-1): a name that cannot be resolved returns ``None`` (the caller
attaches a NAMED note); a BV-BRC network error logs a WARNING and returns ``None`` — never
a silent wrong taxon, never a raise that would strand the workflow cascade.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

_BVBRC_API_BASE = "https://www.bv-brc.org/api"
_REQUEST_TIMEOUT_SECONDS = 20.0
_TAXONOMY_FETCH_LIMIT = 5


@dataclass(frozen=True)
class TaxonResolution:
    """A virus name resolved to a real BV-BRC taxon with sequence coverage.

    ``taxon_id`` feeds the BV-BRC sequence fetch (conservation leg). ``scientific_name``
    is the canonical species spelling fed to the structural-query facet expansion (PDB
    deposits use the scientific name, e.g. "Severe acute respiratory syndrome coronavirus
    2", not "SARS-CoV-2"). ``bvbrc_taxon_name`` is what the BV-BRC node is actually named
    (may differ from ``scientific_name`` when BV-BRC returns a genotype-level node, e.g.
    "Hepatitis C virus genotype 1") — recorded for provenance. ``genomes`` is the aggregate
    BV-BRC genome count that proves coverage.
    """

    taxon_id: int
    scientific_name: str
    bvbrc_taxon_name: str
    genomes: int
    matched_name: str
    source: str = "bv-brc-taxonomy"


# Short-name / acronym -> canonical BV-BRC scientific spelling. ORDER MATTERS: the more
# specific pattern must precede the more general one it is a substring of (SARS-CoV-2 before
# SARS-CoV; HIV-1 before HIV; "influenza A" before "influenza"). Negative lookaheads keep the
# general pattern from also firing on the specific form. Every canonical name on the right was
# verified to resolve against the live BV-BRC taxonomy index with genomes > 0 (probe 2026-06-13).
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
# ``virus(?:es)?`` is correct. (The older ``structural_query._VIRUS_RE`` carries the buggy
# ``viruses?`` form; it has been masked there because its curated-token path covers the same
# viruses — this resolver supersedes that text path for arbitrary viruses.)
_VIRUS_PHRASE_RE = re.compile(
    r"\b([a-z][a-z0-9'-]*(?:\s+[a-z][a-z0-9'-]*){0,3})\s+virus(?:es)?\b",
    re.IGNORECASE,
)

# Immutable taxonomy -> cache by lowercased name. ``None`` = "tried, did not resolve".
_RESOLVE_CACHE: dict[str, TaxonResolution | None] = {}


def extract_virus_names(query: str) -> list[str]:
    """Pull candidate virus name(s) from free query text, most-specific first.

    Returns canonical BV-BRC scientific spellings (from the alias table) followed by any
    generic ``"<X> virus"`` phrases, de-duplicated case-insensitively in priority order.
    The resolver tries them in order and takes the first that resolves with coverage.
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
    return out


def resolve_virus_taxon(
    name: str,
    *,
    api_base: str = _BVBRC_API_BASE,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> TaxonResolution | None:
    """Resolve ONE canonical virus name to a BV-BRC taxon with sequence coverage (cached).

    Queries the live BV-BRC taxonomy index, picks the highest-``genomes`` node with
    genomes > 0, and returns a :class:`TaxonResolution`. Returns ``None`` (cached) when the
    name resolves to nothing with coverage, or on a BV-BRC network error (logged WARNING).
    Never raises — degrade-loud (G127).
    """
    if not isinstance(name, str) or not name.strip():
        return None
    key = name.strip().lower()
    if key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[key]

    try:
        rows = _query_taxonomy(name.strip(), api_base=api_base, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — degrade-loud: a taxonomy outage must not strand the run
        log.warning("taxonomy_resolver: BV-BRC taxonomy lookup failed for %r: %s", name, exc)
        # Do NOT cache a transient network failure as a permanent miss.
        return None

    best = _pick_best_taxon(rows)
    if best is None:
        log.info("taxonomy_resolver: no BV-BRC taxon with genome coverage for %r", name)
        _RESOLVE_CACHE[key] = None
        return None

    taxon_id, bvbrc_name, genomes = best
    resolution = TaxonResolution(
        taxon_id=taxon_id,
        scientific_name=name.strip(),
        bvbrc_taxon_name=bvbrc_name,
        genomes=genomes,
        matched_name=name.strip(),
    )
    log.info(
        "taxonomy_resolver: %r -> taxon_id=%d (%r, %d genomes)",
        name,
        taxon_id,
        bvbrc_name,
        genomes,
    )
    _RESOLVE_CACHE[key] = resolution
    return resolution


def resolve_query_to_taxon(
    query: str,
    *,
    api_base: str = _BVBRC_API_BASE,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> TaxonResolution | None:
    """Extract virus name(s) from a query and resolve the first with BV-BRC coverage.

    Orchestrates :func:`extract_virus_names` + :func:`resolve_virus_taxon`. Returns the
    first :class:`TaxonResolution` (most-specific candidate first) or ``None`` when no
    extracted name resolves — the caller turns ``None`` into a NAMED degrade note.
    """
    for candidate in extract_virus_names(query):
        resolution = resolve_virus_taxon(candidate, api_base=api_base, timeout=timeout)
        if resolution is not None:
            return resolution
    return None


def _pick_best_taxon(rows: list[dict[str, Any]]) -> tuple[int, str, int] | None:
    """From taxonomy rows (sorted -genomes), return (taxon_id, taxon_name, genomes) of the
    highest-coverage node with genomes > 0, or ``None``."""
    best: tuple[int, str, int] | None = None
    for row in rows:
        try:
            genomes = int(row.get("genomes") or 0)
            taxon_id = int(row.get("taxon_id"))
        except (TypeError, ValueError):
            continue
        if genomes <= 0:
            continue
        name = str(row.get("taxon_name") or "").strip()
        if best is None or genomes > best[2]:
            best = (taxon_id, name, genomes)
    return best


def _query_taxonomy(
    name: str,
    *,
    api_base: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """The BV-BRC taxonomy wire (mockable in unit tests). Raises on HTTP/network error."""
    query = (
        f"eq(taxon_name,{requests.utils.quote(name)})"
        f"&select(taxon_id,taxon_name,taxon_rank,genomes)"
        f"&sort(-genomes)"
        f"&limit({_TAXONOMY_FETCH_LIMIT})"
    )
    url = f"{api_base}/taxonomy/?{query}&http_accept=application/json"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(
            f"taxonomy_resolver: unexpected BV-BRC taxonomy response shape: {type(data).__name__}"
        )
    return data


def _clear_cache() -> None:
    """Test seam — drop the in-process resolution cache."""
    _RESOLVE_CACHE.clear()


__all__ = [
    "TaxonResolution",
    "extract_virus_names",
    "resolve_virus_taxon",
    "resolve_query_to_taxon",
]
