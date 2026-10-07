"""Open-access full-text retrieval for PubMed records (Europe PMC).

PubMed eFetch gives title + ABSTRACT only. For specialized questions (epitope residues,
animal-model protection, cross-lineage neutralization) the abstract routinely omits the
detail the answer needs — the detail lives in the full-text body. This module fetches the
open-access full text for records that carry a PMCID (already present in the eFetch
response, see ``_pubmed_helpers.container_to_dict``) and extracts the readable body text.

Source is Europe PMC (``/{PMCID}/fullTextXML``): keyless, and a DIFFERENT host from NCBI
eUtils, so it does not share the PubMed eFetch rate budget. Only ~a third of PubMed is
open-access, so a miss (HTTP 404 / no ``<body>``) is NORMAL and returns ``""`` — never an
error. A genuine network/API failure raises :class:`FulltextUnavailableError` so the
caller degrades LOUD (keep abstracts) rather than crashing.

Not reusing ``functional/_http.AsyncHttpClient``: that base raises on a 404, whereas here a
404 is the common "this paper isn't open-access" signal that must map to ``""``, not an
exception. Keeping the client self-contained avoids bending the shared base's 404 contract.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

log = logging.getLogger(__name__)

_EUROPEPMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
_DEFAULT_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_BACKOFF_BASE = 1.0
_DEFAULT_CONCURRENCY = 5
# Per-paper char cap on extracted body text. A paper body is ~20-40K chars; many papers'
# worth would blow a synthesis prompt. 20K keeps the substantive sections (intro/results/
# discussion — where epitope + animal-model detail lives) while bounding the payload.
_DEFAULT_CHAR_CAP = 20_000

# Blocks that bloat the body with low-value text for a QA task — drop before tag-stripping.
# Best-effort: the backreference + non-greedy match does not strip same-name NESTED blocks
# (a <fig> inside a <fig>); any residual markup is still removed by the _TAG strip below, so
# the output stays tag-free — this only trims obvious noise, it is not a full XML parser.
_DROP_BLOCKS = re.compile(r"<(ref-list|table-wrap|fig)\b.*?</\1>", re.S | re.I)
_BODY = re.compile(r"<body\b[^>]*>(.*?)</body>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


class FulltextUnavailableError(RuntimeError):
    """Europe PMC unreachable or erroring after retries — caller degrades loud (keep abstracts)."""


def _extract_body_text(xml: str, *, char_cap: int) -> str:
    """Pull readable body text out of a JATS fullTextXML document.

    Drops reference lists, tables and figures (noise for a QA task), strips remaining tags,
    collapses whitespace, and caps length. Returns ``""`` when there is no ``<body>`` (e.g.
    a metadata-only record), so the caller treats it as "no full text", not an error.
    """
    body = _BODY.search(xml or "")
    if not body:
        return ""
    inner = _DROP_BLOCKS.sub(" ", body.group(1))
    text = _WS.sub(" ", _TAG.sub(" ", inner)).strip()
    return text[:char_cap]


async def _fetch_one(client: httpx.AsyncClient, pmcid: str, *, char_cap: int) -> str:
    """Fetch + extract one PMCID's body text. ``""`` on a 404/no-body (not open-access);
    raises :class:`FulltextUnavailableError` on a network error or 429/5xx after retries."""
    url = _EUROPEPMC_FULLTEXT.format(pmcid=pmcid)
    last = ""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt >= _RETRY_ATTEMPTS:
                raise FulltextUnavailableError(f"Europe PMC GET {url} failed: {last}") from exc
            await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
            continue
        if resp.status_code == 404:
            return ""  # not open-access / no full text — a normal miss, not an error
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {resp.status_code}"
            if attempt >= _RETRY_ATTEMPTS:
                raise FulltextUnavailableError(f"Europe PMC GET {url} gave up: {last}")
            await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
            continue
        if resp.is_error:
            return ""
        return _extract_body_text(resp.text, char_cap=char_cap)
    raise FulltextUnavailableError(f"Europe PMC GET {url} gave up: {last}")


async def fetch_fulltext_many(
    pmcids: list[str],
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    char_cap: int = _DEFAULT_CHAR_CAP,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, str]:
    """Fetch open-access body text for each PMCID, bounded-concurrency.

    Returns ``{pmcid: body_text}`` including only PMCIDs with non-empty full text. Best-
    effort per paper: a 404 / no-body miss (not open-access) is skipped, and a per-paper API
    error (e.g. Europe PMC 500 on one malformed record) is logged and skipped so one bad
    paper cannot abort the batch. Raises :class:`FulltextUnavailableError` ONLY when EVERY
    paper errored (Europe PMC systemically down) — so the caller degrades loud to abstracts
    instead of silently returning nothing.
    """
    wanted = [p for p in dict.fromkeys(pmcids) if p and p.strip()]
    if not wanted:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(client: httpx.AsyncClient, pmcid: str) -> tuple[str, str | None]:
        # text on success/miss ("" = no full text); None marks a per-paper API error.
        async with sem:
            try:
                return pmcid, await _fetch_one(client, pmcid, char_cap=char_cap)
            except FulltextUnavailableError as exc:
                log.warning("full text fetch failed for %s (skipping): %s", pmcid, exc)
                return pmcid, None

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        results = await asyncio.gather(*(_guarded(client, p) for p in wanted))

    errors = [pmcid for pmcid, text in results if text is None]
    if len(errors) == len(wanted):
        raise FulltextUnavailableError(
            f"Europe PMC systemically unavailable: all {len(wanted)} full-text fetch(es) failed"
        )
    return {pmcid: text for pmcid, text in results if text}


async def fetch_fulltext(
    pmcid: str, *, char_cap: int = _DEFAULT_CHAR_CAP, timeout: float = _DEFAULT_TIMEOUT
) -> str:
    """Open-access body text for one PMCID (``""`` when not open-access)."""
    out = await fetch_fulltext_many([pmcid], char_cap=char_cap, timeout=timeout)
    return out.get(pmcid, "")


__all__ = [
    "FulltextUnavailableError",
    "fetch_fulltext",
    "fetch_fulltext_many",
]
