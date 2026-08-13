"""Query-value safety helpers for BV-BRC RQL (Solr data-API) predicates.

Shared by the taxonomy and protein-FASTA steps: both interpolate an LLM / synonym / user NAME
into an ``eq(field,value)`` RQL predicate, and BV-BRC returns HTTP 400 on RQL metacharacters in
the value (see ``rql_safe_name``).
"""

from __future__ import annotations

import re


def rql_safe_name(s: str) -> str:
    """Strip parenthetical segments (and any stray parens) that break BV-BRC RQL, and
    collapse whitespace.

    BV-BRC percent-DECODES the query value before RQL-parsing, so a literal ``(`` inside
    ``eq(field,value)`` breaks delimiter matching and returns HTTP 400 (even when the parens
    were percent-encoded by ``quote`` or by ``requests``; RQL double-quoting and backslash-
    escaping were both probed and ALSO 400). A paren-bearing value therefore cannot be
    RQL-matched at all, so stripping the parenthetical cannot lose a *retrievable* match —
    e.g. ``"Zika virus (ZIKV)"`` -> ``"Zika virus"`` and ``"envelope glycoprotein (E1)"`` ->
    ``"envelope glycoprotein"``, both of which the live catalog then resolves (verified
    2026-08-12). NOTE: some real catalog ``product`` values DO carry parentheses (EC-number
    annotations, e.g. ``"thymidine kinase (EC 2.7.1.21)"``); such a product is reachable only
    via a WILDCARD on the stripped stem, never an exact paren match — a consumer doing an
    exact match must fall back to wildcard when the raw input carried a parenthetical.

    Balanced groups are removed first; any residual bare ``(``/``)`` from nested or unbalanced
    input is then dropped, so nothing paren-shaped reaches RQL and the result matches this
    function's name. NOTE: a colon also breaks RQL (Solr's field:value separator;
    ``"HHV: type 1"`` -> 400) but is not an observed LLM-output shape — deferred.
    comma / ampersand / slash were probed and are RQL-safe (no strip needed).
    """
    without_groups = re.sub(r"\s*\([^)]*\)", " ", s)
    return " ".join(without_groups.replace("(", " ").replace(")", " ").split())
